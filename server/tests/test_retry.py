from __future__ import annotations

import struct
import time
import uuid

from app.providers.asr.base import ProviderError
from tests.conftest import auth_headers, execute_sql, register_and_login
from tests.test_sessions import create_session, dictation


def key() -> str:
    return str(uuid.uuid4())


def wait_state(app_client, headers, session_id, states=("ready", "failed", "empty"), timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = app_client.get(f"/api/v1/sessions/{session_id}", headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        if body["state"] in states:
            return body
        time.sleep(0.1)
    raise AssertionError("session did not reach a terminal state")


def test_refine_retry_creates_new_attempt(app_client):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    session = create_session(app_client, headers, "refine-retry").json()
    events = dictation(app_client, headers, session["session_id"])
    result = events[-1]["result"]
    execute_sql(
        "UPDATE results SET review_required=true, review_reason='llm_unavailable' WHERE id=$1",
        result["result_id"],
    )
    current = app_client.get(
        f"/api/v1/sessions/{session['session_id']}", headers=headers
    ).json()
    assert current["retry_kind"] == "refine"

    response = app_client.post(
        f"/api/v1/sessions/{session['session_id']}/retry",
        json={"retry_kind": "refine"},
        headers={**headers, "Idempotency-Key": key()},
    )
    assert response.status_code == 202, response.text
    assert response.json()["state"] == "refining"
    final = wait_state(app_client, headers, session["session_id"])
    assert final["state"] == "ready"
    assert final["current_attempt_no"] == 2
    assert final["result"]["attempt_kind"] == "refine"
    assert final["result"]["review_required"] is False
    assert final["result"]["result_id"] != result["result_id"]


def test_refine_retry_is_idempotent(app_client):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    session = create_session(app_client, headers, "retry-idem").json()
    events = dictation(app_client, headers, session["session_id"])
    execute_sql(
        "UPDATE results SET review_required=true, review_reason='llm_unavailable' WHERE id=$1",
        events[-1]["result"]["result_id"],
    )
    idem = key()
    first = app_client.post(
        f"/api/v1/sessions/{session['session_id']}/retry",
        json={"retry_kind": "refine"},
        headers={**headers, "Idempotency-Key": idem},
    )
    assert first.status_code == 202
    assert first.json()["replayed"] is False
    wait_state(app_client, headers, session["session_id"])
    replay = app_client.post(
        f"/api/v1/sessions/{session['session_id']}/retry",
        json={"retry_kind": "refine"},
        headers={**headers, "Idempotency-Key": idem},
    )
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["current_attempt_no"] == 2


def test_refine_retry_unavailable_without_review(app_client):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    session = create_session(app_client, headers, "no-review").json()
    dictation(app_client, headers, session["session_id"])
    response = app_client.post(
        f"/api/v1/sessions/{session['session_id']}/retry",
        json={"retry_kind": "refine"},
        headers={**headers, "Idempotency-Key": key()},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RETRY_UNAVAILABLE"
    assert response.json()["error"]["details"]["reason"] == "not_needed"


def test_asr_retry_uses_retained_audio(app_client, monkeypatch):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    manager = app_client.app.state.sessions
    provider = manager.asr_provider
    original_open = provider.open
    opened, finished, feeds, retry_deadlines = [], [], {}, {}

    async def observed_open(options):
        asr = await original_open(options)
        attempt = options.attempt_no
        opened.append(attempt)
        feeds[attempt] = []
        retry_deadlines[attempt] = manager.runtimes[options.session_id].retry_expires_at
        original_feed, original_finish = asr.feed, asr.finish

        async def feed(pcm, start_sample):
            feeds[attempt].append((start_sample, bytes(pcm)))
            await original_feed(pcm, start_sample)

        async def finish(total_samples, deadline):
            finished.append((attempt, total_samples))
            if attempt == 1:
                raise ProviderError("ASR_FAILED", "injected first complete attempt failure")
            return await original_finish(total_samples, deadline)

        monkeypatch.setattr(asr, "feed", feed)
        monkeypatch.setattr(asr, "finish", finish)
        return asr

    monkeypatch.setattr(provider, "open", observed_open)
    session = create_session(app_client, headers, "asr-retry").json()
    session_id = session["session_id"]
    first_runtime = manager.runtimes[session_id]
    original_audio = first_runtime.audio
    pcm = b"\x01\x00\xfe\xff" * 4000  # 8000 deterministic, nonzero PCM samples.
    with app_client.websocket_connect(f"/ws/v1/sessions/{session_id}/audio", headers=headers) as ws:
        assert ws.receive_json()["type"] == "ready"
        for seq in range(2):
            ws.send_bytes(struct.pack("<II", seq, 4000) + pcm[seq * 8000:(seq + 1) * 8000])
            ack = ws.receive_json()
            assert ack["type"] == "audio.ack"
            assert ack["seq"] == seq
            assert ack["total_samples"] == (seq + 1) * 4000
        ws.send_json({"type": "audio.end", "last_seq": 1, "total_samples": 8000})
        while True:
            event = ws.receive_json()
            if event["type"] in ("error", "result.ready", "session.empty", "cancelled"):
                break
        assert event["type"] == "error"
        assert event["error"]["code"] == "ASR_FAILED"

    current = app_client.get(f"/api/v1/sessions/{session_id}", headers=headers).json()
    assert current["state"] == "failed"
    assert current["audio_complete"] is True
    assert current["received_samples"] == 8000
    assert current["retry_kind"] == "asr"
    assert current["retry_expires_at"] is not None
    assert bytes(original_audio) == pcm
    initial_expiry = first_runtime.retry_expires_at
    assert initial_expiry is not None and 0 < initial_expiry - time.time() <= 600

    retry_key = key()
    response = app_client.post(
        f"/api/v1/sessions/{session_id}/retry",
        json={"retry_kind": "asr"},
        headers={**headers, "Idempotency-Key": retry_key},
    )
    assert response.status_code == 202, response.text
    assert response.json()["replayed"] is False
    final = wait_state(app_client, headers, session_id)
    assert final["state"] == "ready", final
    assert final["session_id"] == session_id
    assert final["current_attempt_no"] == 2
    assert final["result"]["attempt_no"] == 2
    assert final["result"]["attempt_kind"] == "asr"
    assert final["received_samples"] == 8000
    assert retry_deadlines[2] == initial_expiry, "retry extended first-failure audio TTL"
    assert feeds[1] == feeds[2] == [(0, pcm[:8000]), (4000, pcm[8000:])]
    assert finished == [(1, 8000), (2, 8000)]
    # The transferred original bytearray must be cleared on retry success,
    # even when its RuntimeSession has already been retired from the map.
    assert original_audio == b""
    assert first_runtime.audio == b""
    replay = app_client.post(
        f"/api/v1/sessions/{session_id}/retry",
        json={"retry_kind": "asr"},
        headers={**headers, "Idempotency-Key": retry_key},
    )
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["current_attempt_no"] == 2
    assert replay.json()["result"]["result_id"] == final["result"]["result_id"]
    assert opened == [1, 2], "idempotency replay started another provider attempt"


def test_asr_retry_expired_audio_returns_409(app_client):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    session = create_session(app_client, headers, "asr-expired").json()
    session_id = session["session_id"]
    runtime = app_client.app.state.sessions.runtimes[session_id]
    runtime.audio.extend(b"\x00\x00" * 8000)
    runtime.samples = 8000
    execute_sql(
        "UPDATE sessions SET state='failed', error_code='ASR_FAILED', "
        "first_asr_failed_at=now() - interval '20 minutes', first_terminal_at=now(), "
        "received_samples=8000, audio_complete=true WHERE id=$1",
        session_id,
    )
    response = app_client.post(
        f"/api/v1/sessions/{session_id}/retry",
        json={"retry_kind": "asr"},
        headers={**headers, "Idempotency-Key": key()},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RETRY_UNAVAILABLE"
    assert response.json()["error"]["details"]["reason"] == "audio_expired"
