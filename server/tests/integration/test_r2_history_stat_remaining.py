"""Exact existing API/PG arithmetic and retry guards; explicit Mock physics."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import math
from zoneinfo import ZoneInfo

import pytest

from app.providers.asr.base import ProviderError
from app.providers.llm.base import LlmResponse
from app.providers.llm.mock import MockLlmProvider
from app.services.inference_context import current_scope
from tests.conftest import auth_headers, execute_sql, register_and_login
from tests.cases.test_api_cases import claim, dictation_ready, state_of
from tests.cases.test_dict_cases import fetch_one, key
from tests.cases.test_hist_cases import wait_state
from tests.cases.test_stat_cases import add_contribution, sid, stats, uid
from tests.test_sessions import create_session, dictation, frame


def today():
    return dt.datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()


def receipt(client, headers, session, insertion, status='inserted'):
    return client.post('/api/v1/sessions/'+session['session_id']+'/insertions/'+insertion+'/receipt',
        headers=headers, json={'status': status, 'observable': status == 'inserted',
            'error_code': None if status == 'inserted' else 'VERIFY_FAILED'})


def counts():
    return tuple(fetch_one('select count(*) from '+table) for table in
        ('sessions', 'session_attempts', 'results', 'model_calls', 'operation_requests', 'learning_events'))


@pytest.mark.parametrize('variant,kind,code,reason', [
    ('cancelled', 'asr', 'RETRY_UNAVAILABLE', 'cancelled'),
    ('empty', 'asr', 'RETRY_UNAVAILABLE', 'empty'),
    ('reserved', 'refine', 'RETRY_UNAVAILABLE', 'insertion_reserved'),
    ('inserted', 'refine', 'RETRY_UNAVAILABLE', 'already_inserted'),
    ('unverified', 'refine', 'RETRY_UNAVAILABLE', 'insertion_unverified'),
    ('expired', 'refine', 'CONTENT_EXPIRED', None),
])
def test_hist11_every_rejection_has_exact_code_and_no_model_or_attempt(app_client, variant, kind, code, reason):
    h = auth_headers(register_and_login(app_client))
    if variant == 'cancelled':
        session = create_session(app_client, h, 'r2-hist11').json()
        assert app_client.post('/api/v1/sessions/'+session['session_id']+'/cancel', headers=h).status_code == 200
    elif variant == 'empty':
        app_client.app.state.sessions.asr_provider.text = ''
        session = create_session(app_client, h, 'r2-hist11').json()
        assert dictation(app_client, h, session['session_id'])[-1]['type'] == 'session.empty'
    else:
        session, result = dictation_ready(app_client, h, 'r2-hist11')
        if variant == 'expired':
            # Declared cleared-content metadata fixture, not a fake ready state.
            execute_sql('update sessions set content_cleared_at=now() where id=$1', session['session_id'])
            assert state_of(app_client, h, session)['content_expired'] is True
        else:
            claimed = claim(app_client, h, session, result)
            assert claimed.status_code == 201
            if variant != 'reserved':
                assert receipt(app_client, h, session, claimed.json()['insertion_id'], variant).status_code == 200
    before = counts()
    response = app_client.post('/api/v1/sessions/'+session['session_id']+'/retry',
        headers={**h, 'Idempotency-Key': key()}, json={'retry_kind': kind})
    assert response.status_code == (410 if variant == 'expired' else 409), response.text
    assert response.json()['error']['code'] == code
    if reason:
        assert response.json()['error']['details']['reason'] == reason
    assert counts() == before
    assert state_of(app_client, h, session)['current_attempt_no'] == 1


def test_stat05_exact_fstat1_reads_receipt_and_real_refine_retry_replace_contribution(app_client, monkeypatch):
    h = auth_headers(register_and_login(app_client))
    original = MockLlmProvider.generate
    calls, target = [], []
    async def generated(self, request, deadline):
        scope = current_scope.get()
        if request.purpose == 'refine':
            calls.append((scope.session_id, scope.attempt_no))
            if scope.session_id == target[0]:
                if scope.attempt_no == 1:
                    return LlmResponse(None, 'mock', 'mock-llm', finish_reason='error',
                        error=ProviderError('unavailable', 'explicit fictional LLM failure'))
                return LlmResponse(None, 'mock', 'mock-llm', text='甲乙丙丁戊己庚')
        return await original(self, request, deadline)
    monkeypatch.setattr(MockLlmProvider, 'generate', generated)
    asr = app_client.app.state.sessions.asr_provider
    asr.text = '甲乙丙丁戊'
    first = create_session(app_client, h, 'r2-stat05-first').json()
    target.append(first['session_id'])
    r1 = dictation(app_client, h, first['session_id'], chunks=40)[-1]['result']
    assert r1['review_required'] is True
    asr.text = '一二三四五六七八'
    second = create_session(app_client, h, 'r2-stat05-second').json()
    r2 = dictation(app_client, h, second['session_id'], chunks=80)[-1]['result']
    claimed = claim(app_client, h, second, r2)
    assert claimed.status_code == 201
    insertion = claimed.json()['insertion_id']
    assert receipt(app_client, h, second, insertion).status_code == 200
    third = create_session(app_client, h, 'r2-stat05-third').json()
    with app_client.websocket_connect('/ws/v1/sessions/'+third['session_id']+'/audio', headers=h) as ws:
        assert ws.receive_json()['type'] == 'ready'
        for seq in range(20):
            ws.send_bytes(frame(seq))
            assert ws.receive_json()['type'] == 'audio.ack'
        cancelled = app_client.post('/api/v1/sessions/'+third['session_id']+'/cancel', headers=h)
        assert cancelled.status_code == 200 and ws.receive_json()['type'] == 'cancelled'
    day = today()
    initial = stats(app_client, h, day, day).json()
    for field, value in {'sessions': 3, 'chars': 13, 'received_audio_ms': 35000,
                         'successful_audio_ms': 30000, 'average_chars_per_minute': 26.0}.items():
        assert initial['totals'][field] == value
    before = counts()
    for _ in range(10):
        assert stats(app_client, h, day, day).json() == initial
    assert receipt(app_client, h, second, insertion).status_code == 200
    assert stats(app_client, h, day, day).json() == initial and counts() == before
    retry = app_client.post('/api/v1/sessions/'+first['session_id']+'/retry',
        json={'retry_kind': 'refine'}, headers={**h, 'Idempotency-Key': key()})
    assert retry.status_code == 202
    current = wait_state(app_client, h, first['session_id'], {'ready'})
    assert current['current_attempt_no'] == 2 and current['result']['result_id'] != r1['result_id']
    assert current['result']['raw_text'] == r1['raw_text'] and current['result']['refined_text'] == '甲乙丙丁戊己庚'
    updated = stats(app_client, h, day, day).json()['totals']
    assert (updated['sessions'], updated['chars'], updated['received_audio_ms'], updated['successful_audio_ms']) == (3, 15, 35000, 30000)
    assert updated['average_chars_per_minute'] == 30.0
    assert calls == [(first['session_id'], 1), (second['session_id'], 1), (first['session_id'], 2)]
    stale = claim(app_client, h, first, r1, review=True)
    assert stale.status_code == 409 and stale.json()['error']['code'] == 'STALE_RESULT'
    assert stats(app_client, h, day, day).json()['totals'] == updated


def test_stat08_inflight_real_api_refining_does_not_enter_rate_denominators(app_client, monkeypatch):
    tokens = register_and_login(app_client)
    h, day = auth_headers(tokens), today()
    for kwargs in [dict(chars=1, inserted=True), dict(chars=1, fallback=True),
                   dict(outcome='failed', has_result=False, counted=True),
                   dict(outcome='empty', has_result=False, counted=True),
                   dict(outcome='cancelled', has_result=False, counted=True)]:
        add_contribution(uid(tokens), sid(), day, 16000, **kwargs)
    entered = app_client.portal.call(asyncio.Event)
    release = app_client.portal.call(asyncio.Event)
    original = MockLlmProvider.generate
    async def blocked(self, request, deadline):
        if request.purpose == 'refine':
            entered.set()
            async with asyncio.timeout_at(deadline):
                await release.wait()
        return await original(self, request, deadline)
    monkeypatch.setattr(MockLlmProvider, 'generate', blocked)
    session = create_session(app_client, h, 'r2-stat08-inflight').json()
    with app_client.websocket_connect('/ws/v1/sessions/'+session['session_id']+'/audio', headers=h) as ws:
        assert ws.receive_json()['type'] == 'ready'
        ws.send_bytes(frame(0))
        assert ws.receive_json()['type'] == 'audio.ack'
        ws.send_json({'type': 'audio.end', 'last_seq': 0, 'total_samples': 4000})
        assert ws.receive_json()['type'] == 'audio.end_ack'
        try:
            app_client.portal.call(entered.wait)
            assert state_of(app_client, h, session)['state'] == 'refining'
            totals = stats(app_client, h, day, day).json()['totals']
            for field in ('processing_success_rate', 'fallback_rate', 'insertion_success_rate'):
                assert totals[field] == 0.5
        finally:
            app_client.portal.call(release.set)
        while ws.receive_json()['type'] != 'result.ready':
            pass


def test_stat09_all_zero_rate_fields_are_finite_for_new_and_only_cancelled_user(app_client):
    h = auth_headers(register_and_login(app_client))
    day = today()
    def zero():
        response = stats(app_client, h, day, day)
        assert response.status_code == 200 and 'NaN' not in response.text and 'Infinity' not in response.text
        totals = response.json()['totals']
        for field in ('average_chars_per_minute', 'processing_success_rate', 'fallback_rate', 'insertion_success_rate'):
            assert totals[field] == 0 and math.isfinite(totals[field])
    zero()
    session = create_session(app_client, h, 'r2-stat09-cancel').json()
    assert app_client.post('/api/v1/sessions/'+session['session_id']+'/cancel', headers=h).status_code == 200
    zero()
