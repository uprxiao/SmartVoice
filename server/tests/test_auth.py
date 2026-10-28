from __future__ import annotations

from tests.ids import fixture_uuid

from tests.conftest import auth_headers, register_and_login


def test_register_login_refresh_logout(app_client):
    tokens = register_and_login(app_client)
    assert tokens["user"]["role"] == "user"
    assert tokens["token_type"] == "Bearer"

    headers = auth_headers(tokens)
    me = app_client.get("/api/users/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["user"]["email"] == "user@example.com"
    assert me.json()["settings"]["version"] == 1

    refreshed = app_client.post(
        "/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert refreshed.status_code == 200
    assert refreshed.json()["refresh_token"] == tokens["refresh_token"]

    logout = app_client.post("/api/auth/logout", headers=headers)
    assert logout.status_code == 204
    assert app_client.get("/api/users/me", headers=headers).status_code == 401


def test_register_rejects_duplicate_email(app_client):
    register_and_login(app_client)
    response = app_client.post(
        "/api/auth/register",
        json={"email": "USER@example.com", "password": "SmokePass123"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "EMAIL_EXISTS"


def test_register_validation(app_client):
    short = app_client.post(
        "/api/auth/register", json={"email": "a@example.com", "password": "short"}
    )
    assert short.status_code == 422
    assert short.json()["error"]["code"] == "VALIDATION_ERROR"
    bad_email = app_client.post(
        "/api/auth/register", json={"email": "not-an-email", "password": "SmokePass123"}
    )
    assert bad_email.status_code == 422


def test_login_rejects_wrong_password(app_client):
    register_and_login(app_client)
    response = app_client.post(
        "/api/auth/login",
        json={
            "email": "user@example.com",
            "password": "WrongPass123",
            "client_type": "desktop",
            "device": {
                "installation_id": fixture_uuid("test-installation"),
                "name": "test-mac",
                "platform": "macos",
                "client_version": "0.1.0",
            },
        },
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_INVALID"


def test_admin_web_requires_admin_role(app_client):
    register_and_login(app_client)
    response = app_client.post(
        "/api/auth/login",
        json={
            "email": "user@example.com",
            "password": "SmokePass123",
            "client_type": "admin_web",
        },
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


def test_login_rejects_admin_device_combination(app_client):
    register_and_login(app_client)
    response = app_client.post(
        "/api/auth/login",
        json={
            "email": "user@example.com",
            "password": "SmokePass123",
            "client_type": "admin_web",
            "device": {
                "installation_id": fixture_uuid("test-installation"),
                "name": "test-mac",
                "platform": "macos",
                "client_version": "0.1.0",
            },
        },
    )
    assert response.status_code == 422


def test_desktop_login_requires_device(app_client):
    register_and_login(app_client)
    response = app_client.post(
        "/api/auth/login",
        json={
            "email": "user@example.com",
            "password": "SmokePass123",
            "client_type": "desktop",
        },
    )
    assert response.status_code == 422


def test_missing_token_is_rejected(app_client):
    response = app_client.get("/api/v1/settings")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_INVALID"
