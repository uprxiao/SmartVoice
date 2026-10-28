from __future__ import annotations

from tests.ids import fixture_uuid

import datetime as dt
import uuid
from zoneinfo import ZoneInfo

from tests.conftest import auth_headers, execute_sql, register_and_login
from tests.test_sessions import create_session, dictation


def key() -> str:
    return str(uuid.uuid4())


def login_admin(app_client) -> dict:
    register_and_login(app_client, email="admin@example.com")
    execute_sql("UPDATE users SET role='admin' WHERE email=$1", "admin@example.com")
    response = app_client.post(
        "/api/auth/login",
        json={
            "email": "admin@example.com",
            "password": "SmokePass123",
            "client_type": "admin_web",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_admin_requires_admin_role(app_client):
    tokens = register_and_login(app_client)
    response = app_client.get("/api/admin/users", headers=auth_headers(tokens))
    assert response.status_code == 403


def test_admin_users_list_and_create(app_client):
    tokens = login_admin(app_client)
    headers = auth_headers(tokens)

    listed = app_client.get("/api/admin/users", headers=headers)
    assert listed.status_code == 200, listed.text
    assert any(item["user"]["email"] == "admin@example.com" for item in listed.json()["items"])

    idem = key()
    created = app_client.post(
        "/api/admin/users",
        json={"email": "member@example.com", "password": "MemberPass123"},
        headers={**headers, "Idempotency-Key": idem},
    )
    assert created.status_code == 201, created.text
    assert created.json()["user"]["role"] == "user"
    assert created.json()["replayed"] is False

    replay = app_client.post(
        "/api/admin/users",
        json={"email": "member@example.com", "password": "MemberPass123"},
        headers={**headers, "Idempotency-Key": idem},
    )
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["user"]["id"] == created.json()["user"]["id"]


def test_admin_disable_revokes_and_protects(app_client):
    tokens = login_admin(app_client)
    headers = auth_headers(tokens)
    created = app_client.post(
        "/api/admin/users",
        json={"email": "member@example.com", "password": "MemberPass123"},
        headers={**headers, "Idempotency-Key": key()},
    ).json()["user"]

    member_login = app_client.post(
        "/api/auth/login",
        json={
            "email": "member@example.com",
            "password": "MemberPass123",
            "client_type": "desktop",
            "device": {
                "installation_id": fixture_uuid("member-installation"),
                "name": "member-mac",
                "platform": "macos",
                "client_version": "0.1.0",
            },
        },
    )
    assert member_login.status_code == 200
    member_headers = auth_headers(member_login.json())

    disabled = app_client.patch(
        f"/api/admin/users/{created['id']}",
        json={"status": "disabled"},
        headers=headers,
    )
    assert disabled.status_code == 200
    assert disabled.json()["user"]["status"] == "disabled"
    denied = app_client.get("/api/v1/settings", headers=member_headers)
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "ACCOUNT_DISABLED"

    self_disable = app_client.patch(
        f"/api/admin/users/{tokens['user']['id']}",
        json={"status": "disabled"},
        headers=headers,
    )
    assert self_disable.status_code == 409
    assert self_disable.json()["error"]["code"] == "ADMIN_PROTECTED"


def test_admin_stats_and_capacity(app_client):
    # This API integration test requires an execution boundary with a recorder;
    # production MockAsrProvider intentionally has no physical decoder ledger.
    from tests.usage_fixture import install_instrumented_fixture_asr

    asr_fixture = install_instrumented_fixture_asr(app_client)
    admin = login_admin(app_client)
    headers = auth_headers(admin)
    user = register_and_login(app_client, email="member@example.com")
    user_headers = auth_headers(user)
    session = create_session(app_client, user_headers, "admin-stats-session").json()
    events = dictation(app_client, user_headers, session["session_id"])
    assert events[-1]["type"] == "result.ready"
    assert len(asr_fixture.executions) == 1
    assert asr_fixture.executions[0][1:] == (session["session_id"], 8000)
    replay = create_session(app_client, user_headers, "admin-stats-session")
    assert replay.status_code == 200 and replay.json()["replayed"] is True
    assert len(asr_fixture.executions) == 1

    day = dt.datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    overview = app_client.get(
        "/api/admin/stats/overview",
        params={"date_from": day, "date_to": day},
        headers=headers,
    )
    assert overview.status_code == 200, overview.text
    body = overview.json()
    assert body["registered_users"] == 2
    assert body["active_users"] == 2
    assert body["distinct_active_users"] == 1
    assert body["totals"]["sessions"] == 1
    assert body["daily"][0]["sessions"] == 1

    usage = app_client.get(
        "/api/admin/stats/usage",
        params={"date_from": day, "date_to": day},
        headers=headers,
    )
    assert usage.status_code == 200
    assert usage.json()["items"][0]["sessions"] == 1
    assert usage.json()["items"][0]["user_id"] == user["user"]["id"]

    models = app_client.get(
        "/api/admin/stats/models",
        params={"date_from": day, "date_to": day},
        headers=headers,
    )
    assert models.status_code == 200
    purposes = {item["purpose"] for item in models.json()["items"]}
    assert "asr_finish" in purposes
    assert "refine" in purposes
    asr_usage = next(item for item in models.json()["items"] if item["purpose"] == "asr_finish")
    assert asr_usage["provider"] == "test-instrumented-asr"
    assert asr_usage["calls"] == asr_usage["successes"] == 1
    assert asr_usage["processed_audio_ms"] == 500

    capacity = app_client.get("/api/admin/capacity", headers=headers)
    assert capacity.status_code == 200, capacity.text
    view = capacity.json()
    assert view["config"]["version"] == 1
    assert view["deployment"]["effective_ceiling"] == 20
    assert view["validation"]["status"] == "unverified"

    updated = app_client.patch(
        "/api/admin/capacity",
        json={"version": 1, "max_active_sessions": 10},
        headers=headers,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["config"]["version"] == 2
    assert updated.json()["config"]["max_active_sessions"] == 10

    stale = app_client.patch(
        "/api/admin/capacity",
        json={"version": 1, "max_active_sessions": 12},
        headers=headers,
    )
    assert stale.status_code == 409

    too_high = app_client.patch(
        "/api/admin/capacity",
        json={"version": 2, "max_active_sessions": 21},
        headers=headers,
    )
    assert too_high.status_code == 422
    assert too_high.json()["error"]["details"]["max_allowed"] == 20

    bool_value = app_client.patch(
        "/api/admin/capacity",
        json={"version": 2, "max_active_sessions": True},
        headers=headers,
    )
    assert bool_value.status_code == 422


def test_admin_stats_validation(app_client):
    tokens = login_admin(app_client)
    headers = auth_headers(tokens)
    for params in (
        {"date_from": "2026-10-17", "date_to": "2026-10-01"},
        {"date_from": "2023-01-01", "date_to": "2026-10-17"},
    ):
        response = app_client.get("/api/admin/stats/overview", params=params, headers=headers)
        assert response.status_code == 422
