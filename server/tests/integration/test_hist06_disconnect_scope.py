"""Controlled TestClient disconnect/cancel ordering; real PG, Mock ASR.

The A arm deliberately retains the current expected-to-fail product oracle.
B is a diagnostic positive control, not a replacement for HIST06 or TCP proof.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import CancelledError as FutureCancelledError, Future
import json
import time

import pytest
from sqlalchemy import select

from app.models import VoiceSession
from tests.conftest import auth_headers, register_and_login
from tests.test_sessions import create_session, frame


@pytest.mark.parametrize("close_order", ["A_scope_cancel", "B_disconnect_completed"])
def test_hist06_disconnect_cancellation_order(app_client, monkeypatch, record_property, close_order):
    tokens = register_and_login(app_client, email=f"hist06-{close_order.lower()}@example.com")
    headers = auth_headers(tokens)
    response = create_session(app_client, headers, f"hist06-disconnect-{close_order}")
    assert response.status_code == 201
    sid = response.json()["session_id"]
    manager = app_client.app.state.sessions
    runtime = manager.runtimes[sid]
    original_audio = runtime.audio
    original_asr = runtime.asr

    async def make_events():
        return asyncio.Event(), asyncio.Event(), asyncio.Event(), asyncio.Event()

    entered, release, handler_done, fail_finished = app_client.portal.call(make_events)
    counters = {"interrupted_entries": 0, "gate_cancelled": 0, "delegated": 0}
    original_fail, original_handle = manager._fail, manager.handle_ws

    async def gated_fail(owner, code, *, retain_audio=True):
        if owner is runtime and code == "AUDIO_INTERRUPTED":
            counters["interrupted_entries"] += 1
            entered.set()
            try:
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    counters["gate_cancelled"] += 1
                    raise
                counters["delegated"] += 1
                return await original_fail(owner, code, retain_audio=retain_audio)
            finally:
                fail_finished.set()
        return await original_fail(owner, code, retain_audio=retain_audio)

    async def observed_handle(*args, **kwargs):
        try:
            return await original_handle(*args, **kwargs)
        finally:
            handler_done.set()

    monkeypatch.setattr(manager, "_fail", gated_fail)
    monkeypatch.setattr(manager, "handle_ws", observed_handle)

    async def observation(deadline):
        # This portal task is outside the WebSocket's cancel scope. One total
        # 2s observation guard; no idle wait, HTTP polling, clock/TTL changes.
        async with asyncio.timeout_at(deadline):
            if close_order == "B_disconnect_completed":
                await entered.wait()
                release.set()
            await handler_done.wait()
            if entered.is_set():
                await fail_finished.wait()
            async with manager.db.session_factory() as db:
                row = (await db.execute(select(
                    VoiceSession.state, VoiceSession.error_code,
                    VoiceSession.audio_complete, VoiceSession.received_samples,
                    VoiceSession.current_attempt_no, VoiceSession.current_result_id,
                ).where(VoiceSession.user_id == runtime.user_id,
                        VoiceSession.id == sid))).one()
                stored = {"state": row.state, "error_code": row.error_code,
                          "audio_complete": row.audio_complete,
                          "received_samples": row.received_samples,
                          "attempt_no": row.current_attempt_no,
                          "has_result": row.current_result_id is not None}
            return {"stored": stored, "runtime_state": runtime.state,
                    "runtime_error_code": runtime.error_code,
                    "original_pcm_bytes": len(original_audio),
                    "current_pcm_bytes": len(runtime.audio),
                    "same_pcm_object": runtime.audio is original_audio,
                    "runtime_connected": runtime.connected,
                    "mock_asr_cancelled": original_asr.cancelled,
                    "handler_completed": handler_done.is_set(),
                    "fail_finished": fail_finished.is_set(), **counters}

    async def start_clock():
        return asyncio.get_running_loop().time() + 2.0

    observer = None
    exit_started = False
    exit_future_cancelled = False
    try:
        try:
            with app_client.websocket_connect(f"/ws/v1/sessions/{sid}/audio", headers=headers) as ws:
                assert ws.receive_json()["type"] == "ready"
                # Two legal 4000-sample chunks = 8000 samples / 16000 PCM bytes.
                for seq in (0, 1):
                    ws.send_bytes(frame(seq, 4000))
                    ack = ws.receive_json()
                    assert ack["type"] == "audio.ack" and ack["seq"] == seq
                assert len(original_audio) == len(runtime.audio) == 16000
                started = time.monotonic()
                deadline = app_client.portal.call(start_clock)
                if close_order == "B_disconnect_completed":
                    observer = app_client.portal.start_task_soon(observation, deadline)
                    ws.close()
                    observed = observer.result(timeout=max(0.0, 2.0 - (time.monotonic() - started)))
                # No test-body operation follows this marker. Only __exit__
                # may emit the narrowly recognized portal future cancellation.
                exit_started = True
        except FutureCancelledError as exc:
            if (close_order != "A_scope_cancel" or not exit_started
                    or type(exc) is not FutureCancelledError):
                raise
            codes = set()
            traceback = exc.__traceback__
            while traceback is not None:
                codes.add(traceback.tb_frame.f_code)
                traceback = traceback.tb_next
            if (type(ws).__exit__.__code__ not in codes
                    or Future.result.__code__ not in codes):
                raise
            # Diagnostic harness cancellation only. All product assertions
            # below still run and still fail if terminal cleanup was lost.
            exit_future_cancelled = True
        if close_order == "A_scope_cancel":
            # Release before observing: a production-owned failure task must
            # not remain blocked by the diagnostic gate after the socket exits.
            app_client.portal.call(release.set)
            observer = app_client.portal.start_task_soon(observation, deadline)
            observed = observer.result(timeout=max(0.0, 2.0 - (time.monotonic() - started)))
        observed["elapsed_seconds"] = time.monotonic() - started
        observed["arm"] = close_order
        observed["exit_future_cancelled"] = exit_future_cancelled
        record_property("hist06_disconnect_observation", json.dumps(observed, sort_keys=True))
        assert observed["elapsed_seconds"] <= 2.0, observed
        assert observed["handler_completed"] and not observed["runtime_connected"], observed
        if observed["interrupted_entries"]:
            assert observed["fail_finished"], observed
        assert observed["stored"]["state"] == "failed", observed
        assert observed["stored"]["error_code"] == "AUDIO_INTERRUPTED", observed
        assert observed["stored"]["audio_complete"] is False, observed
        assert observed["stored"]["received_samples"] == 8000, observed
        assert observed["stored"]["attempt_no"] == 1 and not observed["stored"]["has_result"], observed
        assert observed["runtime_state"] == "failed", observed
        assert observed["runtime_error_code"] == "AUDIO_INTERRUPTED", observed
        assert observed["original_pcm_bytes"] == observed["current_pcm_bytes"] == 0, observed
        assert observed["mock_asr_cancelled"] is True, observed
        if close_order == "B_disconnect_completed":
            assert observed["interrupted_entries"] == observed["delegated"] == 1, observed
            assert observed["gate_cancelled"] == 0, observed
    finally:
        # Gates cannot outlive the test, including assertion/observation errors.
        app_client.portal.call(release.set)
        if observer is not None and not observer.done():
            observer.cancel()
