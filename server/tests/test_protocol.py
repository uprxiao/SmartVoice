from __future__ import annotations

from tests.ids import fixture_uuid

import asyncio
import struct
import time
from concurrent.futures import CancelledError as FutureCancelledError

import pytest
from starlette.websockets import WebSocketDisconnect
from tests.ws_assertions import assert_handshake_denied, cancel_and_drain

from tests.conftest import auth_headers, execute_sql, register_and_login
from tests.test_sessions import create_session, dictation, frame


def test_message_too_large_closes_with_1009(app_client):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    session = create_session(app_client, headers, "too-large").json()
    with app_client.websocket_connect(
        f"/ws/v1/sessions/{session['session_id']}/audio", headers=headers
    ) as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_bytes(b"\x00" * 8009)
        event = ws.receive_json()
        while event["type"] != "error":
            event = ws.receive_json()
        assert event["error"]["code"] == "MESSAGE_TOO_LARGE"


def test_short_frame_must_be_final(app_client):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    session = create_session(app_client, headers, "short-tail").json()
    with app_client.websocket_connect(
        f"/ws/v1/sessions/{session['session_id']}/audio", headers=headers
    ) as ws:
        ws.receive_json()
        ws.send_bytes(frame(0, 123))
        assert ws.receive_json()["type"] == "audio.ack"
        ws.send_bytes(frame(1, 4000))
        event = ws.receive_json()
        while event["type"] != "error":
            event = ws.receive_json()
        assert event["error"]["code"] == "AUDIO_PROTOCOL_ERROR"


def test_duplicate_end_only_reacks(app_client):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    session = create_session(app_client, headers, "duplicate-end").json()
    try:
        with app_client.websocket_connect(
            f"/ws/v1/sessions/{session['session_id']}/audio", headers=headers
        ) as ws:
            ws.receive_json()
            ws.send_bytes(frame(0))
            ws.receive_json()
            ws.send_json({"type": "audio.end", "last_seq": 0, "total_samples": 4000})
            while True:
                event = ws.receive_json()
                if event["type"] == "audio.end_ack":
                    break
    except (asyncio.CancelledError, FutureCancelledError):
        # The server closes the socket after the terminal event; TestClient
        # surfaces that as a cancelled portal future on exit.
        pass


def test_second_connection_is_rejected(app_client):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    session = create_session(app_client, headers, "second-connection").json()
    path = f"/ws/v1/sessions/{session['session_id']}/audio"
    with app_client.websocket_connect(path, headers=headers) as first:
        assert first.receive_json()["type"] == "ready"
        assert_handshake_denied(app_client, path, headers, 409, "SESSION_STATE_CONFLICT")
        cancel_and_drain(first)


def test_ws_origin_must_be_allowed(app_client):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    session = create_session(app_client, headers, "bad-origin").json()
    response = assert_handshake_denied(
        app_client,
        f"/ws/v1/sessions/{session['session_id']}/audio",
        {**headers, "Origin": "http://evil.example", "X-Request-Id": "origin-denial-test"},
        403,
        "FORBIDDEN",
    )
    assert response.headers["x-request-id"] == "origin-denial-test"


def test_idle_connection_fails(app_client):
    app_client.app.state.config.sessions.audio_idle_timeout_ms = 500
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    session = create_session(app_client, headers, "idle-close").json()
    with app_client.websocket_connect(
        f"/ws/v1/sessions/{session['session_id']}/audio", headers=headers
    ) as ws:
        ws.receive_json()
        deadline = time.monotonic() + 4
        event = ws.receive_json()
        while event["type"] != "error" and time.monotonic() < deadline:
            event = ws.receive_json()
        assert event["error"]["code"] == "AUDIO_IDLE_TIMEOUT"


def test_create_idempotency_conflict_on_different_payload(app_client):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    first = app_client.post(
        "/api/v1/sessions",
        json={"client_request_id": fixture_uuid("idem-conflict"), "learning_enabled": True},
        headers={**headers, "Idempotency-Key": fixture_uuid("idem-conflict")},
    )
    assert first.status_code == 201
    conflict = app_client.post(
        "/api/v1/sessions",
        json={"client_request_id": fixture_uuid("idem-conflict"), "learning_enabled": False},
        headers={**headers, "Idempotency-Key": fixture_uuid("idem-conflict")},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_invalid_json_returns_400(app_client):
    response = app_client.post(
        "/api/auth/login",
        content='{"email":',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_JSON"


def test_retention_read_barrier(app_client):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    session = create_session(app_client, headers, "retention-read").json()
    events = dictation(app_client, headers, session["session_id"])
    result = events[-1]["result"]

    settings = app_client.get("/api/v1/settings", headers=headers).json()
    assert app_client.patch(
        "/api/v1/settings",
        json={"version": settings["version"], "history_retention_days": 7},
        headers=headers,
    ).status_code == 200
    execute_sql(
        "UPDATE sessions SET first_terminal_at=now() - interval '8 days' WHERE id=$1",
        session["session_id"],
    )
    fetched = app_client.get(
        f"/api/v1/sessions/{session['session_id']}", headers=headers
    ).json()
    assert fetched["content_expired"] is True
    assert fetched["result"] is None
    history = app_client.get("/api/v1/history", headers=headers).json()
    assert history["items"] == []
    claim = app_client.post(
        f"/api/v1/sessions/{session['session_id']}/insertions/claim",
        json={
            "result_id": result["result_id"],
            "insertion_id": fixture_uuid("retention-insertion"),
            "text_hash": result["text_hash"],
            "review_confirmed": False,
        },
        headers=headers,
    )
    assert claim.status_code == 410


def test_restart_recovery_marks_active_sessions(app_client):
    from app.main import _recover_interrupted_sessions

    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    session = create_session(app_client, headers, "restart-recovery").json()
    app_client.portal.call(_recover_interrupted_sessions, app_client.app.state.db)
    fetched = app_client.get(
        f"/api/v1/sessions/{session['session_id']}", headers=headers
    ).json()
    assert fetched["state"] == "failed"
    assert fetched["error_code"] == "SERVER_RESTARTED"
    assert fetched["retry_kind"] == "none"
    assert fetched["audio_complete"] is False
    assert fetched["retry_unavailable_reason"] == "server_restarted"
    rejected = app_client.post(
        f"/api/v1/sessions/{session['session_id']}/retry",
        headers=headers | {"Idempotency-Key": fixture_uuid("restart-recovery-retry")},
        json={"retry_kind": "asr"},
    )
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "RETRY_UNAVAILABLE"
    assert rejected.json()["error"]["details"]["reason"] == fetched["retry_unavailable_reason"]
    assert app_client.get(
        f"/api/v1/sessions/{session['session_id']}", headers=headers
    ).json() == fetched
