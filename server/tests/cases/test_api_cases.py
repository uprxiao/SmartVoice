from __future__ import annotations

from tests.ids import fixture_uuid

import asyncio
import struct
import time
import uuid
from concurrent.futures import CancelledError as FutureCancelledError

import pytest
from starlette.websockets import WebSocketDisconnect
from tests.ws_assertions import assert_handshake_denied, cancel_and_drain

from app.services.capacity import CapacityError
from app.services.session_runtime import MAX_SAMPLES
from tests.conftest import auth_headers, register_and_login, execute_sql
from tests.cases.test_dict_cases import fetch_one, key, new_user
from tests.cases.test_learn_cases import set_classifier, set_text, wait_event
from tests.test_sessions import create_session, dictation, frame


def drain_until_close(ws):
    events = []
    try:
        while True:
            events.append(ws.receive_json())
    except WebSocketDisconnect as exc:
        return events, exc.code
    except (asyncio.CancelledError, FutureCancelledError):
        return events, None


def ws_session(app_client, headers, request_id):
    session = create_session(app_client, headers, request_id).json()
    return session, app_client.websocket_connect(
        f"/ws/v1/sessions/{session['session_id']}/audio", headers=headers
    )


def assert_error(events, code):
    assert any(
        event.get("type") == "error" and event["error"]["code"] == code
        for event in events
    ), events


def state_of(app_client, headers, session):
    return app_client.get(
        f"/api/v1/sessions/{session['session_id']}", headers=headers
    ).json()


def test_tc_api_01_sequence_gap(app_client):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    session, connection = ws_session(app_client, headers, "api01-session")
    with connection as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_bytes(frame(0))
        assert ws.receive_json()["type"] == "audio.ack"
        ws.send_bytes(frame(2))
        events, code = drain_until_close(ws)
    assert code == 4400
    assert_error(events, "AUDIO_PROTOCOL_ERROR")
    body = state_of(app_client, headers, session)
    assert body["state"] == "failed"
    assert body["result"] is None


@pytest.mark.parametrize(
    "payload",
    [
        struct.pack("<II", 0, 4000) + b"\x00" * (4000 * 2 - 2),
        struct.pack("<II", 0, 0),
        struct.pack("<II", 0, 4001) + b"\x00" * 8000,
    ],
)
def test_tc_api_02_malformed_frames(app_client, payload):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    session, connection = ws_session(app_client, headers, "api02-frame")
    with connection as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_bytes(payload)
        events, code = drain_until_close(ws)
    assert code == 4400
    assert_error(events, "AUDIO_PROTOCOL_ERROR")
    assert not any(event.get("type") == "audio.ack" for event in events)
    assert state_of(app_client, headers, session)["result"] is None


def test_tc_api_03_bad_end_metadata(app_client):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    session, connection = ws_session(app_client, headers, "api03-session")
    with connection as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_bytes(frame(0))
        ws.receive_json()
        ws.send_bytes(frame(1))
        ws.receive_json()
        ws.send_json({"type": "audio.end", "last_seq": 0, "total_samples": 8000})
        events, code = drain_until_close(ws)
    assert code == 4400
    assert_error(events, "AUDIO_PROTOCOL_ERROR")
    assert not any(event.get("type") == "audio.end_ack" for event in events)
    assert state_of(app_client, headers, session)["result"] is None


def test_tc_api_04_audio_limit(app_client, monkeypatch):
    monkeypatch.setattr("app.services.session_runtime.MAX_SAMPLES", 8001)
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    session, connection = ws_session(app_client, headers, "api04-session")
    with connection as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_bytes(frame(0))
        ws.receive_json()
        ws.send_bytes(frame(1))
        ws.receive_json()
        ws.send_bytes(frame(2, 2))
        events, code = drain_until_close(ws)
    assert code == 4400
    assert_error(events, "AUDIO_LIMIT_EXCEEDED")
    body = state_of(app_client, headers, session)
    assert body["state"] == "failed"
    assert body["result"] is None


