"""Single-owner control flow with synthetic Process/Pipe, never OS/MLX proof.

Use actual DecodeWorker/_stop_owned/Engine/scheduler/provider.health. A thread
Event gates actual to_thread join. Real-OS integration remains primary evidence.
"""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from app.providers.asr.mlx_whisper import MlxWhisperProvider
from app.services import asr_core
from app.services.inference_context import CallScope


class SyntheticConnection:
    def __init__(self, marker="unused"):
        self.marker, self.sent, self.close_count = marker, [], 0

    def send(self, value):
        assert not self.close_count
        self.sent.append(value)

    def recv(self):
        assert not self.close_count
        return True, {"fixture_generation": self.marker}

    def close(self):
        self.close_count += 1


class SyntheticProcess:
    def __init__(self, *, alive=True, join_error=None, gate=True):
        self.loop = asyncio.get_running_loop()
        self.alive, self.started, self.closed = alive, alive, False
        self.start_count = self.terminate_count = self.kill_count = 0
        self.join_timeouts, self.join_error = [], join_error
        self.join_entered, self.join_finished = asyncio.Event(), asyncio.Event()
        self.release_join = threading.Event()
        if not gate:
            self.release_join.set()

    def start(self):
        assert not self.started and not self.closed
        self.start_count += 1
        self.started = self.alive = True

    def is_alive(self):
        if self.closed:
            raise ValueError("synthetic Process handle is closed")
        return self.alive

    def terminate(self):
        assert self.alive
        self.terminate_count += 1
        # This is a controlled handle, not signal delivery. Only the test's
        # explicit join gate confirms exit; no shutdown budget is slept here.

    def join(self, timeout):
        assert self.started
        self.join_timeouts.append(timeout)
        self.loop.call_soon_threadsafe(self.join_entered.set)
        try:
            if not self.release_join.wait(3):
                raise AssertionError("unit join gate was not released")
            if self.join_error is not None:
                raise self.join_error
            self.alive = False
        finally:
            self.loop.call_soon_threadsafe(self.join_finished.set)

    def kill(self):
        self.kill_count += 1
        raise AssertionError("synthetic unit gate must confirm exit at join")

    def close(self):
        assert not self.alive
        self.closed = True


class NeverCalledRecorder:
    def __init__(self):
        self.starts = self.finishes = 0

    async def started(self, *_args, **_kwargs):
        self.starts += 1
        raise AssertionError("quarantined worker must not start a model call")

    async def finished(self, **_kwargs):
        self.finishes += 1
        raise AssertionError("no physical call exists to finish")


def provider_for(worker):
    provider = MlxWhisperProvider(
        asr_core.AsrConfig(Path("/synthetic/unit-only/not-a-model"), prewarm=False),
        "synthetic-unit-model")
    provider.engine.model_id = "synthetic-unit-model"
    provider.engine.workers = [worker]
    return provider


async def bounded(awaitable):
    # Harness safety only, not a replacement for a product deadline.
    return await asyncio.wait_for(awaitable, 5)


