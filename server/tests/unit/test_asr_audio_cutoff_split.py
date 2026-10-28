"""Short deadline/ownership controls only; NOT natural 600s, PG or OS evidence.

Use actual MlxAsrSession / AsrEngine / WeightedScheduler. Only worker and scalar
recorder are synthetic. Original normal-budget tests remain unchanged.
"""
from __future__ import annotations

import asyncio
import inspect
import time
from types import SimpleNamespace

import pytest

from app.core.clock import loop_time
from app.providers.asr.base import AsrOpenOptions, ProviderError
from app.providers.asr.mlx_whisper import MlxAsrSession
from app.services.session_runtime import RuntimeSession, SessionManager
from tests.unit.test_asr_finish_ledger_reserve import pair, scope, until


def split_kwargs(engine, eligibility, operation):
    # Permit the root's tests-only OLD-source negative control to reach the
    # old deadline allocation instead of failing on an unknown kwarg.
    result = {"deadline": eligibility}
    if "outcome_deadline" in inspect.signature(engine.decode).parameters:
        result["outcome_deadline"] = operation
    return result


@pytest.mark.asyncio
async def test_retry_adapter_last_subsecond_can_reach_physical_without_spending_reserve():
    engine, worker, recorder = pair()
    now = loop_time()
    eligibility, operation = now + .4, now + 10
    options = AsrOpenOptions(scope().session_id, 1, user_id=scope().user_id)
    if hasattr(options, "outcome_deadline"):
        options.outcome_deadline = operation
        options.eligibility_deadline = eligibility
    stream = MlxAsrSession(engine, engine.config, options)
    worker.release.set()
    outcome, error = None, None
    try:
        await stream.feed(bytes(8000), 0)
        try:
            outcome = await stream.finish(4000, eligibility)
        except (TimeoutError, ProviderError) as exc:
            error = type(exc).__name__
        assert worker.entered.is_set(), {"physical_started": False, "error_type": error}
        assert worker.deadlines == [eligibility]
        assert outcome is not None and outcome.processed_samples == 4000
        assert len(recorder.starts) == len(recorder.outcomes) == 1
        assert recorder.starts[0][-1] == 4000
        assert recorder.outcomes[0]["status"] == "succeeded"
        assert recorder.finished_at < eligibility < operation
        assert not stream.buffer and worker.active_audio is None
    finally:
        await stream.cancel()
        await engine.close()


@pytest.mark.asyncio
async def test_expiring_audio_still_records_actual_physical_timeout_inside_original_budget():
    engine, worker, recorder = pair()
    now = loop_time()
    eligibility, operation = now + .4, now + 10
    try:
        with pytest.raises(TimeoutError):
            await engine.decode(bytes(8000), "", purpose="asr_finish", scope=scope(),
                                **split_kwargs(engine, eligibility, operation))
        assert worker.entered.is_set()
        assert worker.deadlines == [eligibility]
        assert worker.cancelled_at >= eligibility
        assert len(recorder.starts) == len(recorder.outcomes) == 1
        value = recorder.outcomes[0]
        assert (value["status"], value["error_code"]) in {
            ("failed", "ASR_TIMEOUT"), ("cancelled", "CANCELLED")}
        assert recorder.finished_at < operation
        assert value.get("prompt_tokens") is value.get("completion_tokens") is None
        assert engine.scheduler.snapshot() == {"running": 0, "high_waiting": 0, "low_waiting": 0}
    finally:
        worker.release.set()
        await engine.close()


