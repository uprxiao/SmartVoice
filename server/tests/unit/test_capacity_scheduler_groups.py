"""ASR N=20/50/100 and LLM N=50/100 production scheduler wiring; physical compute is stubbed.

No DB fixture, model preparation, subprocess, sockets or wall-clock monkeypatch.
These tests are control-plane evidence only, never capacity/latency acceptance.
"""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
import uuid

import pytest

from app.core.clock import loop_time
from app.core.config import LlmConfig
from app.providers.asr.base import AsrOpenOptions
from app.providers.asr.mlx_whisper import MlxAsrSession
from app.providers.llm.base import LlmRequest, LlmResponse
from app.providers.llm.scheduled import ScheduledLlmProvider
from app.services.asr_core import AsrConfig, AsrEngine
from app.services.inference_context import CallScope, call_scope, current_scope
from app.services.inference_scheduler import SchedulerBusy


def scope_for(index, *, classify=False):
    def identity(kind):
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"svi-cap-scheduler/{index}/{kind}"))
    return CallScope(identity("user"), identity("session"), 1,
                     identity("learning") if classify else None)


def spawn(scope, awaitable):
    with call_scope(scope):
        return asyncio.create_task(awaitable)


async def until(predicate, seconds=2):
    # A coordination guard only. Product deadlines and queue waits are untouched.
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(0)


def empty(scheduler):
    return scheduler.snapshot() == {"running": 0, "high_waiting": 0, "low_waiting": 0}


class PhysicalAsrStub:
    """The ONLY ASR replacement: the physical worker boundary, not the engine.

    The alive sentinel skips model warm-up. The cleanup gate retains execution
    ownership; active_audio aliases the engine's independently cleared parent PCM.
    It does not prove actual MLX process termination or memory reclamation.
    """
    def __init__(self, expected, blocker):
        self.process = object()
        self.expected, self.blocker = expected, blocker
        self.calls = []
        self.active_audio = None
        self.cleanup_entered, self.release_cleanup = asyncio.Event(), asyncio.Event()
        self.running, self.peak = 0, 0
        self.stopped = False

    async def decode(self, args, deadline):
        scope = current_scope.get()
        assert scope in self.expected
        audio, _model_path, language, prompt = args
        expected_audio, expected_prompt, label = self.expected[scope]
        assert audio == expected_audio and prompt == expected_prompt and language == "auto"
        assert loop_time() < deadline
        self.calls.append((scope, hashlib.sha256(audio).hexdigest(), prompt, deadline))
        self.running += 1
        self.peak = max(self.peak, self.running)
        self.active_audio = audio
        try:
            if scope == self.blocker:
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    self.cleanup_entered.set()
                    async with asyncio.timeout(2):
                        await self.release_cleanup.wait()
                    raise
            return {"words": [(0.0, 0.1, label)], "segments": [0.1], "seconds": 0.0}
        finally:
            self.active_audio = None
            self.running -= 1

    async def stop(self):
        self.release_cleanup.set()
        assert self.running == 0
        self.process = None
        self.stopped = True


