"""TC-CAP-16: real PG and authenticated HTTP/WS, synthetic providers only.

All four terminal outcomes and the capacity rejection use production routes.
Only completed telemetry timestamps/durations are fixed for hand calculation.
This does not measure ASR/LLM latency, throughput, or Desktop/native behavior.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest
from sqlalchemy import select

from app.models import SessionAttempt, SessionResult, VoiceSession
from app.providers.asr.base import ProviderError
from app.providers.asr.mock import MockAsrProvider
from app.providers.llm.base import LlmResponse
from app.services.inference_scheduler import WeightedScheduler
from tests.conftest import auth_headers, register_and_login
from tests.test_admin import login_admin
from tests.test_sessions import create_session, dictation, frame


PRIVATE_TEXT = "CAP16_PRIVATE_BODY_请保留这个虚构标记。"


@pytest.fixture
def metrics_client(monkeypatch, request):
    monkeypatch.setenv("SVI_ASR_PROVIDER", "mock")
    monkeypatch.setenv("SVI_LLM_PROVIDER", "mock")
    client = request.getfixturevalue("app_client")
    provider = client.app.state.asr
    assert isinstance(provider, MockAsrProvider)
    # Stock Mock ASR exposes no scheduler, so runtime=null is honest unknown.
    # This explicit telemetry fixture supplies a real idle scheduler; its
    # snapshot is not mocked, nor are API aggregation or capacity counts.
    async def scheduler():
        return WeightedScheduler(1, 20, 20)

    idle = client.portal.call(scheduler)
    monkeypatch.setattr(provider, "scheduler", idle, raising=False)
    monkeypatch.setattr(provider, "text", PRIVATE_TEXT)
    try:
        yield client
    finally:
        client.portal.call(idle.close)


def _runtime(client, headers):
    response = client.get("/api/admin/capacity", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["runtime"] is not None, "fixture scheduler telemetry must be available"
    serialized = json.dumps(body, ensure_ascii=False)
    assert PRIVATE_TEXT not in serialized
    for field in ("raw_text", "refined_text", "asr_hints", "llm_hints", "password", "secret", "token"):
        assert f'"{field}"' not in serialized
    assert body["runtime"]["window_seconds"] == 300
    return body["runtime"]


def _assert_window(runtime, *, samples, failures, fallbacks, rejected, p50, p95):
    assert {key: runtime[key] for key in (
        "sample_count", "failed_count", "fallback_count", "rejected_count",
        "stop_to_result_p50_ms", "stop_to_result_p95_ms",
    )} == {
        "sample_count": samples, "failed_count": failures,
        "fallback_count": fallbacks, "rejected_count": rejected,
        "stop_to_result_p50_ms": p50, "stop_to_result_p95_ms": p95,
    }


def _fix_completed_telemetry(client, owner, session_id, ended, elapsed_ms):
    """Fix only telemetry on real terminal records, never state/admission facts."""
    async def write():
        async with client.app.state.db.session_factory() as db:
            session = await db.scalar(select(VoiceSession).where(
                VoiceSession.user_id == owner, VoiceSession.id == session_id))
            attempt = await db.scalar(select(SessionAttempt).where(
                SessionAttempt.user_id == owner, SessionAttempt.session_id == session_id,
                SessionAttempt.attempt_no == 1))
            assert session is not None and attempt is not None
            assert session.audio_complete and session.received_samples == 8000
            assert session.current_attempt_no == 1 and attempt.kind == "initial"
            assert session.state == attempt.state and attempt.state in ("ready", "failed")
            accepted = ended - dt.timedelta(milliseconds=elapsed_ms)
            started = accepted - dt.timedelta(milliseconds=500)
            session.created_at = attempt.started_at = started
            session.end_accepted_at = accepted
            session.first_terminal_at = attempt.ended_at = ended
            attempt.stop_to_result_ms = elapsed_ms
            result = await db.scalar(select(SessionResult).where(
                SessionResult.user_id == owner, SessionResult.session_id == session_id,
                SessionResult.attempt_no == 1))
            if attempt.state == "ready":
                assert result is not None and session.current_result_id == result.id
                result.created_at, result.stop_to_result_ms = ended, elapsed_ms
            else:
                assert result is None and session.current_result_id is None
            await db.commit()

    client.portal.call(write)


def test_tc_cap_16_fixed_window_counts_percentiles_failure_and_empty(metrics_client, monkeypatch):
    client = metrics_client
    admin_headers = auth_headers(login_admin(client))
    author = register_and_login(client, email="cap16-author@example.invalid")
    author_headers = auth_headers(author)
    contender_headers = auth_headers(register_and_login(client, email="cap16-contender@example.invalid"))
    # Three registrations stay below the actual per-IP five/hour limit.
    _assert_window(_runtime(client, admin_headers), samples=0, failures=0,
                   fallbacks=0, rejected=0, p50=None, p95=None)

    session_ids = []
    inner = client.app.state.llm.inner
    original_generate = inner.generate
    fallback_enabled = False
    fallback_calls = []

    async def controlled_generate(request, deadline):
        if fallback_enabled:
            fallback_calls.append(request.purpose)
            return LlmResponse(parsed=None, provider=inner.provider_id, model_id=inner.model_id,
                finish_reason="error", error=ProviderError("unavailable", "CAP16 controlled LLM failure"))
        return await original_generate(request, deadline)

    monkeypatch.setattr(inner, "generate", controlled_generate)
    for index in range(4):
        created = create_session(client, author_headers, f"cap16-initial-{index}")
        assert created.status_code == 201
        sid = created.json()["session_id"]
        session_ids.append(sid)
        fallback_enabled = index == 2
        if index == 3:
            asr = client.app.state.sessions.runtimes[sid].asr

            async def controlled_asr_failure(total_samples, deadline):
                assert total_samples == 8000
                raise ProviderError("ASR_FAILED", "CAP16 controlled complete-audio failure")

            monkeypatch.setattr(asr, "finish", controlled_asr_failure)
        events = dictation(client, author_headers, sid)
        terminal = events[-1]
        detail = client.get(f"/api/v1/sessions/{sid}", headers=author_headers)
        assert detail.status_code == 200
        saved = detail.json()
        assert saved["audio_complete"] is True and saved["received_samples"] == 8000
        assert saved["current_attempt_no"] == 1
        if index == 3:
            assert terminal["type"] == "error" and terminal["error"]["code"] == "ASR_FAILED"
            assert saved["state"] == "failed" and saved["result"] is None
        else:
            assert terminal["type"] == "result.ready" and saved["state"] == "ready"
            assert saved["result"]["review_required"] is (index == 2)
            assert saved["result"]["raw_text"] == PRIVATE_TEXT
            if index == 2:
                assert saved["result"]["review_reason"] == "llm_unavailable"
                assert saved["result"]["refined_text"] == PRIVATE_TEXT
    assert fallback_calls == ["refine"]

    anchor = dt.datetime.now(dt.timezone.utc)
    from app.api import admin as admin_module
    # Only the capacity read's UTC window is frozen. No event-loop, watchdog,
    # retention or authentication clock is replaced or advanced.
    monkeypatch.setattr(admin_module, "utcnow", lambda: anchor)
    for sid, age, latency in zip(session_ids, (90, 60, 30, 301), (100, 400, 900, 0)):
        _fix_completed_telemetry(client, author["user"]["id"], sid,
                                 anchor - dt.timedelta(seconds=age), latency)
    # Failed initial is outside the fixed 300-second window. Finite positive
    # control: sorted [100,400,900], nearest-rank ceil(.5*3)=2 / ceil(.95*3)=3.
    finite = _runtime(client, admin_headers)
    _assert_window(finite, samples=3, failures=0, fallbacks=1,
                   rejected=0, p50=400, p95=900)

    # Bring the same actual failure into the observation window; no new result,
    # terminal state, sample or capacity counter is manufactured.
    _fix_completed_telemetry(client, author["user"]["id"], session_ids[3],
                             anchor - dt.timedelta(seconds=10), 0)
    view = client.get("/api/admin/capacity", headers=admin_headers)
    assert view.status_code == 200
    changed = client.patch("/api/admin/capacity", headers=admin_headers,
        json={"version": view.json()["config"]["version"], "max_active_sessions": 1})
    assert changed.status_code == 200
    holder = create_session(client, author_headers, "cap16-real-capacity-holder")
    assert holder.status_code == 201
    holder_id = holder.json()["session_id"]
    with client.websocket_connect(f"/ws/v1/sessions/{holder_id}/audio", headers=author_headers) as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_bytes(frame(0))
        ack = ws.receive_json()
        assert ack["type"] == "audio.ack" and ack["total_samples"] == 4000
        try:
            rejected = create_session(client, contender_headers, "cap16-capacity-rejected")
            assert rejected.status_code == 429
            assert rejected.json()["error"]["code"] == "CAPACITY_EXCEEDED"
            measured = _runtime(client, admin_headers)
            assert measured["active_sessions"] == 1 and measured["available_sessions"] == 0
            # Sorted [100,400,900,+infinity]; ranks 2 and 4. Failure must not be
            # dropped or recorded as zero; failed_count distinguishes null from
            # the empty-window null checked above.
            _assert_window(measured, samples=4, failures=1, fallbacks=1,
                           rejected=1, p50=400, p95=None)
        finally:
            cancelled = client.post(f"/api/v1/sessions/{holder_id}/cancel", headers=author_headers)
            assert cancelled.status_code == 200 and cancelled.json()["state"] == "cancelled"
    after = _runtime(client, admin_headers)
    assert after["active_sessions"] == 0 and after["available_sessions"] == 1
    # The real cancelled/incomplete holder must not become a fifth sample.
    _assert_window(after, samples=4, failures=1, fallbacks=1,
                   rejected=1, p50=400, p95=None)
