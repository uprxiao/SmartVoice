"""Deployment §3 HTTP limits: real routes and the existing owned PostgreSQL fixture.

Only the rate clock, ASGI peer address, and one ASR health failure are injected.
No limits are disabled, no token values are printed, and no live model is used.
These are API/DB regressions, not network proxy or load-throughput evidence.
"""
from __future__ import annotations

import pytest

from app.services import rate_limit
from tests.conftest import auth_headers, execute_sql, register_and_login
from tests.ids import fixture_uuid
from tests.test_sessions import create_session, frame
from tests.ws_assertions import assert_handshake_denied, cancel_and_drain

BASE = 1_800_000_000  # Divisible by 3600; UTC hour/minute/ten-minute boundary.
PASSWORD = "RateFixtureOnly123"


@pytest.fixture
def rate_clock(app_client, monkeypatch):
    clock = [BASE + 58.9999]
    # raising=False lets the tests-only patch run against the old implementation,
    # which ignores this clock and therefore exposes its sliding-window mismatch.
    monkeypatch.setattr(rate_limit, "utc_seconds", lambda: clock[0], raising=False)
    return clock


def peer(client, monkeypatch, ip):
    # Starlette's existing TestClient transport supplies ASGI scope['client'].
    # This does not introduce a trusted request header into the application.
    monkeypatch.setattr(client._transport, "client", (ip, 47111))


def register(client, email, **kwargs):
    return client.post("/api/auth/register", json={"email": email, "password": PASSWORD}, **kwargs)


def login(client, email, *, password=PASSWORD, device="rate-device", **kwargs):
    return client.post("/api/auth/login", json={
        "email": email, "password": password, "client_type": "desktop",
        "device": {"installation_id": fixture_uuid(device), "name": "rate-fixture",
                   "platform": "macos", "client_version": "0.0.1"},
    }, **kwargs)


def assert_limited(response, seconds):
    assert response.status_code == 429
    error = response.json()["error"]
    assert error["code"] == "RATE_LIMITED"
    assert error["retryable"] is True
    assert error["details"]["retry_after"] == seconds
    assert response.headers["Retry-After"] == str(seconds)
    assert error["request_id"] == response.headers["X-Request-Id"]
    assert "access_token" not in response.json()
    assert "refresh_token" not in response.json()


def assert_error(response, status, code):
    assert response.status_code == status
    assert response.json()["error"]["code"] == code


def test_register_five_per_real_peer_utc_hour_with_exact_ceil(app_client, monkeypatch, rate_clock):
    rate_clock[0] = BASE + 3598.9999
    peer(app_client, monkeypatch, "198.51.100.10")
    for index in range(5):
        assert register(app_client, f"rate-register-{index}@example.test").status_code == 201
    # Forwarded client strings cannot buy another registration budget.
    blocked = register(app_client, "rate-register-blocked@example.test",
                       headers={"X-Forwarded-For": "198.51.100.99"})
    assert_limited(blocked, 2)
    # Existing body/schema validation still wins; no rate error masks bad JSON.
    assert_error(app_client.post("/api/auth/register", content=b"{",
                               headers={"Content-Type": "application/json"}), 400, "INVALID_JSON")
    assert_error(app_client.post("/api/auth/register", json={}), 422, "VALIDATION_ERROR")
    peer(app_client, monkeypatch, "198.51.100.11")
    assert register(app_client, "rate-register-other-peer@example.test").status_code == 201
    peer(app_client, monkeypatch, "198.51.100.10")
    rate_clock[0] = BASE + 3600
    # Success for this exact email proves the earlier 429 created no user.
    assert register(app_client, "rate-register-blocked@example.test").status_code == 201