@pytest.mark.parametrize("n", [20, 50, 100], ids=["N20", "N50", "N100"])
@pytest.mark.asyncio
async def test_cap23_real_asr_sessions_latest_pending_finish_weighting_and_cleanup(n):
    # Anti-hang guard; the deadlines passed to production remain unchanged.
    async with asyncio.timeout(15):
        config = AsrConfig(Path("/not-a-model/svi-scheduler-fixture"))
        engine = AsrEngine(config, slots=1, queue_limit=n)  # Real default decode limit: 3000 ms.
        engine.model_id = "physical-stub-not-an-accepted-model"
        assert engine.decode_timeout_ms == 3000 and engine.recorder is None
        blocker_scope = scope_for(-1)
        expected = {blocker_scope: (b"\x01\x00" * 16000, "blocker", "blocker")}
        sessions, scopes, finish_tasks, tasks = [], [], [], []
        worker = PhysicalAsrStub(expected, blocker_scope)
        engine.workers = [worker]
        try:
            blocker_owner = (blocker_scope.user_id, blocker_scope.session_id, 1)
            blocker = spawn(blocker_scope, engine.decode(expected[blocker_scope][0], "blocker", owner=blocker_owner))
            tasks.append(blocker)
            await until(lambda: worker.running == 1)
            for index in range(n):
                scope = scope_for(index)
                scopes.append(scope)
                session = MlxAsrSession(engine, config, AsrOpenOptions(scope.session_id, 1,
                    hint_terms=[f"Term{index:03d}"], user_id=scope.user_id))
                sessions.append(session)
                first = (index + 100).to_bytes(2, "little", signed=True) * 16000
                second = (index + 1000).to_bytes(2, "little", signed=True) * 16000
                expected[scope] = (first + second, f"Term{index:03d}", f"result-{index:03d}")
                with call_scope(scope):
                    await session.feed(first, 0)
            await until(lambda: engine.scheduler.snapshot()["low_waiting"] == n)
            queued = list(engine.scheduler.queues["low"])
            queued_jobs = dict(engine.scheduler.queues["low"])
            assert len(worker.calls) == 1  # Queued factories have not copied/decoded old windows.
            for scope, session in zip(scopes, sessions):
                await session.feed(expected[scope][0][32000:], 16000)
            assert list(engine.scheduler.queues["low"]) == queued
            assert dict(engine.scheduler.queues["low"]) == queued_jobs
            assert all(session.scope == scope and session._received() == 32000
                       for scope, session in zip(scopes, sessions))
            # Real engine low queue is bounded by N; overflow never reaches the worker.
            with pytest.raises(SchedulerBusy):
                await engine.decode(b"\x03\x00" * 16000, "overflow", owner=("overflow", n, 1))
            assert len(worker.calls) == 1

            # Finish must remove each queued partial before scheduling one high job.
            high_owners = {session.owner for session in sessions[::2]}
            for scope, session in zip(scopes[::2], sessions[::2]):
                task = spawn(scope, session.finish(32000, loop_time() + 10))
                finish_tasks.append(task)
                tasks.append(task)
            await until(lambda: engine.scheduler.snapshot() == {
                "running": 1, "high_waiting": n // 2, "low_waiting": n // 2})
            assert set(engine.scheduler.queues["high"]) == high_owners
            assert not (set(engine.scheduler.queues["low"]) & high_owners)
            assert all(queued_jobs[owner].done.is_set() and queued_jobs[owner].operation is None
                       for owner in high_owners)
            high_order = list(engine.scheduler.queues["high"])
            low_order = list(engine.scheduler.queues["low"])

            parent_audio = worker.active_audio
            running_job = engine.scheduler.running[blocker_owner]
            worker_process = worker.process
            assert isinstance(parent_audio, bytearray) and parent_audio == expected[blocker_scope][0]
            cancel = asyncio.create_task(engine.cancel(blocker_owner))
            tasks.append(cancel)
            await until(worker.cleanup_entered.is_set, .5)
            # Local PCM is released immediately; the gated worker still owns its slot.
            assert not cancel.done() and worker.active_audio is parent_audio and not parent_audio
            assert worker.running == 1 and not worker.stopped and worker.process is worker_process
            assert engine.scheduler.running[blocker_owner] is running_job
            assert running_job.task is not None and not running_job.task.done()
            assert not running_job.done.is_set()
            assert len(worker.calls) == 1 and engine.scheduler.snapshot() == {
                "running": 1, "high_waiting": n // 2, "low_waiting": n // 2}
            assert list(engine.scheduler.queues["high"]) == high_order
            assert list(engine.scheduler.queues["low"]) == low_order
            assert all(session.buffer == expected[scope][0] for scope, session in zip(scopes, sessions))
            worker.release_cleanup.set()
            await cancel
            assert isinstance((await asyncio.gather(blocker, return_exceptions=True))[0], asyncio.CancelledError)
            finals = await asyncio.gather(*finish_tasks)
            await until(lambda: all(session.decoded_samples == 32000 for session in sessions[1::2]))
            # Stop continuing partial loops through the production cancellation path.
            await asyncio.gather(*(session.cancel() for session in sessions[1::2]))
            assert worker.active_audio is None and worker.peak == 1
            assert len(worker.calls) == n + 1
            actual_scopes = [item[0] for item in worker.calls[1:]]
            assert set(actual_scopes) == set(scopes) and len(actual_scopes) == len(set(actual_scopes))
            actual_owners = [(s.user_id, s.session_id, s.attempt_no) for s in actual_scopes]
            assert [owner for owner in actual_owners if owner in high_owners] == high_order
            assert [owner for owner in actual_owners if owner not in high_owners] == low_order
            # Contract oracle, not a copied scheduler: while both classes are nonempty,
            # three finishes must precede each partial. The low tail may borrow capacity.
            low_positions = [i for i, owner in enumerate(actual_owners) if owner not in high_owners]
            assert low_positions[:(n // 2) // 3] == list(range(3, 4 * ((n // 2) // 3), 4))
            assert [final.text for final in finals] == [expected[s][2] for s in scopes[::2]]
            assert all(not final.no_speech and final.processed_samples == 32000 for final in finals)
            for scope, session in zip(scopes[1::2], sessions[1::2]):
                partial = session.queue.get_nowait()
                assert partial.stable_text + partial.provisional_text == expected[scope][2]
                assert partial.decoded_samples == partial.received_samples == 32000
            assert all(not session.buffer for session in sessions)
            assert all(not session.task or session.task.done() for session in sessions)
            await until(lambda: empty(engine.scheduler))
            assert current_scope.get() is None
        finally:
            worker.release_cleanup.set()
            # Test cleanup has its own finite guard. No product deadline is changed.
            async with asyncio.timeout(12):
                await asyncio.gather(*(session.cancel() for session in sessions), return_exceptions=True)
                await engine.close()
                await asyncio.gather(*tasks, return_exceptions=True)
        assert worker.stopped and not engine.scheduler.workers


SCHEMA = {"type": "object", "required": ["label"], "additionalProperties": False,
          "properties": {"label": {"type": "string"}}}


def request(label, purpose="refine"):
    return LlmRequest(purpose, f"system:{label}", {"label": label}, SCHEMA)


class PhysicalLlmStub:
    """ONLY the inner provider is stubbed; production scope/queue/schema logic runs."""
    provider_id, model_id = "physical-stub", "not-capacity-evidence"

    def __init__(self):
        self.release = asyncio.Event()
        self.calls = []
        self.active, self.peak = 0, 0
        self.closed = False

    async def generate(self, request, deadline):
        scope = current_scope.get()
        label = request.input["label"]
        assert request.system_prompt == f"system:{label}" and scope is not None
        assert loop_time() < deadline
        self.calls.append((label, request.purpose, scope, deadline, id(request.input)))
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            if label == "blocker":
                await self.release.wait()
            return LlmResponse(parsed={"label": label}, provider=self.provider_id, model_id=self.model_id)
        finally:
            self.active -= 1

    async def close(self):
        self.closed = True


@pytest.mark.parametrize("n", [50, 100], ids=["N50", "N100"])
@pytest.mark.asyncio
async def test_cap23_real_llm_provider_n_foreground_20_classify_scopes_bounds_and_drain(n):
    # Anti-hang guard; the deadlines passed to production remain unchanged.
    async with asyncio.timeout(15):
        config = LlmConfig(max_inflight=1)
        assert (config.call_timeout_ms, config.foreground_wait_ms, config.classify_wait_ms,
                config.classify_queue_size) == (10000, 2000, 1000, 20)
        inner = PhysicalLlmStub()
        provider = ScheduledLlmProvider(inner, config, n)
        assert provider.recorder is None  # No DB ledger or fabricated physical usage.
        tasks, expected, deadlines = [], {}, {}
        try:
            def launch(label, scope, purpose="refine"):
                deadline = loop_time() + 10  # Original total deadline, never reset on queue dispatch.
                expected[label], deadlines[label] = (purpose, scope), deadline
                task = spawn(scope, provider.generate(request(label, purpose), deadline))
                tasks.append(task)
                return task

            blocker = launch("blocker", scope_for(-2))
            await until(lambda: len(inner.calls) == 1)
            foreground = [launch(f"f{i}", scope_for(i)) for i in range(n)]
            classify = [launch(f"c{i}", scope_for(i, classify=True), "classify") for i in range(20)]
            await until(lambda: provider.scheduler.snapshot() == {
                "running": 1, "high_waiting": n, "low_waiting": 20}, .8)
            # Both independent limits and per-owner exclusion are production behavior.
            for label, scope, purpose in [
                ("high-overflow", scope_for(n + 1), "refine"),
                ("low-overflow", scope_for(n + 2, classify=True), "classify"),
                ("duplicate-f", scope_for(0), "audit"),
                ("duplicate-c", scope_for(0, classify=True), "classify"),
            ]:
                with call_scope(scope):
                    response = await provider.generate(request(label, purpose), loop_time() + 10)
                assert response.error is not None and response.error.code == "resource_exhausted"
            assert len(inner.calls) == 1
            cancelled_jobs = [provider.scheduler.queues["high"][(scope_for(n-1).user_id, scope_for(n-1).session_id, 1)],
                              provider.scheduler.queues["low"][(scope_for(19).user_id, "classify")]]
            foreground[-1].cancel()
            classify[-1].cancel()
            cancelled = await asyncio.gather(foreground[-1], classify[-1], return_exceptions=True)
            assert all(isinstance(value, asyncio.CancelledError) for value in cancelled)
            assert all(job.done.is_set() and job.operation is None for job in cancelled_jobs)
            assert provider.scheduler.snapshot() == {"running": 1, "high_waiting": n-1, "low_waiting": 19}
            foreground[-1] = launch(f"f{n-1}-replacement", scope_for(n-1))
            classify[-1] = launch("c19-replacement", scope_for(19, classify=True), "classify")
            await until(lambda: provider.scheduler.snapshot() == {
                "running": 1, "high_waiting": n, "low_waiting": 20}, .8)
            inner.release.set()
            responses = await asyncio.gather(blocker, *foreground, *classify)
            expected_labels = ["blocker"] + [f"f{i}" for i in range(n-1)] + [f"f{n-1}-replacement"] + [f"c{i}" for i in range(19)] + ["c19-replacement"]
            assert [r.parsed for r in responses] == [{"label": label} for label in expected_labels]
            assert all(r.error is None for r in responses)
            assert len(inner.calls) == n + 21 and inner.peak == 1 and inner.active == 0
            assert {call[0] for call in inner.calls} == set(expected_labels)
            assert len({call[4] for call in inner.calls}) == n + 21  # Distinct request payload objects.
            for label, purpose, scope, deadline, _identity in inner.calls:
                assert (purpose, scope) == expected[label]
                assert deadline <= deadlines[label]
            dispatched = inner.calls[1:]
            lows = [i for i, call in enumerate(dispatched) if call[1] == "classify"]
            # Blocker was the first high dispatch. Two more highs then one low;
            # thereafter three highs per low while both classes remain available.
            weighted_lows = min(20, (n + 1) // 3)
            assert lows[:weighted_lows] == list(range(2, 4 * weighted_lows, 4))
            assert [call[0] for call in dispatched if call[1] != "classify"] == expected_labels[1:n+1]
            assert [call[0] for call in dispatched if call[1] == "classify"] == expected_labels[n+1:]
            await until(lambda: empty(provider.scheduler))
            assert current_scope.get() is None
        finally:
            inner.release.set()
            # Real running-LLM cancellation may hold its permit to the original 10 s.
            async with asyncio.timeout(12):
                await provider.close()
                await asyncio.gather(*tasks, return_exceptions=True)
        assert inner.closed and not provider.scheduler.workers