def test_tc_api_05_audio_after_end(app_client):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    session, connection = ws_session(app_client, headers, "api05-session")
    with connection as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_bytes(frame(0))
        ws.receive_json()
        ws.send_json({"type": "audio.end", "last_seq": 0, "total_samples": 4000})
        while True:
            event = ws.receive_json()
            if event["type"] in ("audio.end_ack", "error"):
                break
        assert event["type"] == "audio.end_ack"
        ws.send_bytes(frame(1))
        events, code = drain_until_close(ws)
    assert code == 4400
    assert_error(events, "AUDIO_PROTOCOL_ERROR")
    body = state_of(app_client, headers, session)
    assert body["state"] in ("failed", "ready")
    if body["state"] == "ready":
        # A late illegal frame must not roll back an already committed result.
        assert body["result"] is not None
        assert body["current_result_id"] == body["result"]["result_id"]
    else:
        assert body["result"] is None


def test_tc_api_08_cancel_discards_late_result(app_client):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    session, connection = ws_session(app_client, headers, "api08-session")
    with connection as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_bytes(frame(0))
        ws.receive_json()
        ws.send_json({"type": "cancel"})
        while True:
            event = ws.receive_json()
            if event["type"] in ("cancelled", "error"):
                break
        assert event["type"] == "cancelled"
    time.sleep(0.3)
    body = state_of(app_client, headers, session)
    assert body["state"] == "cancelled"
    assert body["result"] is None
    assert body["current_result_id"] is None


def test_tc_api_11_repeat_and_changed_end(app_client):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    session, connection = ws_session(app_client, headers, "api11-session")
    with connection as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_bytes(frame(0))
        ws.receive_json()
        ws.send_json({"type": "audio.end", "last_seq": 0, "total_samples": 4000})
        first = ws.receive_json()
        assert first["type"] == "audio.end_ack"
        ws.send_json({"type": "audio.end", "last_seq": 0, "total_samples": 4000})
        second = ws.receive_json()
        assert second["type"] == "audio.end_ack"
        ws.send_json({"type": "audio.end", "last_seq": 0, "total_samples": 8000})
        events, code = drain_until_close(ws)
    assert code == 4400
    assert_error(events, "AUDIO_PROTOCOL_ERROR")


def test_tc_api_12_second_connection_and_foreign_user(app_client):
    u1 = new_user(app_client, "api12-u1@example.com", "api12-d1")
    u2 = new_user(app_client, "api12-u2@example.com", "api12-d2")
    h1, h2 = auth_headers(u1), auth_headers(u2)
    session, connection = ws_session(app_client, h1, "api12-session")
    path = f"/ws/v1/sessions/{session['session_id']}/audio"
    with connection as first:
        assert first.receive_json()["type"] == "ready"
        assert_handshake_denied(app_client, path, h1, 409, "SESSION_STATE_CONFLICT")
        assert_handshake_denied(app_client, path, h2, 404, "NOT_FOUND")
        # A rejected second/foreign handshake has not cancelled the first one.
        first.send_bytes(frame(0))
        assert first.receive_json()["type"] == "audio.ack"
        cancel_and_drain(first)


def test_tc_api_14_claim_guards(app_client):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    session, result1 = dictation_ready(app_client, headers, "api14-session")
    execute_sql(
        "update results set review_required=true where id=$1", result1["result_id"]
    )
    retry = app_client.post(
        f"/api/v1/sessions/{session['session_id']}/retry",
        json={"retry_kind": "refine"},
        headers={**headers, "Idempotency-Key": key()},
    )
    assert retry.status_code == 202
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        detail = state_of(app_client, headers, session)
        if detail["result"] and detail["result"]["attempt_no"] == 2:
            break
        time.sleep(0.05)
    result2 = detail["result"]
    stale = claim(app_client, headers, session, result1, hash_override=None)
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "STALE_RESULT"
    wrong_hash = claim(
        app_client, headers, session, result2, hash_override="0" * 64
    )
    assert wrong_hash.status_code == 409
    assert wrong_hash.json()["error"]["code"] == "RESULT_HASH_MISMATCH"
    execute_sql(
        "update results set review_required=true where id=$1", result2["result_id"]
    )
    unreviewed = claim(app_client, headers, session, result2)
    assert unreviewed.status_code == 409
    assert unreviewed.json()["error"]["code"] == "REVIEW_REQUIRED"
    reviewed = claim(app_client, headers, session, result2, review=True)
    assert reviewed.status_code == 201
    reserved_retry = app_client.post(
        f"/api/v1/sessions/{session['session_id']}/retry",
        json={"retry_kind": "refine"},
        headers={**headers, "Idempotency-Key": key()},
    )
    assert reserved_retry.status_code == 409
    assert reserved_retry.json()["error"]["details"]["reason"] == "insertion_reserved"


