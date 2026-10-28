from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import SERVER_ROOT, auth_headers, register_and_login
from tests.cases.test_dict_cases import fetch_one
from tests.test_admin import login_admin


def capacity(app_client, headers):
    return app_client.get("/api/admin/capacity", headers=headers)


def patch_capacity(app_client, headers, version, value, **extra):
    return app_client.patch(
        "/api/admin/capacity",
        json={"version": version, "max_active_sessions": value, **extra},
        headers=headers,
    )


def test_tc_cap_01_capacity_auth(app_client):
    assert app_client.get("/api/admin/capacity").status_code == 401
    assert app_client.patch(
        "/api/admin/capacity",
        json={"version": 1, "max_active_sessions": 10},
    ).status_code == 401

    user = register_and_login(app_client, email="cap01@example.com")
    user_headers = auth_headers(user)
    assert capacity(app_client, user_headers).status_code == 403
    assert patch_capacity(app_client, user_headers, 1, 10).status_code == 403

    admin_headers = auth_headers(login_admin(app_client))
    view = capacity(app_client, admin_headers)
    assert view.status_code == 200, view.text
    body = view.json()
    assert "max_active_sessions" in body["config"]
    serialized = str(body).lower()
    for forbidden in ("password", "secret", "token", "refined_text", "raw_text"):
        assert forbidden not in serialized


def test_tc_cap_02_capacity_persists_and_is_singleton(app_client):
    headers = auth_headers(login_admin(app_client))
    initial = capacity(app_client, headers).json()
    assert initial["config"]["max_active_sessions"] == 20
    assert initial["config"]["version"] == 1

    updated = patch_capacity(app_client, headers, 1, 10)
    assert updated.status_code == 200, updated.text
    assert updated.json()["config"]["max_active_sessions"] == 10
    assert updated.json()["config"]["version"] == 2

    # A fresh app instance runs ensure_initialized and must keep the stored
    # value instead of reseeding 20.
    fresh_app = create_app(str(SERVER_ROOT / "config" / "app.yaml"))
    with TestClient(fresh_app) as fresh:
        login = fresh.post(
            "/api/auth/login",
            json={
                "email": "admin@example.com",
                "password": "SmokePass123",
                "client_type": "admin_web",
            },
        )
        assert login.status_code == 200, login.text
        reread = fresh.get("/api/admin/capacity", headers=auth_headers(login.json()))
        assert reread.status_code == 200, reread.text
        assert reread.json()["config"]["max_active_sessions"] == 10

    assert (
        fetch_one("select count(*)::int from runtime_capacity") == 1
    )


def test_tc_cap_03_capacity_validation(app_client):
    headers = auth_headers(login_admin(app_client))
    before = capacity(app_client, headers).json()["config"]

    for value in (0, -1, 1.5, True, 21):
        response = patch_capacity(app_client, headers, before["version"], value)
        assert response.status_code == 422, (value, response.text)
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    unknown = patch_capacity(app_client, headers, before["version"], 10, foo="bar")
    assert unknown.status_code == 422, unknown.text
    assert unknown.json()["error"]["code"] == "VALIDATION_ERROR"

    after = capacity(app_client, headers).json()["config"]
    assert after["max_active_sessions"] == before["max_active_sessions"]
    assert after["version"] == before["version"]


def test_tc_cap_04_version_conflict_and_same_value_noop(app_client):
    headers = auth_headers(login_admin(app_client))
    view = capacity(app_client, headers).json()
    version = view["config"]["version"]
    value = view["config"]["max_active_sessions"]

    same = patch_capacity(app_client, headers, version, value)
    assert same.status_code == 200, same.text
    assert same.json()["config"]["version"] == version
    assert fetch_one("select count(*)::int from capacity_changes") == 0

    first = patch_capacity(app_client, headers, version, 10)
    assert first.status_code == 200, first.text
    stale = patch_capacity(app_client, headers, version, 15)
    assert stale.status_code == 409, stale.text
    assert stale.json()["error"]["code"] == "VERSION_CONFLICT"

    assert (
        fetch_one(
            "select old_max_active from capacity_changes order by created_at desc limit 1"
        )
        == value
    )
    assert (
        fetch_one(
            "select new_max_active from capacity_changes order by created_at desc limit 1"
        )
        == 10
    )
    assert (
        fetch_one(
            "select from_version from capacity_changes order by created_at desc limit 1"
        )
        == version
    )
    assert (
        fetch_one(
            "select to_version from capacity_changes order by created_at desc limit 1"
        )
        == version + 1
    )
    current = capacity(app_client, headers).json()["config"]
    assert current["max_active_sessions"] == 10
    assert current["version"] == version + 1