def test_login_ip_utc_minute_is_not_a_sliding_minute(app_client, monkeypatch, rate_clock):
    peer(app_client, monkeypatch, "198.51.100.12")
    for _ in range(10):
        assert_error(login(app_client, "rate-absent@example.test"), 401, "AUTH_INVALID")
    assert_limited(login(app_client, "rate-absent@example.test",
                         headers={"X-Forwarded-For": "198.51.100.98"}), 2)
    rate_clock[0] = BASE + 60
    # Only 1.0001 seconds elapsed, but a fresh UTC minute must open the IP window.
    assert_error(login(app_client, "rate-absent@example.test"), 401, "AUTH_INVALID")


def test_login_email_digest_utc_ten_minutes_is_shared_across_peers(app_client, monkeypatch, rate_clock):
    rate_clock[0] = BASE + 598.9999
    for index in range(20):
        peer(app_client, monkeypatch, f"198.51.100.{index + 20}")
        email = " RATE-EMAIL@Example.Test " if index % 2 else "rate-email@example.test"
        assert_error(login(app_client, email), 401, "AUTH_INVALID")
    peer(app_client, monkeypatch, "198.51.100.88")
    assert_limited(login(app_client, "rate-email@example.test"), 2)
    assert_error(login(app_client, "rate-independent@example.test"), 401, "AUTH_INVALID")
    rate_clock[0] = BASE + 600
    assert_error(login(app_client, "rate-email@example.test"), 401, "AUTH_INVALID")


def test_refresh_sixty_per_sid_without_access_token(app_client, rate_clock):
    tokens = register_and_login(app_client, "rate-refresh@example.test")
    second = login(app_client, "rate-refresh@example.test", password="SmokePass123", device="rate-refresh-d2")
    assert second.status_code == 200
    other_sid = second.json()
    payload = {"refresh_token": tokens["refresh_token"]}
    for _ in range(60):
        response = app_client.post("/api/auth/refresh", json=payload)
        assert response.status_code == 200
        assert response.json()["refresh_token"] == tokens["refresh_token"]
        assert response.json()["refresh_expires_at"] == tokens["refresh_expires_at"]
    assert_limited(app_client.post("/api/auth/refresh", json=payload), 2)
    assert app_client.post("/api/auth/refresh", json={"refresh_token": other_sid["refresh_token"]}).status_code == 200
    assert_error(app_client.post("/api/auth/refresh", json={"refresh_token": "not-valid-" * 8}), 401, "AUTH_INVALID")
    execute_sql("update users set status='disabled' where id=$1", tokens["user"]["id"])
    assert_error(app_client.post("/api/auth/refresh", json=payload), 403, "ACCOUNT_DISABLED")
    execute_sql("update users set status='active' where id=$1", tokens["user"]["id"])
    rate_clock[0] = BASE + 60
    assert app_client.post("/api/auth/refresh", json=payload).status_code == 200
    execute_sql("update login_sessions set revoked_at=now() where user_id=$1", tokens["user"]["id"])
    assert_error(app_client.post("/api/auth/refresh", json=payload), 401, "AUTH_INVALID")


