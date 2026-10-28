from __future__ import annotations

from tests.ids import fixture_uuid

import asyncio
from datetime import timedelta

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.core.security import create_access_token
from tests.conftest import (
    PG_HOST,
    PG_PASSWORD,
    PG_PORT,
    PG_USER,
    TEST_DB,
    auth_headers,
    execute_sql,
    register_and_login,
)

PASSWORD = "Password123"


def register(client: TestClient, email: str, password: str = PASSWORD):
    return client.post("/api/auth/register", json={"email": email, "password": password})


def login(
    client: TestClient,
    email: str,
    password: str = PASSWORD,
    installation: str = "test-installation",
):
    return client.post(
        "/api/auth/login",
        json={
            "email": email,
            "password": password,
            "client_type": "desktop",
            "device": {
                "installation_id": fixture_uuid(installation),
                "name": "test-mac",
                "platform": "macos",
                "client_version": "0.0.1",
            },
        },
    )


def fetch_one(sql: str, *args):
    async def run():
        conn = await asyncpg.connect(
            host=PG_HOST,
            port=PG_PORT,
            user=PG_USER,
            password=PG_PASSWORD,
            database=TEST_DB,
        )
        try:
            return await conn.fetchval(sql, *args)
        finally:
            await conn.close()

    return asyncio.run(run())


def test_tc_auth_01_register_and_login(app_client):
    response = register(app_client, "auth01@example.com")
    assert response.status_code == 201, response.text
    assert response.json()["role"] == "user"
    assert login(app_client, "auth01@example.com").status_code == 200


def test_tc_auth_02_duplicate_email_variants(app_client):
    assert register(app_client, "auth02@example.com").status_code == 201
    before = fetch_one("select count(*) from users")
    for variant in ("auth02@example.com", "  AUTH02@Example.com  ", "Auth02@EXAMPLE.com"):
        response = register(app_client, variant)
        assert response.status_code == 409, (variant, response.text)
        assert response.json()["error"]["code"] == "EMAIL_EXISTS"
    assert fetch_one("select count(*) from users") == before


def test_tc_auth_03_password_and_email_boundaries(app_client):
    assert register(app_client, "auth03-short@example.com", "1234567").status_code == 422
    assert register(app_client, "auth03-ok8@example.com", "12345678").status_code == 201
    assert register(app_client, "auth03-long129@example.com", "x" * 129).status_code == 422
    assert register(app_client, "auth03-ok128@example.com", "x" * 128).status_code == 201
    spaced = "  pass word  "
    assert register(app_client, "auth03-space@example.com", spaced).status_code == 201
    assert login(app_client, "auth03-space@example.com", spaced).status_code == 200
    emoji = "😀" * 8
    assert register(app_client, "auth03-emoji@example.com", emoji).status_code == 201
    assert register(app_client, "not-an-email", PASSWORD).status_code == 422
    assert register(app_client, ("a" * 250) + "@example.com", PASSWORD).status_code == 422


def test_tc_auth_04_token_pair_and_devices(app_client):
    register(app_client, "auth04@example.com")
    response = login(app_client, "auth04@example.com", installation="auth04-device")
    assert response.status_code == 200, response.text
    pair = response.json()
    assert pair["expires_in"] == 900
    assert pair["user"]["email"] == "auth04@example.com"
    expires = pair["refresh_expires_at"].replace("Z", "+00:00")
    from datetime import datetime

    delta = datetime.fromisoformat(expires) - datetime.now(datetime.fromisoformat(expires).tzinfo)
    assert timedelta(days=29, hours=23) < delta < timedelta(days=30, minutes=1)
    devices = app_client.get("/api/devices", headers=auth_headers(pair))
    assert devices.status_code == 200, devices.text
    body = devices.json()
    items = body["items"] if isinstance(body, dict) else body
    assert any(item.get("installation_id") == fixture_uuid("auth04-device") for item in items)


def test_tc_auth_05_unknown_and_wrong_password_same_error(app_client):
    register(app_client, "auth05@example.com")
    wrong = login(app_client, "auth05@example.com", "WrongPassword1")
    missing = login(app_client, "nobody-auth05@example.com", "WrongPassword1")
    assert wrong.status_code == missing.status_code == 401
    assert wrong.json()["error"]["code"] == missing.json()["error"]["code"] == "AUTH_INVALID"
    assert wrong.json()["error"]["message"] == missing.json()["error"]["message"]


