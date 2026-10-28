"""Budget/cancellation regressions; deterministic providers are not model evidence."""
from __future__ import annotations

import asyncio

import pytest

from app.core.clock import loop_time
from app.core.config import LlmConfig
from app.providers.llm.base import LlmRequest, LlmResponse
from app.providers.llm.scheduled import ScheduledLlmProvider
from app.services.inference_context import CallScope, call_scope


class ObservedRecorder:
    def __init__(self, *, hang_started=False):
        self.hang_started = hang_started
        self.start_entered = asyncio.Event()
        self.start_cancelled = asyncio.Event()
        self.started_calls = []
        self.outcomes = []
        self.finished_clock = None

    async def started(self, scope, provider, model_id, purpose):
        self.started_calls.append((scope, provider, model_id, purpose))
        self.start_entered.set()
        if self.hang_started:
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.start_cancelled.set()
                raise
        return "fixture-call-" + str(len(self.started_calls))

    async def finished(self, **outcome):
        # A real asynchronous metadata completion, separate from model execution.
        await asyncio.sleep(.02)
        self.outcomes.append(outcome)
        self.finished_clock = loop_time()


class HeldRemote:
    provider_id, model_id = "fixture", "budget-control"

    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.deadlines = []

    async def generate(self, request, deadline):
        self.deadlines.append(deadline)
        self.entered.set()
        await self.release.wait()
        return LlmResponse(parsed={}, provider=self.provider_id, model_id=self.model_id)

    async def close(self):
        self.release.set()


def request():
    return LlmRequest("classify", "synthetic-control", {}, {"type": "object"})


def provider_pair(*, hang_started=False):
    remote, recorder = HeldRemote(), ObservedRecorder(hang_started=hang_started)
    provider = ScheduledLlmProvider(remote, LlmConfig(max_inflight=1), 2)
    provider.recorder = recorder
    return provider, remote, recorder


@pytest.mark.asyncio
async def test_transport_timeout_commits_unknown_inside_original_total_budget():
    provider, remote, recorder = provider_pair()
    deadline = loop_time() + 1.25
    try:
        with call_scope(CallScope("fixture-user", "fixture-session", 1)):
            answer = await provider.generate(request(), deadline)
        assert answer.error is not None and answer.error.code == "timeout"
        assert len(remote.deadlines) == len(recorder.started_calls) == len(recorder.outcomes) == 1
        assert remote.deadlines[0] == deadline - 1
        assert remote.deadlines[0] <= recorder.finished_clock < deadline
        outcome = recorder.outcomes[0]
        assert outcome["status"] == "unknown" and outcome["error_code"] == "timeout"
        assert outcome["prompt_tokens"] is None and outcome["completion_tokens"] is None
        assert outcome["elapsed_ms"] > 0
        assert provider.scheduler.snapshot() == {"running": 0, "high_waiting": 0, "low_waiting": 0}
    finally:
        remote.release.set()
        await provider.close()


@pytest.mark.asyncio
async def test_insufficient_total_budget_never_records_or_dispatches():
    provider, remote, recorder = provider_pair()
    try:
        with call_scope(CallScope("fixture-user", "fixture-session", 1)):
            answer = await provider.generate(request(), loop_time() + .9)
        assert answer.error is not None and answer.error.code == "timeout"
        assert recorder.started_calls == recorder.outcomes == remote.deadlines == []
        assert provider.scheduler.workers == []
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_started_timeout_does_not_send_model_request_or_invent_completion():
    provider, remote, recorder = provider_pair(hang_started=True)
    deadline = loop_time() + 1.15
    try:
        with call_scope(CallScope("fixture-user", "fixture-session", 1)):
            answer = await provider.generate(request(), deadline)
        assert answer.error is not None and answer.error.code == "timeout"
        assert recorder.start_entered.is_set() and recorder.start_cancelled.is_set()
        assert len(recorder.started_calls) == 1
        assert recorder.outcomes == remote.deadlines == []
        assert loop_time() < deadline
        assert provider.scheduler.snapshot()["running"] == 0
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_explicit_early_cancel_keeps_interrupted_reason_and_unknown_tokens():
    provider, remote, recorder = provider_pair()
    deadline = loop_time() + 1.3
    task = None
    try:
        with call_scope(CallScope("fixture-user", "fixture-session", 1)):
            task = asyncio.create_task(provider.generate(request(), deadline))
        await asyncio.wait_for(remote.entered.wait(), .2)
        assert loop_time() < remote.deadlines[0]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(recorder.outcomes) == 1
        outcome = recorder.outcomes[0]
        assert outcome["status"] == "unknown" and outcome["error_code"] == "CLIENT_INTERRUPTED"
        assert outcome["prompt_tokens"] is None and outcome["completion_tokens"] is None
        assert remote.deadlines[0] <= recorder.finished_clock < deadline
        assert provider.scheduler.snapshot()["running"] == 0
    finally:
        remote.release.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await provider.close()


@pytest.mark.asyncio
async def test_waiting_consumes_call_budget_before_started_and_dispatch():
    provider, remote, recorder = provider_pair()
    first = second = None
    try:
        with call_scope(CallScope("first-user", "first-session", 1)):
            first = asyncio.create_task(provider.generate(request(), loop_time() + 2))
        await asyncio.wait_for(remote.entered.wait(), .2)
        with call_scope(CallScope("second-user", "second-session", 1)):
            second = asyncio.create_task(provider.generate(request(), loop_time() + 1.1))
        async with asyncio.timeout(.2):
            while provider.scheduler.snapshot()["low_waiting"] != 1:
                await asyncio.sleep(0)
        await asyncio.sleep(.15)
        remote.release.set()
        assert (await first).error is None
        answer = await second
        assert answer.error is not None and answer.error.code == "timeout"
        assert len(recorder.started_calls) == len(remote.deadlines) == len(recorder.outcomes) == 1
        assert recorder.started_calls[0][0].user_id == "first-user"
        assert provider.scheduler.snapshot() == {"running": 0, "high_waiting": 0, "low_waiting": 0}
    finally:
        remote.release.set()
        tasks = [task for task in (first, second) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await provider.close()
