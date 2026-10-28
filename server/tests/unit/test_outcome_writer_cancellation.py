"""Metadata cancellation control units; not physical model/PG acceptance.

Uses actual persist_outcome and actual provider wrapper control flow. Synthetic
recorders expose write/rollback boundaries without inventing durable DB rows.
The separate real-PG/OS diagnostic retains the original finish10 budget.
"""
from __future__ import annotations

import asyncio
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.clock import loop_time
from app.core.config import LlmConfig
from app.providers.asr.base import ProviderError
from app.providers.llm.base import LlmRequest, LlmResponse
from app.providers.llm.scheduled import ScheduledLlmProvider
from app.services.asr_core import AsrConfig, AsrEngine
from app.services.inference_context import (
    OUTCOME_WRITE_BUDGET_SECONDS, CallScope, call_scope, persist_outcome,
)


def payload():
    return dict(call_id='synthetic-call', status='failed', error_code='ASR_TIMEOUT',
                elapsed_ms=10, prompt_tokens=None, completion_tokens=None)


def warnings(caplog):
    return [record for record in caplog.records
            if record.name == 'app.services.inference_context'
            and str(record.msg).startswith('MODEL_CALL_OUTCOME_UNCONFIRMED')]


async def joined(task):
    if task is not None:
        async with asyncio.timeout(2):
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel_count', [1, 3], ids=['first-cancel', 'repeated-cancel'])
async def test_caller_cancels_do_not_cancel_or_restart_the_frozen_writer(cancel_count, caplog):
    entered, release = asyncio.Event(), asyncio.Event()
    calls, replacement_calls = [], []
    writer_task = None

    async def finish(**outcome):
        nonlocal writer_task
        writer_task = asyncio.current_task()
        calls.append(outcome)
        entered.set()
        await release.wait()
        calls.append('completed')

    async def replacement(**outcome):
        replacement_calls.append(outcome)

    data, recorder = payload(), SimpleNamespace(finished=finish)
    task = asyncio.create_task(persist_outcome(recorder, data, loop_time() + 1.5))
    try:
        async with asyncio.timeout(.5):
            await entered.wait()
        for index in range(cancel_count):
            task.cancel('first-cancel' if index == 0 else 'later-cancel')
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not task.done(), 'caller completed before its sole write owner finished'
            assert writer_task is not task and writer_task.cancelling() == 0
        # A cancelled caller must not restart against changed dependencies/data.
        data.update(status='succeeded', error_code=None, prompt_tokens=0)
        recorder.finished = replacement
        release.set()
        with pytest.raises(asyncio.CancelledError) as result:
            await task
        assert result.value.args == ('first-cancel',)
        assert calls == [payload(), 'completed']
        assert replacement_calls == [] and writer_task.done()
        assert warnings(caplog) == []
    finally:
        release.set()
        await joined(task)


@pytest.mark.asyncio
@pytest.mark.parametrize('remaining', [.12, 2.0], ids=['original-deadline', 'one-second-cap'])
async def test_repeated_cancel_never_renews_the_original_write_deadline(remaining, caplog):
    assert OUTCOME_WRITE_BUDGET_SECONDS == 1.0
    entered = asyncio.Event()
    observations = {}

    async def finish(**outcome):
        observations['task'] = asyncio.current_task()
        observations['entered'] = loop_time()
        entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            observations['cancel_at'] = loop_time()
            observations['writer_cancels'] = asyncio.current_task().cancelling()
            raise

    before = loop_time()
    deadline = before + remaining
    task = asyncio.create_task(persist_outcome(SimpleNamespace(finished=finish), payload(), deadline))
    try:
        async with asyncio.timeout(.5):
            await entered.wait()
        for index in range(3):
            task.cancel(f'caller-{index}')
            await asyncio.sleep(.02)
            assert not task.done()
        with pytest.raises(asyncio.CancelledError) as result:
            await task
        assert result.value.args == ('caller-0',)
        expected = min(remaining, 1.0)
        # Unit scheduling tolerance only; real PG's original finish10 oracle is
        # separate and unchanged. The recorder's first cancellation is expiry,
        # never one of the earlier caller cancellations.
        assert before + expected <= observations['cancel_at']
        assert observations['cancel_at'] <= observations['entered'] + expected + .2
        assert observations['writer_cancels'] == 1
        assert observations['task'].done()
        assert len(warnings(caplog)) == 1
    finally:
        await joined(task)


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel_count', [0, 2], ids=['deadline-cleanup', 'cancel-during-cleanup'])
async def test_deadline_warns_but_repeated_cancels_cannot_cut_rollback_cleanup(cancel_count, caplog):
    cleanup_entered, release_cleanup = asyncio.Event(), asyncio.Event()
    observations = {}

    async def finish(**outcome):
        observations['task'] = asyncio.current_task()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            observations['cleanup_enter'] = loop_time()
            cleanup_entered.set()
            await release_cleanup.wait()
            observations['cleanup_done'] = True
            raise

    deadline = loop_time() + .08
    task = asyncio.create_task(persist_outcome(SimpleNamespace(finished=finish), payload(), deadline))
    try:
        async with asyncio.timeout(.5):
            await cleanup_entered.wait()
        # uvloop's integer-ms clock / 1000 can be one binary64 ULP below
        # the same tick obtained by adding .08. No millisecond SLA tolerance.
        assert loop_time() >= math.nextafter(deadline, -math.inf)
        assert len(warnings(caplog)) == 1, 'overdue rollback must already be observable'
        for index in range(cancel_count):
            task.cancel(f'cleanup-cancel-{index}')
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        assert not task.done() and not observations['task'].done()
        assert observations['task'].cancelling() == 1
        release_cleanup.set()
        if cancel_count:
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert await task is False
        assert observations['cleanup_done'] and observations['task'].done()
        assert len(warnings(caplog)) == 1
    finally:
        release_cleanup.set()
        await joined(task)