def test_tc_auth_06_disabled_account_login(app_client):
    register(app_client, "auth06@example.com")
    execute_sql("update users set status='disabled' where email=$1", "auth06@example.com")
    response = login(app_client, "auth06@example.com")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ACCOUNT_DISABLED"
    assert "access_token" not in response.text


def test_tc_auth_07_expired_access_refresh_and_retry(app_client):
    register(app_client, "auth07@example.com")
    pair = login(app_client, "auth07@example.com").json()
    config = app_client.app.state.config
    import jwt

    real = jwt.decode(
        pair["access_token"],
        config.jwt_secret,
        algorithms=["HS256"],
        options={"verify_aud": False},
    )
    expired, _ = create_access_token(
        config.auth,
        config.jwt_secret,
        pair["user"]["id"],
        real["sid"],
        "user",
        ttl=timedelta(seconds=-5),
    )
    expired_headers = {"Authorization": f"Bearer {expired}"}
    first = app_client.patch(
        "/api/users/me", json={"display_name": "重试后"}, headers=expired_headers
    )
    assert first.status_code == 401
    assert fetch_one("select display_name from users where email=$1", "auth07@example.com") is None
    refreshed = app_client.post("/api/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    assert refreshed.status_code == 200
    second = app_client.patch(
        "/api/users/me", json={"display_name": "重试后"}, headers=auth_headers(refreshed.json())
    )
    assert second.status_code == 200
    assert (
        fetch_one("select display_name from users where email=$1", "auth07@example.com") == "重试后"
    )


def test_tc_auth_08_logout_only_current_device(app_client):
    register(app_client, "auth08@example.com")
    d1 = login(app_client, "auth08@example.com", installation="auth08-d1").json()
    d2 = login(app_client, "auth08@example.com", installation="auth08-d2").json()
    response = app_client.post("/api/auth/logout", headers=auth_headers(d1))
    assert response.status_code == 204
    assert app_client.get("/api/users/me", headers=auth_headers(d1)).status_code == 401
    assert (
        app_client.post("/api/auth/refresh", json={"refresh_token": d1["refresh_token"]}).status_code
        == 401
    )
    assert app_client.get("/api/users/me", headers=auth_headers(d2)).status_code == 200


def test_tc_auth_10_devices_and_same_installation_revoke(app_client):
    register(app_client, "auth10@example.com")
    first = login(app_client, "auth10@example.com", installation="auth10-d1").json()
    second = login(app_client, "auth10@example.com", installation="auth10-d2").json()
    body = app_client.get("/api/devices", headers=auth_headers(second)).json()
    items = body["items"] if isinstance(body, dict) else body
    ids = {item.get("installation_id") for item in items}
    assert {fixture_uuid("auth10-d1"), fixture_uuid("auth10-d2")} <= ids
    again = login(app_client, "auth10@example.com", installation="auth10-d1").json()
    assert app_client.get("/api/users/me", headers=auth_headers(first)).status_code == 401
    assert app_client.get("/api/users/me", headers=auth_headers(second)).status_code == 200
    assert app_client.get("/api/users/me", headers=auth_headers(again)).status_code == 200


def test_tc_auth_11_cross_tenant_not_found(app_client):
    register(app_client, "auth11-u1@example.com")
    register(app_client, "auth11-u2@example.com")
    u1 = login(app_client, "auth11-u1@example.com", installation="auth11-u1").json()
    u2 = login(app_client, "auth11-u2@example.com", installation="auth11-u2").json()
    created = app_client.post(
        "/api/v1/sessions",
        json={"client_request_id": fixture_uuid("auth11-session"), "learning_enabled": False},
        headers={**auth_headers(u1), "Idempotency-Key": fixture_uuid("auth11-session")},
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["session_id"]
    assert (
        app_client.get(f"/api/v1/sessions/{session_id}", headers=auth_headers(u1)).status_code
        == 200
    )
    foreign = app_client.get(f"/api/v1/sessions/{session_id}", headers=auth_headers(u2))
    assert foreign.status_code == 404
    assert foreign.json()["error"]["code"] == "NOT_FOUND"


def test_tc_auth_12_login_rate_limit(app_client, monkeypatch):
    monkeypatch.setattr("app.services.rate_limit.utc_seconds", lambda: 1_800_000_001.0, raising=False)
    register(app_client, "auth12@example.com")
    register(app_client, "auth12-other@example.com")
    for _ in range(10):
        assert login(app_client, "auth12@example.com", "WrongPassword1").status_code == 401
    blocked = login(app_client, "auth12@example.com", "WrongPassword1")
    assert blocked.status_code == 429, blocked.text
    assert blocked.json()["error"]["code"] == "RATE_LIMITED"
    ip_retry = int(blocked.headers.get("Retry-After", "0"))
    assert 1 <= ip_retry <= 60

    # A second IP exhausts the per-email budget (20 attempts / 10 minutes).
    limiter = app_client.app.state.login_limiter
    for _ in range(10):
        assert limiter.check("198.51.100.11", "auth12@example.com") is None
    email_retry = limiter.check("198.51.100.11", "auth12@example.com")
    assert email_retry is not None and email_retry > 60
    assert limiter.check("198.51.100.12", "auth12-other@example.com") is None
    # Twenty accepted checks above exhaust the email budget across IPs.
    assert limiter.check("198.51.100.13", "auth12@example.com") == email_retry


def test_tc_auth_13_refresh_revoked_and_stable_expiry(app_client):
    register(app_client, "auth13@example.com")
    pair = login(app_client, "auth13@example.com").json()
    first = app_client.post("/api/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    second = app_client.post("/api/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    assert first.status_code == second.status_code == 200
    assert first.json()["refresh_expires_at"] == pair["refresh_expires_at"]
    assert (
        app_client.post("/api/auth/refresh", json={"refresh_token": "x" * 40}).status_code
        == 401
    )
    app_client.post("/api/auth/logout", headers=auth_headers(second.json()))
    assert (
        app_client.post("/api/auth/refresh", json={"refresh_token": pair["refresh_token"]}).status_code
        == 401
    )
    # Expired refresh tokens are rejected even when never revoked.
    expired = login(app_client, "auth13@example.com", installation="auth13-expired").json()
    execute_sql(
        "update login_sessions set refresh_expires_at = now() - interval '1 second' "
        "where id = (select id from login_sessions where user_id="
        "(select id from users where email=$1) order by created_at desc limit 1)",
        "auth13@example.com",
    )
    assert (
        app_client.post(
            "/api/auth/refresh", json={"refresh_token": expired["refresh_token"]}
        ).status_code
        == 401
    )


def test_tc_auth_14_admin_disable_blocks_login(app_client):
    register(app_client, "auth14-user@example.com")
    register(app_client, "auth14-admin@example.com")
    execute_sql("update users set role='admin' where email=$1", "auth14-admin@example.com")
    admin = app_client.post(
        "/api/auth/login",
        json={
            "email": "auth14-admin@example.com",
            "password": PASSWORD,
            "client_type": "admin_web",
        },
    )
    assert admin.status_code == 200, admin.text
    old = login(app_client, "auth14-user@example.com", installation="auth14-d1").json()
    user_id = fetch_one("select id from users where email=$1", "auth14-user@example.com")
    disabled = app_client.patch(
        f"/api/admin/users/{user_id}",
        json={"status": "disabled"},
        headers=auth_headers(admin.json()),
    )
    assert disabled.status_code == 200, disabled.text
    denied_login = login(app_client, "auth14-user@example.com")
    assert denied_login.status_code == 403
    assert denied_login.json()["error"]["code"] == "ACCOUNT_DISABLED"
    denied_old = app_client.get("/api/users/me", headers=auth_headers(old))
    assert denied_old.status_code == 403
    assert denied_old.json()["error"]["code"] == "ACCOUNT_DISABLED"
    enabled = app_client.patch(
        f"/api/admin/users/{user_id}",
        json={"status": "active"},
        headers=auth_headers(admin.json()),
    )
    assert enabled.status_code == 200, enabled.text
    revoked = app_client.get("/api/users/me", headers=auth_headers(old))
    assert revoked.status_code == 401
    assert revoked.json()["error"]["code"] == "AUTH_INVALID"
    assert login(app_client, "auth14-user@example.com").status_code == 200


def test_tc_auth_14_ws_closes_after_admin_disable(app_client):
    import struct
    import time

    from starlette.websockets import WebSocketDisconnect

    register(app_client, "auth14b-user@example.com")
    register(app_client, "auth14b-admin@example.com")
    execute_sql("update users set role='admin' where email=$1", "auth14b-admin@example.com")
    admin = app_client.post(
        "/api/auth/login",
        json={
            "email": "auth14b-admin@example.com",
            "password": PASSWORD,
            "client_type": "admin_web",
        },
    )
    assert admin.status_code == 200, admin.text
    pair = login(app_client, "auth14b-user@example.com", installation="auth14b-d1").json()
    created = app_client.post(
        "/api/v1/sessions",
        json={"client_request_id": fixture_uuid("auth14b-session"), "learning_enabled": False},
        headers={**auth_headers(pair), "Idempotency-Key": fixture_uuid("auth14b-session")},
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["session_id"]
    user_id = fetch_one("select id from users where email=$1", "auth14b-user@example.com")
    closed_at = None
    with app_client.websocket_connect(
        f"/ws/v1/sessions/{session_id}/audio",
        headers={"authorization": f"Bearer {pair['access_token']}"},
    ) as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_bytes(struct.pack("<II", 0, 10) + b"\x00\x00" * 10)
        started = time.monotonic()
        disabled = app_client.patch(
            f"/api/admin/users/{user_id}",
            json={"status": "disabled"},
            headers=auth_headers(admin.json()),
        )
        assert disabled.status_code == 200, disabled.text
        seen = []
        while True:
            try:
                seen.append(ws.receive_json().get("type"))
            except WebSocketDisconnect as exc:
                closed_at = time.monotonic()
                assert exc.code == 4403
                break
    assert closed_at is not None
    assert "result.ready" not in seen
    assert closed_at - started <= 5.0


def test_tc_auth_19_ws_closes_when_access_expires(app_client):
    import struct
    import time

    import jwt
    from starlette.websockets import WebSocketDisconnect

    register(app_client, "auth19@example.com")
    pair = login(app_client, "auth19@example.com", installation="auth19-d1").json()
    config = app_client.app.state.config
    real = jwt.decode(
        pair["access_token"],
        config.jwt_secret,
        algorithms=["HS256"],
        options={"verify_aud": False},
    )
    short_lived, _ = create_access_token(
        config.auth,
        config.jwt_secret,
        pair["user"]["id"],
        real["sid"],
        "user",
        ttl=timedelta(seconds=3),
    )
    created = app_client.post(
        "/api/v1/sessions",
        json={"client_request_id": fixture_uuid("auth19-session"), "learning_enabled": False},
        headers={**auth_headers(pair), "Idempotency-Key": fixture_uuid("auth19-session")},
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["session_id"]
    started = time.monotonic()
    closed_at = None
    error_seen = None
    with app_client.websocket_connect(
        f"/ws/v1/sessions/{session_id}/audio",
        headers={"authorization": f"Bearer {short_lived}"},
    ) as ws:
        ready = ws.receive_json()
        assert ready["type"] == "ready"
        ws.send_bytes(struct.pack("<II", 0, 10) + b"\x00\x00" * 10)
        while True:
            try:
                message = ws.receive_json()
            except WebSocketDisconnect as exc:
                closed_at = time.monotonic()
                assert exc.code == 4401
                break
            if message.get("type") == "error":
                error_seen = message
    assert error_seen is not None
    assert error_seen["error"]["code"] == "AUTH_INVALID"
    assert closed_at is not None
    assert closed_at - started <= 6.5


def test_tc_auth_17_registration_cannot_escalate(app_client):
    response = app_client.post(
        "/api/auth/register",
        json={"email": "auth17@example.com", "password": PASSWORD, "role": "admin"},
    )
    assert response.status_code == 422, response.text
    register(app_client, "auth17-plain@example.com")
    denied = app_client.post(
        "/api/auth/login",
        json={
            "email": "auth17-plain@example.com",
            "password": PASSWORD,
            "client_type": "admin_web",
        },
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "FORBIDDEN"