@pytest.mark.asyncio
async def test_expired_queued_retry_never_creates_call_or_physical_input():
    engine, worker, recorder = pair()
    first = second = None
    try:
        first = asyncio.create_task(engine.decode(bytes(8000), "", purpose="asr_finish",
            deadline=loop_time() + 10, scope=scope(1)))
        await asyncio.wait_for(worker.entered.wait(), .5)
        now = loop_time()
        second = asyncio.create_task(engine.decode(bytes(8000), "", purpose="asr_finish", scope=scope(2),
            **split_kwargs(engine, now + .4, now + 10)))
        owner = (scope(2).user_id, scope(2).session_id, 1)
        await until(lambda: owner in engine.scheduler.queues["high"], seconds=.2)
        with pytest.raises(TimeoutError):
            await second
        assert len(recorder.starts) == len(worker.deadlines) == 1
        assert recorder.starts[0][0] == scope(1)
        assert owner not in engine.scheduler.queues["high"]
        worker.release.set()
        await first
    finally:
        worker.release.set()
        tasks = [task for task in (first, second) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await engine.close()


@pytest.mark.asyncio
async def test_cancel_zeros_parent_pcm_while_physical_cleanup_still_owns_permit():
    engine, worker, recorder = pair()
    cleanup_entered, release_cleanup, probe_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    held = {}

    async def physical(args, deadline):
        held["audio"] = args[0]
        worker.entered.set()
        try:
            async with asyncio.timeout_at(deadline):
                await asyncio.Future()
        finally:
            cleanup_entered.set()
            await release_cleanup.wait()
            args = None

    async def probe(_slot):
        probe_started.set()
        return "probe"

    worker.decode = physical
    owner = (scope().user_id, scope().session_id, 1)
    task = cancel = queued = None
    try:
        task = asyncio.create_task(engine.decode(bytes(8000), "", owner=owner,
            purpose="asr_finish", deadline=loop_time() + 10, scope=scope()))
        await asyncio.wait_for(worker.entered.wait(), .5)
        assert len(held["audio"]) == 8000
        cancel = asyncio.create_task(engine.cancel(owner))
        await asyncio.wait_for(cleanup_entered.wait(), .5)
        assert len(held["audio"]) == 0
        assert not cancel.done() and engine.scheduler.snapshot()["running"] == 1
        now = loop_time()
        queued = asyncio.create_task(engine.scheduler.submit("probe", "high", probe,
            deadline=now + 10, wait_deadline=now + 2))
        await until(lambda: "probe" in engine.scheduler.queues["high"])
        assert not probe_started.is_set()
        release_cleanup.set()
        await cancel
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await queued == "probe" and probe_started.is_set()
        assert len(recorder.outcomes) == 1
    finally:
        release_cleanup.set()
        for pending in (task, cancel, queued):
            if pending is not None and not pending.done():
                pending.cancel()
        await asyncio.gather(*(x for x in (task, cancel, queued) if x is not None), return_exceptions=True)
        await engine.close()


@pytest.mark.asyncio
async def test_absolute_lease_fences_active_pcm_before_blocked_metadata_and_cancel_join(monkeypatch):
    # Candidate-only lease control: deliberately short explicit unit deadline.
    # No config TTL is changed and this is NOT the F-AUDIO-TTL 600s acceptance.
    metadata_entered, cancel_entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    failure_entered, processing_cancelled = asyncio.Event(), asyncio.Event()
    buffer = bytearray(b"\x01\x00" * 4000)

    async def cancel():
        buffer.clear()
        cancel_entered.set()
        await release.wait()

    async def persist(runtime, status):
        assert status == "expired"
        metadata_entered.set()
        await release.wait()

    async def fail(runtime, code, *, retain_audio=True):
        assert code == "ASR_TIMEOUT" and retain_audio is False
        failure_entered.set()
        await release.wait()

    async def processing():
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            processing_cancelled.set()
            await release.wait()
            raise

    manager = SessionManager(SimpleNamespace(), SimpleNamespace(), None, None, None)
    runtime = RuntimeSession("session", "user", "device", 1, state="finalizing",
        audio=bytearray(buffer), asr=SimpleNamespace(cancel=cancel), ended=True)
    runtime.task = asyncio.create_task(processing())
    original_audio = runtime.audio
    runtime.retry_expires_at = time.time() + .1
    manager.runtimes[runtime.session_id] = runtime
    monkeypatch.setattr(manager, "_persist_cache_status", persist)
    monkeypatch.setattr(manager, "_fail", fail)
    lease = manager._arm_audio_lease(runtime, runtime.retry_expires_at)
    try:
        assert len(original_audio) == len(buffer) == 8000
        async with asyncio.timeout(1):
            await metadata_entered.wait()
            await cancel_entered.wait()
            await failure_entered.wait()
            await processing_cancelled.wait()
        assert not original_audio and not buffer
        assert runtime.cancelled and runtime.state == "failed" and runtime.error_code == "ASR_TIMEOUT"
        assert lease.status == "expired" and lease.owner is None and lease.timer is None
        assert not release.is_set() and manager.background_tasks
        assert manager._audio_expired(runtime)
        cancellations = runtime.task.cancelling()
        manager._fence_runtime(runtime, "ASR_TIMEOUT")
        assert runtime.task.cancelling() == cancellations == 1
        assert not runtime.task.done()
    finally:
        release.set()
        if lease.timer is not None:
            lease.timer.cancel()
        await manager.close()
        await asyncio.gather(runtime.task, return_exceptions=True)