def test_user_120_shared_across_devices_routes_and_logout(app_client, rate_clock):
    first = register_and_login(app_client, "rate-user1@example.test")
    second = register_and_login(app_client, "rate-user2@example.test")
    first_headers, second_headers = auth_headers(first), auth_headers(second)
    second_device = login(app_client, "rate-user1@example.test", password="SmokePass123", device="rate-user1-d2")
    assert second_device.status_code == 200
    d2_headers = auth_headers(second_device.json())
    foreign = create_session(app_client, second_headers, "rate-foreign-session")
    assert foreign.status_code == 201
    # Keep tenant isolation as an actual API assertion before exhausting budget.
    foreign_path = f"/api/v1/sessions/{foreign.json()['session_id']}"
    assert_error(app_client.get(foreign_path, headers=first_headers), 404, "NOT_FOUND")
    for _ in range(118):
        assert app_client.get("/api/users/me", headers=first_headers).status_code == 200
    assert app_client.get("/api/v1/settings", headers=d2_headers).status_code == 200
    assert_limited(app_client.get("/api/users/me", headers=first_headers), 2)
    assert_limited(app_client.get("/api/v1/settings", headers=d2_headers), 2)
    assert_limited(app_client.post("/api/auth/logout", headers=first_headers), 2)
    assert app_client.get("/api/users/me", headers=second_headers).status_code == 200
    # Distinct refresh rules remain usable even when normal HTTP is throttled.
    assert app_client.post("/api/auth/refresh", json={"refresh_token": first["refresh_token"]}).status_code == 200
    assert_error(app_client.get("/api/admin/users", headers=first_headers), 403, "FORBIDDEN")
    bad = app_client.get("/api/users/me", headers={"Authorization": "Bearer invalid",
                                                  "Origin": "https://untrusted.example.test"})
    assert_error(bad, 401, "AUTH_INVALID")
    assert "access-control-allow-origin" not in bad.headers
    execute_sql("update users set status='disabled' where id=$1", first["user"]["id"])
    assert_error(app_client.get("/api/users/me", headers=first_headers), 403, "ACCOUNT_DISABLED")
    execute_sql("update users set status='active' where id=$1", first["user"]["id"])
    rate_clock[0] = BASE + 60
    assert app_client.post("/api/auth/logout", headers=first_headers).status_code == 204
    assert_error(app_client.get("/api/users/me", headers=first_headers), 401, "AUTH_INVALID")
    assert app_client.get("/api/users/me", headers=d2_headers).status_code == 200


def test_admin_same_user_budget_and_asr_unready_management_channel(app_client, monkeypatch, rate_clock):
    from app.main import _refresh_readiness

    tokens = register_and_login(app_client, "rate-admin@example.test")
    execute_sql("update users set role='admin' where id=$1", tokens["user"]["id"])
    response = app_client.post("/api/auth/login", json={"email": "rate-admin@example.test",
                              "password": "SmokePass123", "client_type": "admin_web"})
    assert response.status_code == 200
    web_headers = auth_headers(response.json())

    async def unavailable():
        return False

    monkeypatch.setattr(app_client.app.state.asr, "health", unavailable)
    app_client.portal.call(_refresh_readiness, app_client.app)
    assert app_client.app.state.ready is False
    # ASR is unavailable, but DB and management routes remain genuinely usable.
    for _ in range(119):
        assert app_client.get("/api/admin/users", headers=web_headers).status_code == 200
    assert app_client.get("/api/users/me", headers=auth_headers(tokens)).status_code == 200
    assert_limited(app_client.get("/api/admin/users", headers=web_headers), 2)
    # Client-role denial is checked before the user bucket; read paths still run.
    assert_error(app_client.get(f"/api/v1/sessions/{fixture_uuid('rate-admin-missing')}",
                               headers=web_headers), 403, "FORBIDDEN")
    rate_clock[0] = BASE + 60
    assert app_client.get("/api/admin/users", headers=web_headers).status_code == 200


def test_active_websocket_frames_are_not_http_requests_and_origin_still_precedes_auth(app_client, rate_clock):
    tokens = register_and_login(app_client, "rate-ws@example.test")
    headers = auth_headers(tokens)
    # Fill most of the window before creating a session, preserving the real
    # 10-second created/streaming idle deadline even on a slow database host.
    for _ in range(119):
        assert app_client.get("/api/users/me", headers=headers).status_code == 200
    created = create_session(app_client, headers, "rate-ws-session")
    assert created.status_code == 201
    assert_limited(app_client.get("/api/users/me", headers=headers), 2)
    path = f"/ws/v1/sessions/{created.json()['session_id']}/audio"
    with app_client.websocket_connect(path, headers=headers) as ws:
        assert ws.receive_json()["type"] == "ready"
        assert_handshake_denied(app_client, path,
            {"Origin": "https://untrusted.example.test", "Authorization": "Bearer invalid"},
            403, "FORBIDDEN")
        assert_handshake_denied(app_client, path, {"Authorization": "Bearer invalid"}, 401, "AUTH_INVALID")
        ws.send_bytes(frame(0))
        assert ws.receive_json()["type"] == "audio.ack"
        cancel_and_drain(ws)
