from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import SERVER_ROOT, auth_headers
from tests.cases.test_dict_cases import fetch_one, login, new_user
from tests.test_sessions import create_session


def settings(client, headers):
    return client.get("/api/v1/settings", headers=headers)


def patch_settings(client, headers, version, **values):
    return client.patch(
        "/api/v1/settings",
        json={"version": version, **values},
        headers=headers,
    )


def session_learning(client, headers, request_id):
    session = create_session(client, headers, request_id).json()
    return session, fetch_one(
        "select learning_enabled from sessions where id=$1", session["session_id"]
    )


def test_tc_set_02_locale_persists_across_devices(app_client):
    tokens = new_user(app_client, "set02@example.com", "set02-d1")
    headers = auth_headers(tokens)
    current = settings(app_client, headers).json()
    assert current["locale"] == "zh-CN"
    patched = patch_settings(app_client, headers, current["version"], locale="en")
    assert patched.status_code == 200, patched.text
    assert patched.json()["locale"] == "en"
    second_login = login(app_client, "set02@example.com", "set02-d2")
    assert second_login.status_code == 200
    assert (
        settings(app_client, auth_headers(second_login.json())).json()["locale"]
        == "en"
    )
    fresh_app = create_app(str(SERVER_ROOT / "config" / "app.yaml"))
    with TestClient(fresh_app) as fresh:
        fresh_login = login(fresh, "set02@example.com", "set02-d3")
        assert fresh_login.status_code == 200
        assert (
            settings(fresh, auth_headers(fresh_login.json())).json()["locale"]
            == "en"
        )


def test_tc_set_06_learning_toggle_is_frozen_per_session(app_client):
    tokens = new_user(app_client, "set06@example.com", "set06-d1")
    headers = auth_headers(tokens)
    current = settings(app_client, headers).json()
    assert current["learning_enabled"] is True

    disabled = patch_settings(
        app_client, headers, current["version"], learning_enabled=False
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["learning_enabled"] is False
    assert settings(app_client, headers).json()["learning_enabled"] is False
    off_session, off_flag = session_learning(
        app_client, headers, "set06-off-session"
    )
    assert off_flag is False
    app_client.post(
        f"/api/v1/sessions/{off_session['session_id']}/cancel", headers=headers
    )

    enabled = patch_settings(
        app_client, headers, disabled.json()["version"], learning_enabled=True
    )
    assert enabled.status_code == 200
    on_session, on_flag = session_learning(app_client, headers, "set06-on-session")
    assert on_flag is True
    assert (
        fetch_one(
            "select learning_enabled from sessions where id=$1",
            off_session["session_id"],
        )
        is False
    )
    app_client.post(
        f"/api/v1/sessions/{on_session['session_id']}/cancel", headers=headers
    )