def dictation_ready(app_client, headers, request_id):
    session = create_session(app_client, headers, request_id).json()
    events = dictation(app_client, headers, session["session_id"])
    return session, events[-1]["result"]


def claim(app_client, headers, session, result, hash_override=None, review=False):
    return app_client.post(
        f"/api/v1/sessions/{session['session_id']}/insertions/claim",
        json={
            "result_id": result["result_id"],
            "insertion_id": key(),
            "text_hash": hash_override or result["text_hash"],
            "review_confirmed": review,
        },
        headers=headers,
    )


def test_tc_api_15_correction_position_guards(app_client):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    session, result = dictation_ready(app_client, headers, "api15-session")
    insertion = claim(app_client, headers, session, result)
    assert insertion.status_code == 201
    insertion_id = insertion.json()["insertion_id"]
    app_client.post(
        f"/api/v1/sessions/{session['session_id']}/insertions/{insertion_id}/receipt",
        json={"status": "inserted", "observable": True, "error_code": None},
        headers=headers,
    )
    text = result["refined_text"]
    offset = text.index("按钮")
    bad_left = app_client.post(
        "/api/v1/corrections",
        json={
            "session_id": session["session_id"],
            "insertion_id": insertion_id,
            "correction_id": key(),
            "offset": offset,
            "before": "按钮",
            "after": "按键",
            "left": "错误的左侧",
            "right": "",
        },
        headers=headers,
    )
    assert bad_left.status_code == 422
    bad_right = app_client.post(
        "/api/v1/corrections",
        json={
            "session_id": session["session_id"],
            "insertion_id": insertion_id,
            "correction_id": key(),
            "offset": offset,
            "before": "按钮",
            "after": "按键",
            "left": "",
            "right": "错误的右侧",
        },
        headers=headers,
    )
    assert bad_right.status_code == 422
    assert fetch_one("select count(*) from learning_events") == 0
    assert fetch_one("select count(*) from dictionary_terms") == 0