@pytest.mark.asyncio
async def test_concurrent_stop_callers_delay_repeated_cancellation_until_owner_finishes():
    worker = asr_core.DecodeWorker()
    process, connection = SyntheticProcess(), SyntheticConnection()
    worker.process, worker.connection = process, connection
    provider = provider_for(worker)
    entered, exits = [asyncio.Event() for _ in range(3)], []

    async def call_stop(index):
        entered[index].set()
        try:
            await worker.stop()
        finally:
            exits.append((index, process.closed, connection.close_count))

    callers = [asyncio.create_task(call_stop(i)) for i in range(3)]
    owner = None
    try:
        await bounded(asyncio.gather(*(event.wait() for event in entered)))
        await bounded(process.join_entered.wait())
        owner = worker._stop_task
        assert owner is not None and not owner.done()
        assert not await provider.health()
        for caller in (callers[0], callers[0], callers[1], callers[0]):
            assert caller.cancel()
            await asyncio.sleep(0)  # Deliver this cancellation at the event gate.
            assert not caller.done()
            assert not owner.done() and owner.cancelling() == 0
            assert worker._stop_task is owner
            assert worker.process is process and worker.connection is connection
            assert not exits
        assert process.terminate_count == 1 and len(process.join_timeouts) == 1
        assert not process.closed and connection.close_count == 0
        process.release_join.set()
        results = await bounded(asyncio.gather(*callers, return_exceptions=True))
        assert isinstance(results[0], asyncio.CancelledError)
        assert isinstance(results[1], asyncio.CancelledError)
        assert results[2] is None
        assert callers[0].cancelled() and callers[1].cancelled()
        assert owner.done() and not owner.cancelled() and owner.result() is None
        assert len(exits) == 3 and all(closed and count == 1 for _, closed, count in exits)
        assert worker.process is worker.connection is None
        assert process.terminate_count == 1 and process.kill_count == 0
        assert connection.close_count == 1 and not worker.stop_incomplete
        assert not await provider.health()  # Successfully stopped is not ready.
    finally:
        process.release_join.set()
        await bounded(asyncio.gather(*callers, return_exceptions=True))
        if owner is not None:
            await bounded(asyncio.shield(owner))
        await bounded(process.join_finished.wait())
        await bounded(provider.close())


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["join_failure", "owner_cancelled"])
async def test_unconfirmed_stop_quarantines_start_engine_dispatch_and_health(monkeypatch, mode):
    worker = asr_core.DecodeWorker()
    error = RuntimeError("synthetic join failed") if mode == "join_failure" else None
    process, connection = SyntheticProcess(join_error=error), SyntheticConnection()
    worker.process, worker.connection = process, connection
    provider, recorder, context_requests = provider_for(worker), NeverCalledRecorder(), []
    provider.recorder = recorder

    def forbidden_context(method):
        context_requests.append(method)
        raise AssertionError("quarantined worker attempted a new spawn context")

    monkeypatch.setattr(asr_core.multiprocessing, "get_context", forbidden_context)
    caller = asyncio.create_task(worker.stop())
    expected = "synthetic join failed" if error else "stop was not confirmed"
    try:
        await bounded(process.join_entered.wait())
        owner = worker._stop_task
        assert owner is not None
        if mode == "join_failure":
            process.release_join.set()
        else:
            # Deliberately corrupt the private owner: failure isolation, not
            # normal caller cancellation or confirmed physical process exit.
            assert owner.cancel()
        with pytest.raises(RuntimeError, match=expected):
            await bounded(caller)
        assert worker._stop_task is owner and worker.stop_incomplete
        assert worker.process is process and worker.connection is connection
        assert process.alive and not process.closed and not connection.close_count
        assert not await provider.health()
        with pytest.raises(RuntimeError, match=expected):
            worker._start()
        scope = CallScope("00000000-0000-0000-0000-000000000001",
                          "00000000-0000-0000-0000-000000000002", 1)
        with pytest.raises(RuntimeError, match=expected):
            await bounded(provider.engine.decode(
                bytes(8), "", purpose="asr_finish", scope=scope,
                deadline=asyncio.get_running_loop().time() + 10))
        assert recorder.starts == recorder.finishes == 0
        assert not context_requests and not connection.sent
        assert not provider.engine.repairs
        assert provider.engine.scheduler.snapshot() == {
            "running": 0, "high_waiting": 0, "low_waiting": 0}
        assert worker._stop_task is owner
        with pytest.raises(RuntimeError, match=expected):
            await bounded(worker.stop())
        assert process.terminate_count == 1 and len(process.join_timeouts) == 1
        assert not await provider.health()
    finally:
        # Cancelled to_thread waiters do not stop threads. Release and wait for
        # this exact synthetic join before its fixture event loop is closed.
        process.release_join.set()
        await bounded(asyncio.gather(caller, return_exceptions=True))
        await bounded(process.join_finished.wait())
        with pytest.raises(RuntimeError, match=expected):
            await bounded(provider.close())
        assert worker.stop_incomplete


@pytest.mark.asyncio
async def test_successful_stop_allows_exactly_one_new_usable_generation(monkeypatch):
    worker = asr_core.DecodeWorker()
    provider = provider_for(worker)
    created, pipes = [], []

    class SyntheticSpawnContext:
        def Pipe(self):
            parent, child = SyntheticConnection(str(len(pipes) + 1)), SyntheticConnection()
            pipes.append((parent, child))
            return parent, child

        def Process(self, *, target, args, daemon):
            assert target is asr_core._worker_main and daemon is True
            assert args == (pipes[-1][1],)
            assert all(process.closed and not process.alive for process in created)
            process = SyntheticProcess(alive=False, gate=False)
            created.append(process)
            return process

    context = SyntheticSpawnContext()

    def get_context(method):
        assert method == "spawn"
        return context

    monkeypatch.setattr(asr_core.multiprocessing, "get_context", get_context)
    try:
        first_args = (bytes(4), "/synthetic", "zh", "first")
        assert await bounded(worker.decode(first_args, asyncio.get_running_loop().time() + 10)) == {
            "fixture_generation": "1"}
        first = worker.process
        assert first is created[0] and first.start_count == 1
        assert pipes[0][0].sent == [first_args] and pipes[0][1].close_count == 1
        assert await provider.health()
        await bounded(worker.stop())
        first_owner = worker._stop_task
        assert first_owner is not None and first_owner.done() and first_owner.result() is None
        assert first.closed and pipes[0][0].close_count == 1
        assert worker.process is worker.connection is None
        assert not await provider.health()
        second_args = (bytes(6), "/synthetic", "zh", "second")
        assert await bounded(worker.decode(second_args, asyncio.get_running_loop().time() + 10)) == {
            "fixture_generation": "2"}
        assert len(created) == 2 and worker.process is created[1]
        assert created[1] is not first and created[1].start_count == 1
        assert worker._stop_task is None and not worker.stop_incomplete
        assert pipes[1][0].sent == [second_args] and pipes[1][1].close_count == 1
        assert await provider.health()
        await bounded(worker.stop())
        assert worker._stop_task is not first_owner
        assert worker._stop_task.done() and worker._stop_task.result() is None
        assert all(process.closed and process.terminate_count == 1 and
                   len(process.join_timeouts) == 1 for process in created)
        assert all(parent.close_count == child.close_count == 1 for parent, child in pipes)
        assert worker.process is worker.connection is None
        assert not await provider.health()
    finally:
        await bounded(provider.close())
