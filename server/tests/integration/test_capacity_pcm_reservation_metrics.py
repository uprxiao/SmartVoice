"""Real HTTP/WS/PG reservation accounting; synthetic model execution only."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time

import pytest
from sqlalchemy import select

from app.core.config import SESSION_PCM_RESERVE
from app.models import SessionAttempt, VoiceSession
from app.providers.asr.base import ProviderError
from app.providers.llm.base import LlmResponse
from tests.conftest import auth_headers, register_and_login
from tests.ids import fixture_uuid
from tests.integration.test_capacity_window_metrics import metrics_client
from tests.test_admin import login_admin
from tests.test_sessions import create_session, dictation


@pytest.mark.parametrize('mode', ['created', 'initial-refining', 'asr-retry', 'refine-retry', 'mixed'])
def test_audio_reservation_counts_current_attempt_kind_not_all_active_sessions(
    metrics_client, monkeypatch, record_property, mode
):
    client = metrics_client
    admin = auth_headers(login_admin(client))
    actor = register_and_login(client, email='pcm-metric@example.invalid')
    headers = auth_headers(actor)
    inner = client.app.state.llm.inner
    original_generate = inner.generate
    entered = threading.Event()

    async def make_gate():
        return asyncio.Event()

    gate = client.portal.call(make_gate)
    phase = 'ordinary'

    async def generate(request, deadline):
        if phase == 'fallback':
            return LlmResponse(parsed=None, provider=inner.provider_id, model_id=inner.model_id,
                finish_reason='error', error=ProviderError('unavailable', 'owned metric fixture'))
        if phase == 'llm-blocked':
            entered.set()
            await gate.wait()
        return await original_generate(request, deadline)

    monkeypatch.setattr(inner, 'generate', generate)
    created = create_session(client, headers, 'pcm-metric-initial')
    assert created.status_code == 201
    sid = created.json()['session_id']
    holders = [(sid, headers)]
    pool = ThreadPoolExecutor(max_workers=1)
    future = None
    try:
        if mode == 'initial-refining':
            phase = 'llm-blocked'
            future = pool.submit(dictation, client, headers, sid)
            assert entered.wait(5), 'actual initial refine dispatch did not enter fixture'
        elif mode == 'asr-retry':
            first = client.app.state.sessions.runtimes[sid].asr

            async def failed_finish(total_samples, deadline):
                raise ProviderError('ASR_FAILED', 'owned first complete audio failure')

            monkeypatch.setattr(first, 'finish', failed_finish)
            assert dictation(client, headers, sid)[-1]['type'] == 'error'
            original_open = client.app.state.asr.open

            async def open_blocked(options):
                value = await original_open(options)
                finish = value.finish

                async def blocked_finish(total_samples, deadline):
                    entered.set()
                    await gate.wait()
                    return await finish(total_samples, deadline)

                value.finish = blocked_finish
                return value

            monkeypatch.setattr(client.app.state.asr, 'open', open_blocked)
            response = client.post(f'/api/v1/sessions/{sid}/retry', json={'retry_kind':'asr'},
                headers={**headers, 'Idempotency-Key':fixture_uuid('pcm-metric-asr-retry')})
            assert response.status_code == 202, response.text
            assert entered.wait(5), 'actual retry ASR did not enter fixture'
        elif mode in ('refine-retry', 'mixed'):
            phase = 'fallback'
            assert dictation(client, headers, sid)[-1]['type'] == 'result.ready'
            original = client.get(f'/api/v1/sessions/{sid}', headers=headers).json()
            assert original['result']['review_required'] is True
            phase = 'llm-blocked'
            response = client.post(f'/api/v1/sessions/{sid}/retry', json={'retry_kind':'refine'},
                headers={**headers, 'Idempotency-Key':fixture_uuid('pcm-metric-refine-retry')})
            assert response.status_code == 202, response.text
            assert entered.wait(5), 'actual refine retry did not enter fixture'
            if mode == 'mixed':
                other = auth_headers(register_and_login(client, email='pcm-other@example.invalid'))
                second = create_session(client, other, 'pcm-other-initial')
                assert second.status_code == 201
                holders.append((second.json()['session_id'], other))

        async def database_facts():
            async with client.app.state.db.session_factory() as db:
                rows = (await db.execute(select(VoiceSession.id, VoiceSession.state,
                    VoiceSession.current_attempt_no, SessionAttempt.kind).join(SessionAttempt,
                    (SessionAttempt.user_id == VoiceSession.user_id)
                    & (SessionAttempt.session_id == VoiceSession.id)
                    & (SessionAttempt.attempt_no == VoiceSession.current_attempt_no))
                    .where(VoiceSession.state.in_(('created','streaming','finalizing','refining'))))).all()
                return [dict(session_id=r.id, state=r.state, attempt_no=r.current_attempt_no, kind=r.kind) for r in rows]

        facts = client.portal.call(database_facts)
        expected_active = 2 if mode == 'mixed' else 1
        expected_reservation = 0 if mode == 'refine-retry' else SESSION_PCM_RESERVE
        assert len(facts) == expected_active
        assert sum(row['kind'] != 'refine' for row in facts)*SESSION_PCM_RESERVE == expected_reservation
        response = client.get('/api/admin/capacity', headers=admin)
        assert response.status_code == 200
        runtime = response.json()['runtime']
        assert runtime is not None and runtime['active_sessions'] == expected_active
        assert runtime['reserved_pcm_bytes'] == expected_reservation, {
            'mode':mode, 'actual':runtime['reserved_pcm_bytes'], 'expected':expected_reservation, 'current_attempts':facts}
        assert runtime['active_pcm_bytes'] == (16000 if mode == 'asr-retry' else 0)
        record_property('actual_pcm_reservation_metric', json.dumps({'mode':mode,
            'current_attempts':facts, 'runtime':runtime, 'real_model_or_capacity_claimed':False}))
    finally:
        client.portal.call(gate.set)
        if future is not None:
            assert future.result(timeout=10)[-1]['type'] == 'result.ready'
        for target, owner in holders:
            end = time.monotonic()+3
            while time.monotonic() < end:
                state = client.get(f'/api/v1/sessions/{target}', headers=owner).json()['state']
                if state not in ('created','streaming','finalizing','refining'):
                    break
                if state == 'created':
                    assert client.post(f'/api/v1/sessions/{target}/cancel', headers=owner).status_code == 200
                else:
                    time.sleep(.01)
            assert client.get(f'/api/v1/sessions/{target}', headers=owner).json()['state'] not in ('created','streaming','finalizing','refining')
        pool.shutdown(wait=True)
