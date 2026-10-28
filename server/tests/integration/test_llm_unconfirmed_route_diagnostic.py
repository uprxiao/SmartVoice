"""Diagnostic controls, NOT a fix or a real-provider success replacement.

Original Refiner/Scheduled/PG recorder; only physical Mock response and the
explicit finished-exception control differ. Root chooses whether to execute.
"""
from __future__ import annotations

import json
import logging
import uuid

import pytest
from sqlalchemy import select

from app.core.clock import loop_time
from app.models import ModelCall, ModelUsageDaily
from app.providers.llm.base import LlmResponse
from app.services.inference_context import CallScope, call_scope
from tests.conftest import auth_headers, register_and_login
from tests.test_sessions import create_session


@pytest.mark.parametrize('mode', [
    'healthy-success', 'healthy-provider-exception', 'finished-exception',
])
def test_original_refiner_scheduled_pg_ledger_route(
    app_client, monkeypatch, caplog, record_property, mode,
):
    # Actual API-created identities; this directly exercises the model layer,
    # not audio/WS processing, ready publication or actual Ollama inference.
    auth = register_and_login(app_client)
    headers = auth_headers(auth)
    created = create_session(app_client, headers, str(uuid.uuid4()))
    assert created.status_code == 201
    sid, uid = created.json()['session_id'], auth['user']['id']
    state = app_client.app.state
    raw = '固定合成账本对照。'
    private_exception_canary = 'synthetic-private-exception-must-not-be-logged'
    calls, finishes = [], []
    original_finished = state.recorder.finished
    caplog.set_level(logging.WARNING, logger='app.services.inference_context')

    async def physical(request, deadline):
        assert request.purpose == 'refine' and request.call_id
        calls.append(request.call_id)
        if mode == 'healthy-provider-exception':
            raise RuntimeError(private_exception_canary)
        assert request.output_mode == 'text' and request.output_schema_id == 'refine-r2-text-v1'
        return LlmResponse(parsed=None, text=raw,
            provider=state.llm.inner.provider_id, model_id=state.llm.inner.model_id)

    async def observed_finished(**outcome):
        event = {'call_id': outcome['call_id'], 'status': outcome['status'],
                 'error_code': outcome['error_code'], 'entered': loop_time()}
        finishes.append(event)
        try:
            if mode == 'finished-exception':
                # Declared persistence-boundary fault, after the real started
                # commit; not proof this caused the original real-provider F.
                raise RuntimeError(private_exception_canary)
            return await original_finished(**outcome)
        except BaseException as exc:
            event['exception_type'] = type(exc).__name__
            raise
        finally:
            event['returned_or_raised_at'] = loop_time()

    monkeypatch.setattr(state.llm.inner, 'generate', physical)
    monkeypatch.setattr(state.recorder, 'finished', observed_finished)

    async def exercise():
        deadline = loop_time() + state.config.pipeline.stop_to_result_budget_ms / 1000
        with call_scope(CallScope(uid, sid, 1)):
            result = await state.sessions.refiner.refine(raw, [], deadline=deadline)
        assert loop_time() < deadline
        assert result.text == raw
        assert len(calls) == len(finishes) == 1 and calls[0] == finishes[0]['call_id']
        async with state.db.session_factory() as db:
            rows = (await db.scalars(select(ModelCall).where(
                ModelCall.user_id == uid, ModelCall.session_id == sid,
                ModelCall.attempt_no == 1, ModelCall.purpose == 'refine'))).all()
            assert len(rows) == 1 and rows[0].id == calls[0]
            row = rows[0]
            usage = await db.scalar(select(ModelUsageDaily).where(
                ModelUsageDaily.user_id == uid, ModelUsageDaily.purpose == 'refine'))
            safe = {'mode': mode, 'call_id': row.id, 'model_call_status': row.status,
                    'error_code': row.error_code, 'ended_at_present': row.ended_at is not None,
                    'aggregated_at_present': row.aggregated_at is not None,
                    'usage_calls': usage.calls if usage else 0,
                    'refiner_review_required': result.review_required,
                    'refiner_review_reason': result.review_reason,
                    'finished_attempts': finishes}
            if mode == 'finished-exception':
                # Diagnostic of the existing documented unconfirmed branch,
                # not a claim that a permanently running row closes NFR04.
                assert result.review_required and result.review_reason == 'llm_unavailable'
                assert row.status == 'running' and row.ended_at is row.aggregated_at is None
                assert usage is None
            else:
                assert row.ended_at is not None and row.aggregated_at is not None
                assert usage is not None and usage.calls == 1
                if mode == 'healthy-success':
                    assert not result.review_required
                    assert row.status == 'succeeded' and row.error_code is None
                    assert usage.successes == 1 and usage.failures == 0
                else:
                    assert result.review_required and result.review_reason == 'llm_unavailable'
                    assert row.status == 'unknown' and row.error_code == 'CLIENT_INTERRUPTED'
                    assert usage.successes == 0 and usage.failures == usage.unknown_calls == 1
        assert state.llm.scheduler.snapshot() == {'running': 0, 'high_waiting': 0, 'low_waiting': 0}
        return safe

    try:
        safe = app_client.portal.call(exercise)
        warnings = [record for record in caplog.records
                    if record.name == 'app.services.inference_context'
                    and str(record.msg).startswith('MODEL_CALL_OUTCOME_UNCONFIRMED')]
        assert len(warnings) == int(mode == 'finished-exception')
        assert private_exception_canary not in caplog.text
        record_property('llm_unconfirmed_route_diagnostic', json.dumps(safe, sort_keys=True))
    finally:
        # Official resource cleanup only. No SQL status UPDATE, manual ledger
        # completion, fake aggregation or recovery is used to repair the oracle.
        cancelled = app_client.post('/api/v1/sessions/'+sid+'/cancel', headers=headers)
        assert cancelled.status_code == 200 and cancelled.json()['state'] == 'cancelled'