@pytest.mark.asyncio
async def test_recorder_suppressing_timeout_and_returning_late_cannot_report_success(caplog):
    expired, release = asyncio.Event(), asyncio.Event()
    writer = None

    async def finish(**outcome):
        nonlocal writer
        writer = asyncio.current_task()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            expired.set()
            await release.wait()
            # Deliberately broken recorder: simulate swallowing timeout. The
            # helper cannot undo a late real commit, but must never claim True.
            return

    deadline = loop_time() + .05
    task = asyncio.create_task(persist_outcome(SimpleNamespace(finished=finish), payload(), deadline))
    try:
        async with asyncio.timeout(.5):
            await expired.wait()
        assert loop_time() >= deadline and not task.done()
        release.set()
        assert await task is False
        assert writer.done() and len(warnings(caplog)) == 1
    finally:
        release.set()
        await joined(task)


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['db-error', 'writer-self-cancel'])
async def test_writer_failure_is_false_and_safe_warning_not_new_caller_cancellation(failure, caplog):
    observed = []

    async def finish(**outcome):
        observed.append(asyncio.current_task())
        if failure == 'db-error':
            raise RuntimeError('synthetic-sensitive-error-detail-must-not-be-logged')
        asyncio.current_task().cancel('writer-internal-cancel')
        await asyncio.Future()

    assert await persist_outcome(SimpleNamespace(finished=finish), payload(), loop_time() + 1) is False
    assert len(observed) == 1 and observed[0].done()
    assert len(warnings(caplog)) == 1
    assert 'synthetic-sensitive-error-detail' not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['no-recorder', 'missing-call', 'empty-call'])
async def test_nondispatched_calls_never_create_outcome_work_or_warning(mode, caplog):
    calls = []

    async def finish(**outcome):
        calls.append(outcome)
        raise AssertionError('not dispatched')

    recorder = None if mode == 'no-recorder' else SimpleNamespace(finished=finish)
    data = payload() if mode == 'no-recorder' else ({} if mode == 'missing-call' else {'call_id': ''})
    assert await persist_outcome(recorder, data, loop_time() - 1) is True
    assert calls == [] and warnings(caplog) == []


@pytest.mark.asyncio
async def test_already_expired_budget_does_not_start_writer(caplog):
    calls = []

    async def finish(**outcome):
        calls.append(outcome)

    assert await persist_outcome(SimpleNamespace(finished=finish), payload(), loop_time() - .1) is False
    assert calls == [] and len(warnings(caplog)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('wrapper', ['asr-exception', 'llm-exception', 'llm-structured-error'])
async def test_real_wrappers_keep_existing_error_policy_when_ledger_fails(wrapper, caplog):
    initial = TimeoutError('synthetic-original-asr') if wrapper == 'asr-exception' else RuntimeError('synthetic-original-llm')
    attempts = []

    class Recorder:
        async def started(self, *args, **kwargs):
            return 'synthetic-wrapper-call'

        async def finished(self, **outcome):
            attempts.append(outcome)
            raise RuntimeError('synthetic-db-failure')

    class Worker:
        process = SimpleNamespace(is_alive=lambda: True)
        async def decode(self, args, deadline):
            raise initial
        async def stop(self):
            self.process = None

    class Remote:
        provider_id, model_id = 'synthetic', 'no-real-model'
        async def generate(self, request, deadline):
            if wrapper == 'llm-structured-error':
                return LlmResponse(parsed=None, provider=self.provider_id, model_id=self.model_id,
                    finish_reason='error', error=ProviderError('timeout', 'synthetic timeout'))
            raise initial
        async def close(self):
            pass

    if wrapper == 'asr-exception':
        provider = AsrEngine(AsrConfig(Path('/not-a-real-model')))
        provider.workers = [Worker()]
        provider.model_id = 'synthetic-model'
    else:
        provider = ScheduledLlmProvider(Remote(), LlmConfig(max_inflight=1), 2)
    provider.recorder = Recorder()
    try:
        scope = CallScope('synthetic-user', 'synthetic-session', 1)
        if wrapper == 'asr-exception':
            with pytest.raises(TimeoutError) as result:
                await provider.decode(b'\0\0', '', purpose='asr_finish', scope=scope, deadline=loop_time() + 2)
            assert result.value is initial
        else:
            with call_scope(scope):
                if wrapper == 'llm-structured-error':
                    value = await provider.generate(LlmRequest('refine', '', {}, {}), loop_time() + 2)
                    # Existing Scheduled policy remains unchanged; this patch
                    # does not alter returned error precedence in callers.
                    assert value.error is not None and value.error.code == 'unavailable'
                else:
                    with pytest.raises(RuntimeError) as result:
                        await provider.generate(LlmRequest('refine', '', {}, {}), loop_time() + 2)
                    assert result.value is initial
        assert len(attempts) == 1 and len(warnings(caplog)) == 1
        assert provider.scheduler.snapshot() == {'running': 0, 'high_waiting': 0, 'low_waiting': 0}
    finally:
        await provider.close()