def test_tc_api_16_confirm_guards(app_client):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    session, result = dictation_ready(app_client, headers, "api16-session")
    insertion = claim(app_client, headers, session, result)
    insertion_id = insertion.json()["insertion_id"]
    app_client.post(
        f"/api/v1/sessions/{session['session_id']}/insertions/{insertion_id}/receipt",
        json={"status": "inserted", "observable": True, "error_code": None},
        headers=headers,
    )
    text = result["refined_text"]
    ignored = app_client.post(
        "/api/v1/corrections",
        json={
            "session_id": session["session_id"],
            "insertion_id": insertion_id,
            "correction_id": key(),
            "offset": text.index("一"),
            "before": "一",
            "after": "二",
        },
        headers=headers,
    )
    assert ignored.status_code == 202
    assert ignored.json()["status"] == "ignored"
    blocked = app_client.post(
        f"/api/v1/learning-events/{ignored.json()['event_id']}/confirm",
        headers={**headers, "Idempotency-Key": key()},
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "LEARNING_CONFLICT"
    missing = app_client.post(
        f"/api/v1/learning-events/{uuid.uuid4()}/confirm",
        headers={**headers, "Idempotency-Key": key()},
    )
    assert missing.status_code == 404


def test_tc_api_18_error_contract(app_client, monkeypatch):
    no_token = app_client.post(
        "/api/v1/sessions",
        json={"client_request_id": fixture_uuid("api18-token"), "learning_enabled": False},
        headers={"Idempotency-Key": fixture_uuid("api18-token")},
    )
    assert no_token.status_code == 401
    assert no_token.json()["error"]["code"] == "AUTH_INVALID"
    assert no_token.json()["error"]["request_id"]
    assert "Traceback" not in no_token.text

    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    bad_schema = app_client.post(
        "/api/v1/sessions",
        json={"client_request_id": "x"},
        headers={**headers, "Idempotency-Key": fixture_uuid("api18-schema")},
    )
    assert bad_schema.status_code == 422
    assert bad_schema.json()["error"]["code"] == "VALIDATION_ERROR"

    provider = app_client.app.state.sessions.asr_provider
    original = provider.open

    async def unavailable(options):
        raise RuntimeError("asr offline")

    monkeypatch.setattr(provider, "open", unavailable)
    unavailable_response = app_client.post(
        "/api/v1/sessions",
        json={"client_request_id": fixture_uuid("api18-asr"), "learning_enabled": False},
        headers={**headers, "Idempotency-Key": fixture_uuid("api18-asr")},
    )
    assert unavailable_response.status_code == 503
    assert unavailable_response.json()["error"]["code"] == "ASR_UNAVAILABLE"
    monkeypatch.setattr(provider, "open", original)

    async def capacity_full(db, *, locked_capacity=None):
        assert locked_capacity is not None and locked_capacity.id == 1
        raise CapacityError("active_limit")

    monkeypatch.setattr(app_client.app.state.sessions.capacity, "admit", capacity_full)
    full = app_client.post(
        "/api/v1/sessions",
        json={"client_request_id": fixture_uuid("api18-capacity"), "learning_enabled": False},
        headers={**headers, "Idempotency-Key": fixture_uuid("api18-capacity")},
    )
    assert full.status_code == 429
    assert full.json()["error"]["code"] == "CAPACITY_EXCEEDED"
    monkeypatch.setattr(
        app_client.app.state.sessions.capacity, "admit", original_admit(app_client)
    )

    invalid = app_client.post(
        "/api/auth/login",
        content='{"email":',
        headers={"Content-Type": "application/json"},
    )
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "INVALID_JSON"

    wrong_type = app_client.post(
        "/api/auth/login",
        content='{"email":"x@example.com"}',
        headers={"Content-Type": "text/plain"},
    )
    assert wrong_type.status_code == 415
    assert wrong_type.json()["error"]["code"] == "UNSUPPORTED_MEDIA_TYPE"


def original_admit(app_client):
    from app.services.capacity import CapacityService

    bound = CapacityService.admit
    return bound.__get__(app_client.app.state.sessions.capacity, CapacityService)


def test_tc_api_21_terminal_result_is_not_rolled_back(app_client):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    session, connection = ws_session(app_client, headers, "api21-session")
    with connection as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_bytes(frame(0))
        ws.receive_json()
        ws.send_json({"type": "audio.end", "last_seq": 0, "total_samples": 4000})
        while True:
            event = ws.receive_json()
            if event["type"] == "result.ready":
                break
        try:
            ws.send_bytes(struct.pack("<II", 9, 4000) + b"\x00" * 8000)
        except Exception:
            pass
    body = state_of(app_client, headers, session)
    assert body["state"] == "ready"
    assert body["result"] is not None
    assert body["current_result_id"] == body["result"]["result_id"]


def test_tc_api_06_create_idempotency_and_cross_user(app_client):
    u1 = new_user(app_client, "api06-u1@example.com", "api06-d1")
    u2 = new_user(app_client, "api06-u2@example.com", "api06-d2")
    h1 = auth_headers(u1)
    h2 = auth_headers(u2)
    payload = {"client_request_id": fixture_uuid("api06-session"), "learning_enabled": False}
    first = app_client.post(
        "/api/v1/sessions",
        json=payload,
        headers={**h1, "Idempotency-Key": fixture_uuid("api06-session")},
    )
    assert first.status_code == 201
    replay = app_client.post(
        "/api/v1/sessions",
        json=payload,
        headers={**h1, "Idempotency-Key": fixture_uuid("api06-session")},
    )
    assert replay.status_code == 200
    assert replay.json()["session_id"] == first.json()["session_id"]
    assert replay.json()["replayed"] is True
    conflict = app_client.post(
        "/api/v1/sessions",
        json={"client_request_id": fixture_uuid("api06-session"), "learning_enabled": True},
        headers={**h1, "Idempotency-Key": fixture_uuid("api06-session")},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    other = app_client.post(
        "/api/v1/sessions",
        json=payload,
        headers={**h2, "Idempotency-Key": fixture_uuid("api06-session")},
    )
    assert other.status_code == 201
    assert other.json()["session_id"] != first.json()["session_id"]


def test_tc_api_07_receipt_idempotency_and_missing_claim(app_client):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    session, result = dictation_ready(app_client, headers, "api07-session")
    missing = app_client.post(
        f"/api/v1/sessions/{session['session_id']}/insertions/{uuid.uuid4()}/receipt",
        json={"status": "inserted", "observable": True, "error_code": None},
        headers=headers,
    )
    assert missing.status_code == 404
    insertion_id = key()
    claimed = app_client.post(
        f"/api/v1/sessions/{session['session_id']}/insertions/claim",
        json={
            "result_id": result["result_id"],
            "insertion_id": insertion_id,
            "text_hash": result["text_hash"],
            "review_confirmed": False,
        },
        headers=headers,
    )
    assert claimed.status_code == 201
    first = app_client.post(
        f"/api/v1/sessions/{session['session_id']}/insertions/{insertion_id}/receipt",
        json={"status": "inserted", "observable": True, "error_code": None},
        headers=headers,
    )
    assert first.status_code == 200
    replay = app_client.post(
        f"/api/v1/sessions/{session['session_id']}/insertions/{insertion_id}/receipt",
        json={"status": "inserted", "observable": True, "error_code": None},
        headers=headers,
    )
    assert replay.status_code == 200
    assert replay.json()["status"] == "inserted"
    changed = app_client.post(
        f"/api/v1/sessions/{session['session_id']}/insertions/{insertion_id}/receipt",
        json={"status": "failed", "observable": False, "error_code": "ZERO_WRITE"},
        headers=headers,
    )
    assert changed.status_code == 409
    assert changed.json()["error"]["code"] == "INSERTION_STATE_CONFLICT"
    assert fetch_one("select count(*) from insertions") == 1


def test_tc_api_09_body_size_boundaries(app_client):
    base = '{"email":"api09@example.com","password":"Password123"'
    pad_length = 65536 - len(base) - len(',"pad":""}')
    exact = base + ',"pad":"' + "a" * pad_length + '"}'
    assert len(exact.encode()) == 65536
    inside = app_client.post(
        "/api/auth/login",
        content=exact,
        headers={"Content-Type": "application/json"},
    )
    assert inside.status_code == 422, inside.text
    assert inside.json()["error"]["code"] == "VALIDATION_ERROR"
    too_large = app_client.post(
        "/api/auth/login",
        content=exact + " ",
        headers={"Content-Type": "application/json"},
    )
    assert too_large.status_code == 413
    assert too_large.json()["error"]["code"] == "REQUEST_TOO_LARGE"

    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    header = b"term,aliases\n"
    rows = [f"T{i}," + "a" * 1000 for i in range(1000)]
    exact_csv = header + "\n".join(rows).encode() + b"\n"
    exact_csv += b" " * (1024 * 1024 - len(exact_csv))
    assert len(exact_csv) == 1024 * 1024
    accepted = app_client.post(
        "/api/v1/dictionary/import",
        files={"file": ("edge.csv", exact_csv, "text/csv")},
        headers={**headers, "Idempotency-Key": key()},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["invalid"] == 1000
    assert accepted.json()["created"] == 0
    over_csv = exact_csv + b"a"
    rejected = app_client.post(
        "/api/v1/dictionary/import",
        files={"file": ("over.csv", over_csv, "text/csv")},
        headers={**headers, "Idempotency-Key": key()},
    )
    assert rejected.status_code == 413
    assert rejected.json()["error"]["code"] == "REQUEST_TOO_LARGE"


def test_tc_api_10_cors_contract(app_client):
    allowed = app_client.options(
        "/api/auth/login",
        headers={
            "Origin": "http://127.0.0.1:17864",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert allowed.status_code == 200
    assert (
        allowed.headers.get("access-control-allow-origin")
        == "http://127.0.0.1:17864"
    )
    assert not allowed.headers.get("access-control-allow-credentials")
    denied = app_client.options(
        "/api/auth/login",
        headers={
            "Origin": "http://evil.example",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert not denied.headers.get("access-control-allow-origin")


def test_tc_api_13_audio_complete_survives_lost_ack(app_client):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    session, connection = ws_session(app_client, headers, "api13-session")
    try:
        with connection as ws:
            assert ws.receive_json()["type"] == "ready"
            ws.send_bytes(frame(0))
            ws.receive_json()
            ws.send_json({"type": "audio.end", "last_seq": 0, "total_samples": 4000})
            ws.receive_json()  # end_ack, then deliberately drop the connection
    except (asyncio.CancelledError, FutureCancelledError):
        pass
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        body = state_of(app_client, headers, session)
        if body["state"] == "ready":
            break
        time.sleep(0.05)
    assert body["state"] == "ready"
    assert body["audio_complete"] is True
    assert body["result"] is not None


def test_tc_api_17_idempotent_resource_replays(app_client):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    create_key = key()
    created = app_client.post(
        "/api/v1/dictionary/terms",
        json={"term": "Kubernetes", "aliases": ["k8s"], "scope": "default"},
        headers={**headers, "Idempotency-Key": create_key},
    )
    assert created.status_code == 201
    replay = app_client.post(
        "/api/v1/dictionary/terms",
        json={"term": "Kubernetes", "aliases": ["k8s"], "scope": "default"},
        headers={**headers, "Idempotency-Key": create_key},
    )
    assert replay.status_code == 200
    assert replay.json()["id"] == created.json()["id"]
    assert replay.json()["replayed"] is True
    conflict = app_client.post(
        "/api/v1/dictionary/terms",
        json={"term": "Docker", "aliases": [], "scope": "default"},
        headers={**headers, "Idempotency-Key": create_key},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    session, result = dictation_ready(app_client, headers, "api17-session")
    insertion_id = key()
    claim_payload = {
        "result_id": result["result_id"],
        "insertion_id": insertion_id,
        "text_hash": result["text_hash"],
        "review_confirmed": False,
    }
    first_claim = app_client.post(
        f"/api/v1/sessions/{session['session_id']}/insertions/claim",
        json=claim_payload,
        headers=headers,
    )
    assert first_claim.status_code == 201
    replay_claim = app_client.post(
        f"/api/v1/sessions/{session['session_id']}/insertions/claim",
        json=claim_payload,
        headers=headers,
    )
    assert replay_claim.status_code == 200
    assert replay_claim.json()["insertion_id"] == insertion_id
    assert replay_claim.json()["replayed"] is True


def test_tc_api_22_oversized_control_and_unknown_fields(app_client):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    session, connection = ws_session(app_client, headers, "api22-session")
    with connection as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "x", "pad": "a" * 4097})
        events, code = drain_until_close(ws)
    assert code == 1009
    assert_error(events, "MESSAGE_TOO_LARGE")

    second, connection = ws_session(app_client, headers, "api22-json")
    with connection as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_text("{not-json")
        events, code = drain_until_close(ws)
    assert code == 4400
    assert_error(events, "AUDIO_PROTOCOL_ERROR")

    third, connection = ws_session(app_client, headers, "api22-unknown")
    with connection as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "unknown", "value": 1})
        events, code = drain_until_close(ws)
    assert code == 4400
    assert_error(events, "AUDIO_PROTOCOL_ERROR")
