"""Permit/ledger fault isolation only; these fixtures do not prove model latency."""
import asyncio
import time
from types import SimpleNamespace

import pytest

from app.core.clock import loop_time
from app.core.config import LlmConfig
from app.providers.llm.base import LlmRequest, LlmResponse
from app.providers.llm.scheduled import ScheduledLlmProvider
from app.services.capacity import CapacityError, CapacityService
from app.services.inference_context import CallScope, call_scope, persist_outcome


@pytest.mark.asyncio
async def test_finished_write_does_not_hold_model_execution_permit():
    ledger_entered, release_ledger = asyncio.Event(), asyncio.Event()

    class Remote:
        provider_id, model_id = "stub", "fixture"

        async def generate(self, request, deadline):
            return LlmResponse(parsed={}, provider=self.provider_id, model_id=self.model_id)

        async def close(self):
            pass

    class Recorder:
        async def started(self, *args):
            return "fixture-call-id"

        async def finished(self, **outcome):
            ledger_entered.set()
            await release_ledger.wait()

    provider = ScheduledLlmProvider(Remote(), LlmConfig(max_inflight=1), 2)
    provider.recorder = Recorder()
    try:
        with call_scope(CallScope("user", "session", 1)):
            task = asyncio.create_task(provider.generate(LlmRequest("refine", "", {}, {}), loop_time() + 2))
        await asyncio.wait_for(ledger_entered.wait(), .5)
        assert provider.scheduler.snapshot()["running"] == 0
        assert not task.done()
        release_ledger.set()
        assert (await task).error is None
    finally:
        release_ledger.set()
        await provider.close()


@pytest.mark.asyncio
async def test_terminal_write_is_bounded_by_remaining_deadline_and_observable(caplog):
    entered = asyncio.Event()

    async def hang(**outcome):
        entered.set()
        await asyncio.Future()

    recorder = SimpleNamespace(finished=hang)
    start = time.monotonic()
    deadline = loop_time() + .03
    assert not await persist_outcome(recorder, {"call_id": "fixture-call-id"}, deadline)
    assert entered.is_set()
    assert time.monotonic() - start < .3
    assert "MODEL_CALL_OUTCOME_UNCONFIRMED" in caplog.text


@pytest.mark.asyncio
async def test_cancel_during_terminal_write_propagates():
    entered = asyncio.Event()

    async def hang(**outcome):
        entered.set()
        await asyncio.Future()

    task = asyncio.create_task(persist_outcome(SimpleNamespace(finished=hang),
        {"call_id": "fixture-call-id"}, loop_time() + 2))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_missing_capacity_row_is_unavailable_not_cached_default():
    class EmptyTransaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, model, key):
            return None

    service = CapacityService(SimpleNamespace(sessions=SimpleNamespace(initial_max_active=20)),
        SimpleNamespace(session_factory=EmptyTransaction))
    with pytest.raises(CapacityError, match="capacity row missing"):
        await service.current()
