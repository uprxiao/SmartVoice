"""Pre-upgrade HTTP denial contract. These remain TestClient tests, not wire proof."""
from __future__ import annotations

from datetime import timedelta
import uuid

import jwt
import pytest
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from app.core.security import create_access_token
from tests.conftest import auth_headers, execute_sql, register_and_login
from tests.ids import fixture_uuid
from tests.test_sessions import create_session, frame
from tests.ws_assertions import assert_handshake_denied, cancel_and_drain


def pending(client, name):
    tokens = register_and_login(client, email=f"{name}@example.test")
    headers = auth_headers(tokens)
    response = create_session(client, headers, name)
    assert response.status_code == 201
    return tokens, headers, response.json()


@pytest.mark.parametrize("credential_kind", ["malformed", "expired", "revoked"])
def test_invalid_pre_upgrade_credentials_are_http401(app_client, credential_kind):
    tokens, headers, session = pending(app_client, f"denial-{credential_kind}")
    if credential_kind == "malformed":
        headers = {"Authorization": "Bearer not-a-valid-token"}
    elif credential_kind == "expired":
        cfg = app_client.app.state.config
        payload = jwt.decode(tokens["access_token"], cfg.jwt_secret,
                             algorithms=["HS256"], options={"verify_aud": False})
        expired, _ = create_access_token(cfg.auth, cfg.jwt_secret,
                                        tokens["user"]["id"], payload["sid"], "user",
                                        ttl=timedelta(seconds=-1))
        headers = {"Authorization": f"Bearer {expired}"}
    else:
        assert app_client.post("/api/auth/logout", headers=headers).status_code == 204
    assert_handshake_denied(app_client, f"/ws/v1/sessions/{session['session_id']}/audio",
                            headers, 401, "AUTH_INVALID")


def test_disabled_pre_upgrade_account_preserves_http403_account_disabled(app_client):
    tokens, headers, session = pending(app_client, "denial-disabled")
    execute_sql("update users set status='disabled' where id=$1", tokens["user"]["id"])
    assert_handshake_denied(app_client, f"/ws/v1/sessions/{session['session_id']}/audio",
                            headers, 403, "ACCOUNT_DISABLED")


def test_other_desktop_device_is_http403_and_does_not_claim_the_connection(app_client):
    tokens, headers, session = pending(app_client, "denial-device")
    response = app_client.post("/api/auth/login", json={
        "email": "denial-device@example.test", "password": "SmokePass123",
        "client_type": "desktop", "device": {
            "installation_id": fixture_uuid("denial-second-device"),
            "name": "second-test-mac", "platform": "macos", "client_version": "0.0.1",
        },
    })
    assert response.status_code == 200
    path = f"/ws/v1/sessions/{session['session_id']}/audio"
    assert_handshake_denied(app_client, path, auth_headers(response.json()), 403, "FORBIDDEN")
    # The owner's legitimate handshake still works after the rejected attempt.
    with app_client.websocket_connect(path, headers=headers) as ws:
        assert ws.receive_json()["type"] == "ready"
        cancel_and_drain(ws)


def test_admin_web_credentials_cannot_upgrade_desktop_stream(app_client):
    tokens, headers, session = pending(app_client, "denial-admin-web")
    execute_sql("update users set role='admin' where id=$1", tokens["user"]["id"])
    response = app_client.post("/api/auth/login", json={
        "email": "denial-admin-web@example.test", "password": "SmokePass123",
        "client_type": "admin_web",
    })
    assert response.status_code == 200
    assert_handshake_denied(app_client, f"/ws/v1/sessions/{session['session_id']}/audio",
                            auth_headers(response.json()), 403, "FORBIDDEN")


def test_nonexistent_session_is_http404_without_identity_leak(app_client):
    tokens = register_and_login(app_client, email="denial-missing@example.test")
    assert_handshake_denied(app_client, f"/ws/v1/sessions/{uuid.uuid4()}/audio",
                            auth_headers(tokens), 404, "NOT_FOUND")


def test_terminal_session_is_http409_not_generic403(app_client):
    _, headers, session = pending(app_client, "denial-terminal")
    response = app_client.post(f"/api/v1/sessions/{session['session_id']}/cancel", headers=headers)
    assert response.status_code == 200
    assert_handshake_denied(app_client, f"/ws/v1/sessions/{session['session_id']}/audio",
                            headers, 409, "SESSION_STATE_CONFLICT")


@pytest.mark.parametrize("origin_kind", ["absent", "allowed"])
@pytest.mark.parametrize("violation", ["bad_sequence", "oversized_binary"])
def test_accepted_socket_keeps_protocol_error_event_and_4400_or_1009(app_client, origin_kind, violation):
    _, headers, session = pending(app_client, f"accepted-{origin_kind}-{violation}")
    if origin_kind == "allowed":
        app_client.app.state.config.security.ws_origins = ["http://127.0.0.1:17864"]
        headers = {**headers, "Origin": "http://127.0.0.1:17864"}
    with app_client.websocket_connect(f"/ws/v1/sessions/{session['session_id']}/audio", headers=headers) as ws:
        assert ws.receive_json()["type"] == "ready"
        if violation == "bad_sequence":
            ws.send_bytes(frame(1))
            code, expected_close = "AUDIO_PROTOCOL_ERROR", 4400
        else:
            ws.send_bytes(b"\x00" * 8009)
            code, expected_close = "MESSAGE_TOO_LARGE", 1009
        event = ws.receive_json()
        assert event["type"] == "error"
        assert event["error"]["code"] == code
        with pytest.raises(WebSocketDisconnect) as raised:
            ws.receive_json()
        assert not isinstance(raised.value, WebSocketDenialResponse)
        assert raised.value.code == expected_close
