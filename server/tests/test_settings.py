from __future__ import annotations

from tests.ids import fixture_uuid

from tests.conftest import auth_headers, register_and_login


def test_settings_version_cas_and_validation(app_client):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)

    current = app_client.get("/api/v1/settings", headers=headers)
    assert current.status_code == 200
    assert current.json() == {
        "history_retention_days": None,
        "learning_enabled": True,
        "locale": "zh-CN",
        "version": 1,
    }

    updated = app_client.patch(
        "/api/v1/settings",
        json={"version": 1, "learning_enabled": False},
        headers=headers,
    )
    assert updated.status_code == 200
    assert updated.json()["learning_enabled"] is False
    assert updated.json()["version"] == 2

    stale = app_client.patch(
        "/api/v1/settings",
        json={"version": 1, "locale": "en"},
        headers=headers,
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "VERSION_CONFLICT"
    assert stale.json()["error"]["details"]["current_version"] == 2

    invalid = app_client.patch(
        "/api/v1/settings",
        json={"version": 2, "history_retention_days": 15},
        headers=headers,
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "VALIDATION_ERROR"

    final = app_client.patch(
        "/api/v1/settings",
        json={"version": 2, "history_retention_days": 30, "locale": "en"},
        headers=headers,
    )
    assert final.status_code == 200
    body = final.json()
    assert body["history_retention_days"] == 30
    assert body["locale"] == "en"
    assert body["version"] == 3

    cleared = app_client.patch(
        "/api/v1/settings",
        json={"version": 3, "history_retention_days": None},
        headers=headers,
    )
    assert cleared.status_code == 200
    assert cleared.json()["history_retention_days"] is None


def test_devices_lists_current_installation(app_client):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    response = app_client.get("/api/devices", headers=headers)
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["installation_id"] == fixture_uuid("test-installation")
    assert items[0]["is_current"] is True


def test_display_name_update(app_client):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    response = app_client.patch(
        "/api/users/me", json={"display_name": " 留步 "}, headers=headers
    )
    assert response.status_code == 200
    assert response.json()["user"]["display_name"] == "留步"
    cleared = app_client.patch(
        "/api/users/me", json={"display_name": None}, headers=headers
    )
    assert cleared.status_code == 200
    assert cleared.json()["user"]["display_name"] is None
