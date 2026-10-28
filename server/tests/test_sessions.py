from __future__ import annotations

from tests.ids import fixture_uuid

import struct

import pytest
from starlette.websockets import WebSocketDisconnect
from tests.ws_assertions import assert_handshake_denied

from tests.conftest import auth_headers, register_and_login


def create_session(client, headers, request_id="request-0001"):
    response = client.post(
        "/api/v1/sessions",
        json={"client_request_id": fixture_uuid(request_id), "learning_enabled": True},
        headers={**headers, "Idempotency-Key": fixture_uuid(request_id)},
    )
    return response


def frame(seq: int, samples: int = 4000) -> bytes:
    return struct.pack("<II", seq, samples) + b"\x00\x00" * samples


def dictation(client, headers, session_id, chunks=2, end_samples=None):
    events = []
    with client.websocket_connect(
        f"/ws/v1/sessions/{session_id}/audio", headers=headers
    ) as ws:
        events.append(ws.receive_json())
        for seq in range(chunks):
            ws.send_bytes(frame(seq))
        total = chunks * 4000
        for seq in range(chunks):
            events.append(ws.receive_json())
        ws.send_json(
            {
                "type": "audio.end",
                "last_seq": chunks - 1,
                "total_samples": total if end_samples is None else end_samples,
            }
        )
        while True:
            event = ws.receive_json()
            events.append(event)
            if event["type"] in ("result.ready", "error", "session.empty", "cancelled"):
                break
    return events


def test_create_and_get_session(app_client):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    response = create_session(app_client, headers)
    assert response.status_code == 201, response.text
    session = response.json()
    assert session["state"] == "created"
    assert session["mode"] == "dictation"
    fetched = app_client.get(f"/api/v1/sessions/{session['session_id']}", headers=headers)
    assert fetched.status_code == 200
    assert fetched.json()["session_id"] == session["session_id"]


def test_create_is_idempotent(app_client):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    first = create_session(app_client, headers, "same-request")
    second = create_session(app_client, headers, "same-request")
    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["session_id"] == second.json()["session_id"]


def test_second_active_session_conflicts(app_client):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    assert create_session(app_client, headers, "request-a").status_code == 201
    second = create_session(app_client, headers, "request-b")
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "ACTIVE_SESSION_EXISTS"


def test_session_requires_desktop_client(app_client):
    from tests.conftest import execute_sql

    register_and_login(app_client)
    # Promote a regular account to admin for the admin web flow.
    execute_sql("UPDATE users SET role='admin' WHERE email=$1", "user@example.com")
    response = app_client.post(
        "/api/auth/login",
        json={
            "email": "user@example.com",
            "password": "SmokePass123",
            "client_type": "admin_web",
        },
    )
    assert response.status_code == 200
    headers = auth_headers(response.json())
    created = app_client.post(
        "/api/v1/sessions",
        json={"client_request_id": fixture_uuid("admin-request")},
        headers=headers,
    )
    assert created.status_code == 403


def test_dictation_round_trip(app_client):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    session = create_session(app_client, headers, "round-trip").json()
    events = dictation(app_client, headers, session["session_id"])
    types = [event["type"] for event in events]
    assert types[0] == "ready"
    assert types[-1] == "result.ready", types
    assert "asr.final" in types
    result = events[-1]["result"]
    assert result["raw_text"]
    assert result["refined_text"]

    fetched = app_client.get(
        f"/api/v1/sessions/{session['session_id']}", headers=headers
    ).json()
    assert fetched["state"] == "ready"
    assert fetched["audio_complete"] is True
    assert fetched["result"]["result_id"] == result["result_id"]


def test_empty_audio_ends_with_empty_state(app_client):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    session = create_session(app_client, headers, "empty-audio").json()
    with app_client.websocket_connect(
        f"/ws/v1/sessions/{session['session_id']}/audio", headers=headers
    ) as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "audio.end", "last_seq": -1, "total_samples": 0})
        event = ws.receive_json()
        while event["type"] != "session.empty":
            event = ws.receive_json()
    fetched = app_client.get(
        f"/api/v1/sessions/{session['session_id']}", headers=headers
    ).json()
    assert fetched["state"] == "empty"


def test_out_of_order_frame_is_rejected(app_client):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    session = create_session(app_client, headers, "invalid-seq").json()
    with app_client.websocket_connect(
        f"/ws/v1/sessions/{session['session_id']}/audio", headers=headers
    ) as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_bytes(frame(3))
        event = ws.receive_json()
        while event["type"] != "error":
            event = ws.receive_json()
        assert event["error"]["code"] == "AUDIO_PROTOCOL_ERROR"
    fetched = app_client.get(
        f"/api/v1/sessions/{session['session_id']}", headers=headers
    ).json()
    assert fetched["state"] == "failed"


def test_end_sample_mismatch_is_rejected(app_client):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    session = create_session(app_client, headers, "invalid-end").json()
    with app_client.websocket_connect(
        f"/ws/v1/sessions/{session['session_id']}/audio", headers=headers
    ) as ws:
        ws.receive_json()
        ws.send_bytes(frame(0))
        ws.receive_json()
        ws.send_json({"type": "audio.end", "last_seq": 0, "total_samples": 1})
        event = ws.receive_json()
        while event["type"] != "error":
            event = ws.receive_json()
        assert event["error"]["code"] == "AUDIO_PROTOCOL_ERROR"


def test_cancel_session(app_client):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    session = create_session(app_client, headers, "cancel-me").json()
    response = app_client.post(
        f"/api/v1/sessions/{session['session_id']}/cancel", headers=headers
    )
    assert response.status_code == 200
    assert response.json()["state"] == "cancelled"
    again = app_client.post(
        f"/api/v1/sessions/{session['session_id']}/cancel", headers=headers
    )
    assert again.status_code == 200
    assert again.json()["state"] == "cancelled"


def test_websocket_requires_auth(app_client):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    session = create_session(app_client, headers, "no-auth-ws").json()
    assert_handshake_denied(
        app_client,
        f"/ws/v1/sessions/{session['session_id']}/audio",
        {},
        401,
        "AUTH_INVALID",
    )


def test_session_isolation_between_users(app_client):
    first = register_and_login(app_client)
    second = register_and_login(app_client, email="second@example.com")
    session = create_session(app_client, auth_headers(first), "isolated").json()
    denied = app_client.get(
        f"/api/v1/sessions/{session['session_id']}", headers=auth_headers(second)
    )
    assert denied.status_code == 404


def test_capacity_limit_returns_429(app_client):
    from tests.conftest import execute_sql

    first = register_and_login(app_client)
    second = register_and_login(app_client, email="second@example.com")
    execute_sql("UPDATE runtime_capacity SET max_active_sessions=1 WHERE id=1")
    assert (
        create_session(app_client, auth_headers(first), "capacity-a").status_code == 201
    )
    response = create_session(app_client, auth_headers(second), "capacity-b")
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "CAPACITY_EXCEEDED"
