"""Actual partial adapter/engine; synthetic existing worker, not natural TTL."""
from __future__ import annotations

import pytest

from app.core.clock import loop_time
from app.providers.asr.base import AsrOpenOptions, ProviderError
from app.providers.asr.mlx_whisper import MlxAsrSession
from tests.unit.test_asr_finish_ledger_reserve import pair, scope


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["audio_near_attempt_far", "attempt_near_audio_far"])
async def test_partial_keeps_metadata_reserve_inside_original_attempt(boundary):
    engine, worker, recorder = pair()
    now = loop_time()
    operation = now + (10 if boundary == "audio_near_attempt_far" else .9)
    audio_expiry = now + (.4 if boundary == "audio_near_attempt_far" else 600)
    eligibility = min(operation, audio_expiry)
    options = AsrOpenOptions(scope().session_id, 1, user_id=scope().user_id,
        outcome_deadline=operation, eligibility_deadline=eligibility)
    stream = MlxAsrSession(engine, engine.config, options)
    worker.release.set()
    error = None
    try:
        assert engine.decode_timeout_ms == 3000 and engine.config.step_seconds == 1
        # Feed the real adapter; call one actual window without waiting for the
        # periodic loop. No buffer/state/clock edits, no mocked engine result.
        await stream.feed(bytes(32_000), 0)
        assert loop_time() < eligibility
        try:
            await stream._decode_window()
        except ProviderError as exc:
            error = exc
        if boundary == "audio_near_attempt_far":
            assert error is None
            assert worker.entered.is_set() and worker.deadlines == [eligibility]
            assert len(recorder.starts) == len(recorder.outcomes) == 1
            assert recorder.starts[0][0] == scope()
            assert recorder.starts[0][-2:] == ("asr_decode", 16_000)
            assert recorder.outcomes[0]["status"] == "succeeded"
            assert recorder.finished_at < eligibility < operation
            assert stream.decoded_samples == 16_000
            partial = stream.queue.get_nowait()
            assert partial.decoded_samples == partial.received_samples == 16_000
        else:
            # Remaining original B < 1s: no queue/ledger/physical dispatch, even
            # though the audio D is distant. A fresh partial B must not replace it.
            assert not worker.entered.is_set()
            assert worker.deadlines == recorder.starts == recorder.outcomes == []
            assert engine.scheduler.workers == []
            assert error is not None and error.code == "resource_exhausted"
            assert isinstance(error.__cause__, TimeoutError)
            assert stream.decoded_samples == 0 and stream.queue.empty()
        assert worker.active_audio is None
        assert engine.scheduler.snapshot() == {"running": 0, "high_waiting": 0, "low_waiting": 0}
    finally:
        worker.release.set()
        try:
            await stream.cancel()
        finally:
            await engine.close()
    assert not stream.buffer and worker.active_audio is None
