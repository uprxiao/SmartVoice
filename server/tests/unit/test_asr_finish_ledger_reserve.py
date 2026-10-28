"""ASR bookkeeping/cancellation units, not original3/10 or MLX evidence.

The separate PG/ASGI integration keeps original finish10/total30 unchanged.
These short explicit deadlines isolate allocation and cancellation branches.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.clock import loop_time
from app.services.asr_core import AsrConfig, AsrEngine
from app.services.inference_context import CallScope


class Recorder:
    def __init__(self, hang_started=False):
        self.hang_started = hang_started
        self.start_entered, self.start_cancelled = asyncio.Event(), asyncio.Event()
        self.starts, self.outcomes = [], []
        self.finished_at = None

    async def started(self, scope, provider, model_id, purpose, processed_audio_samples=0):
        self.starts.append((scope, provider, model_id, purpose, processed_audio_samples))
        self.start_entered.set()
        if self.hang_started:
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.start_cancelled.set()
                raise
        return f"fixture-asr-call-{len(self.starts)}"

    async def finished(self, **outcome):
        await asyncio.sleep(.02)
        self.outcomes.append(outcome)
        self.finished_at = loop_time()


class PhysicalWorker:
    def __init__(self):
        self.process = SimpleNamespace(is_alive=lambda: True)
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.deadlines = []
        self.active_audio = None
        self.cancelled_at = None

    async def decode(self, args, deadline):
        self.deadlines.append(deadline)
        self.active_audio = args[0]
        self.entered.set()
        try:
            # This is the real worker's contract, without starting MLX/processes.
            async with asyncio.timeout_at(deadline):
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    self.cancelled_at = loop_time()
                    raise
            return {"words": [], "segments": [], "seconds": 0}
        finally:
            self.active_audio = None
            args = None

    async def stop(self):
        self.release.set()
        assert self.active_audio is None
        self.process = None


def pair(*, hang_started=False):
    engine = AsrEngine(AsrConfig(Path("/not-a-model/asr-finish-budget-fixture")))
    worker, recorder = PhysicalWorker(), Recorder(hang_started)
    engine.workers, engine.recorder = [worker], recorder
    engine.model_id = "fixture-not-a-physical-model"
    return engine, worker, recorder


def scope(index=1):
    return CallScope(f"fixture-user-{index}", f"fixture-session-{index}", 1)


async def until(predicate, seconds=.5):
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", ["asr_finish", "asr_decode"], ids=["finish", "partial"])
async def test_finish_timeout_releases_local_work_then_persists_before_original_deadline(purpose):
    engine, worker, recorder = pair()
    deadline = loop_time() + 1.25
    try:
        with pytest.raises(TimeoutError):
            await engine.decode(bytes(8000), "", purpose=purpose, deadline=deadline, scope=scope())
        assert worker.deadlines == [deadline - 1]
        assert len(recorder.starts) == len(recorder.outcomes) == 1
        assert worker.cancelled_at >= worker.deadlines[0]
        assert worker.active_audio is None
        assert worker.cancelled_at <= recorder.finished_at < deadline
        result = recorder.outcomes[0]
        assert result["status"] == "failed" and result["error_code"] == "ASR_TIMEOUT"
        assert result.get("prompt_tokens") is result.get("completion_tokens") is None
        assert recorder.starts[0][-1] == 4000
        assert recorder.starts[0][-2] == purpose
        assert engine.scheduler.snapshot() == {"running": 0, "high_waiting": 0, "low_waiting": 0}
    finally:
        worker.release.set()
        await engine.close()


@pytest.mark.asyncio
async def test_less_than_metadata_reserve_never_queues_starts_or_decodes():
    engine, worker, recorder = pair()
    try:
        with pytest.raises(TimeoutError):
            await engine.decode(bytes(8000), "", purpose="asr_finish", deadline=loop_time() + .9, scope=scope())
        assert recorder.starts == recorder.outcomes == worker.deadlines == []
        assert engine.scheduler.workers == []
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_started_timeout_never_dispatches_or_invents_terminal_call():
    engine, worker, recorder = pair(hang_started=True)
    deadline = loop_time() + 1.15
    try:
        with pytest.raises(TimeoutError):
            await engine.decode(bytes(8000), "", purpose="asr_finish", deadline=deadline, scope=scope())
        assert recorder.start_entered.is_set() and recorder.start_cancelled.is_set()
        assert len(recorder.starts) == 1 and recorder.outcomes == worker.deadlines == []
        assert loop_time() < deadline
        assert engine.scheduler.snapshot()["running"] == 0
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", ["asr_finish", "asr_decode"], ids=["finish", "partial"])
async def test_queue_wait_consumes_physical_budget_without_new_deadline_or_call(purpose):
    engine, worker, recorder = pair()
    first = second = None
    try:
        first = asyncio.create_task(engine.decode(bytes(8000), "", purpose=purpose,
            deadline=loop_time() + 2, scope=scope(1)))
        await asyncio.wait_for(worker.entered.wait(), .3)
        deadline = loop_time() + 1.1
        second = asyncio.create_task(engine.decode(bytes(8000), "", purpose=purpose,
            deadline=deadline, scope=scope(2)))
        owner = (scope(2).user_id, scope(2).session_id, 1)
        priority = "high" if purpose == "asr_finish" else "low"
        await until(lambda: owner in engine.scheduler.queues[priority])
        job = engine.scheduler.queues[priority][owner]
        assert job.deadline == job.wait_deadline == deadline
        await asyncio.sleep(.15)
        worker.release.set()
        await first
        with pytest.raises(TimeoutError):
            await second
        assert job.done.is_set() and job.operation is None
        assert len(recorder.starts) == len(recorder.outcomes) == len(worker.deadlines) == 1
        assert recorder.starts[0][0] == scope(1)
        assert recorder.starts[0][-2] == purpose
        assert engine.scheduler.snapshot() == {"running": 0, "high_waiting": 0, "low_waiting": 0}
    finally:
        worker.release.set()
        tasks = [task for task in (first, second) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await engine.close()


@pytest.mark.asyncio
async def test_explicit_cancel_persists_confirmed_local_cancel_to_frozen_recorder():
    engine, worker, recorder = pair()
    task = None
    deadline = loop_time() + 1.5
    try:
        task = asyncio.create_task(engine.decode(bytes(8000), "", purpose="asr_finish", deadline=deadline, scope=scope()))
        await asyncio.wait_for(worker.entered.wait(), .3)
        replacement = Recorder()
        engine.recorder = replacement
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert worker.active_audio is None
        assert len(recorder.outcomes) == 1 and replacement.starts == replacement.outcomes == []
        value = recorder.outcomes[0]
        assert value["status"] == "cancelled" and value["error_code"] == "CANCELLED"
        assert value.get("prompt_tokens") is value.get("completion_tokens") is None
        assert worker.cancelled_at <= recorder.finished_at < worker.deadlines[0] < deadline
        assert engine.scheduler.snapshot()["running"] == 0
    finally:
        worker.release.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await engine.close()


@pytest.mark.asyncio
async def test_worker_prepare_wait_also_cannot_spend_metadata_reserve():
    engine, worker, recorder = pair()
    async def preparing():
        await asyncio.Future()
    repair = asyncio.create_task(preparing())
    engine.repairs[0] = repair
    deadline = loop_time() + 1.15
    try:
        with pytest.raises(TimeoutError):
            await engine.decode(bytes(8000), "", purpose="asr_finish", deadline=deadline, scope=scope())
        assert not repair.done()  # Shared repair is shielded, not killed by a user's deadline.
        assert recorder.starts == recorder.outcomes == worker.deadlines == []
        assert loop_time() < deadline
        assert engine.scheduler.snapshot()["running"] == 0
    finally:
        await engine.close()
