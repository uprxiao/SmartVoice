"""Supplement exact existing API parameters; physical providers are Mock.

Copied to a new server test module only after frozen inference/control queues.
No browser, microphone, native write, or model-quality claims.
"""
import json
from uuid import uuid4

import pytest

from app.services.capacity import CapacityError
from app.services.classifier import Classification
from tests.conftest import auth_headers, register_and_login
from tests.cases.test_api_cases import (
    assert_error, claim, dictation_ready, drain_until_close, state_of, ws_session,
)
from tests.cases.test_dict_cases import fetch_one, key, new_user
from tests.cases.test_learn_cases import (
    attach_insertion, list_dict, prepare_inserted, submit,
)
from tests.test_sessions import frame


def no_result(client, headers, session):
    for _ in range(2):
        body = state_of(client, headers, session)
        assert body['state'] == 'failed' and body['result'] is None
        assert body['current_result_id'] is None
    assert fetch_one('select count(*) from results') == 0
    assert fetch_one('select count(*) from insertions') == 0
    assert fetch_one('select count(*) from learning_events') == 0


@pytest.mark.parametrize('last_seq,total', [(0, 8000), (1, 4000)],
    ids=['wrong-last-seq-only', 'wrong-total-only'])
def test_api03_each_end_metadata_field_independently(app_client, last_seq, total):
    headers = auth_headers(register_and_login(app_client))
    session, connection = ws_session(app_client, headers, 'exact-bad-end')
    with connection as ws:
        assert ws.receive_json()['type'] == 'ready'
        for seq in (0, 1):
            ws.send_bytes(frame(seq))
            while ws.receive_json()['type'] != 'audio.ack':
                pass
        ws.send_json({'type': 'audio.end', 'last_seq': last_seq, 'total_samples': total})
        events, code = drain_until_close(ws)
    assert code == 4400
    assert_error(events, 'AUDIO_PROTOCOL_ERROR')
    assert not any(e['type'] in ('audio.end_ack', 'result.ready') for e in events)
    no_result(app_client, headers, session)


@pytest.mark.parametrize('invalid', ['offset', 'left', 'right'])
def test_api15_position_rejects_before_any_classification(app_client, monkeypatch, invalid):
    headers, session, result, insertion = prepare_inserted(
        app_client, '帮我加一个按钮，不要修改数据库。', 'r2-invalid-position')
    attach_insertion(session, insertion)
    calls = []
    async def classifier(**payload):
        calls.append(payload)
        return Classification()
    monkeypatch.setattr(app_client.app.state.classifier, 'classify', classifier)
    offset = result['refined_text'].index('按钮')
    changes = {'offset': offset+1} if invalid == 'offset' else {invalid: '错误上下文'}
    response = submit(app_client, headers, session, result, '按钮', '按键', extra=changes)
    assert response.status_code == 422 and response.json()['error']['code'] == 'VALIDATION_ERROR'
    assert not calls
    assert fetch_one('select count(*) from learning_events') == 0
    assert fetch_one('select count(*) from dictionary_terms') == 0
    assert fetch_one("select count(*) from model_calls where purpose='classify'") == 0


def test_api16_confirm_ignored_and_missing_does_not_bypass_guard(app_client, monkeypatch):
    headers, session, result, insertion = prepare_inserted(
        app_client, '帮我加一个按钮，不要修改数据库。', 'r2-confirm-guard')
    attach_insertion(session, insertion)
    calls = []
    async def classifier(**payload):
        calls.append(payload)
        return Classification()
    monkeypatch.setattr(app_client.app.state.classifier, 'classify', classifier)
    ignored = submit(app_client, headers, session, result, '一', '二')
    assert ignored.status_code == 202 and ignored.json()['status'] == 'ignored'
    for ident, status, code in [(ignored.json()['event_id'], 409, 'LEARNING_CONFLICT'),
                                (str(uuid4()), 404, 'NOT_FOUND')]:
        response = app_client.post('/api/v1/learning-events/'+ident+'/confirm',
            headers={**headers, 'Idempotency-Key': key()})
        assert response.status_code == status and response.json()['error']['code'] == code
    again = app_client.get('/api/v1/learning-events/'+ignored.json()['event_id'], headers=headers)
    assert again.status_code == 200 and again.json()['status'] == 'ignored'
    assert not calls and list_dict(app_client, headers)['items'] == []
    assert fetch_one("select count(*) from learning_events where status='learned'") == 0


def test_api06_replayed_create_has_no_new_runtime_open_or_task(app_client, monkeypatch):
    users = [new_user(app_client, f'exact-create-{i}@example.test', f'exact-create-d{i}') for i in (1, 2)]
    h1, h2 = map(auth_headers, users)
    manager = app_client.app.state.sessions
    provider = manager.asr_provider
    original, opens = provider.open, []
    async def observed(options):
        opens.append((options.user_id, options.session_id))
        return await original(options)
    monkeypatch.setattr(provider, 'open', observed)
    ident = key()
    payload = {'client_request_id': ident, 'mode': 'dictation', 'learning_enabled': False}
    first = app_client.post('/api/v1/sessions', json=payload, headers={**h1, 'Idempotency-Key': ident})
    assert first.status_code == 201
    body = first.json()
    runtime = manager.runtimes[body['session_id']]
    for _ in range(3):
        replay = app_client.post('/api/v1/sessions', json=payload, headers={**h1, 'Idempotency-Key': ident})
        assert replay.status_code == 200 and replay.json()['replayed'] is True
        assert all(replay.json()[k] == body[k] for k in ('session_id', 'state', 'current_attempt_no'))
        assert manager.runtimes[body['session_id']] is runtime
        assert len(opens) == 1 and len(manager.runtimes) == 1 and not manager._processing_tasks
    conflict = app_client.post('/api/v1/sessions', json={**payload, 'learning_enabled': True},
        headers={**h1, 'Idempotency-Key': ident})
    assert conflict.status_code == 409 and conflict.json()['error']['code'] == 'IDEMPOTENCY_CONFLICT'
    other = app_client.post('/api/v1/sessions', json=payload, headers={**h2, 'Idempotency-Key': ident})
    assert other.status_code == 201 and other.json()['session_id'] != body['session_id']
    assert len(opens) == 2 and len(manager.runtimes) == 2 and not manager._processing_tasks
    assert fetch_one('select count(*) from sessions') == 2
    assert fetch_one('select count(*) from session_attempts') == 2
    assert fetch_one('select count(*) from results') == 0


def test_api07_receipt_replay_and_rejection_keeps_one_aggregate(app_client):
    headers = auth_headers(register_and_login(app_client))
    session, result = dictation_ready(app_client, headers, 'exact-receipt')
    path = '/api/v1/sessions/'+session['session_id']+'/insertions/'
    receipt = {'status': 'inserted', 'observable': True, 'error_code': None}
    missing = app_client.post(path+key()+'/receipt', json=receipt, headers=headers)
    assert missing.status_code == 404 and missing.json()['error']['code'] == 'NOT_FOUND'
    response = claim(app_client, headers, session, result)
    assert response.status_code == 201
    ident = response.json()['insertion_id']
    for _ in range(3):
        response = app_client.post(path+ident+'/receipt', json=receipt, headers=headers)
        assert response.status_code == 200 and response.json()['status'] == 'inserted'
    conflict = app_client.post(path+ident+'/receipt', json={'status': 'failed',
        'observable': False, 'error_code': 'ZERO_WRITE'}, headers=headers)
    assert conflict.status_code == 409 and conflict.json()['error']['code'] == 'INSERTION_STATE_CONFLICT'
    assert fetch_one('select count(*) from insertions') == 1
    assert fetch_one('select sum(inserted_sessions) from usage_daily') == 1
    assert fetch_one('select count(*) from session_contributions where inserted') == 1
    assert state_of(app_client, headers, session)['insertion_status'] == 'inserted'


def test_api20_short_tail_then_normal_frame_rejected_and_closed(app_client):
    headers = auth_headers(register_and_login(app_client))
    session, connection = ws_session(app_client, headers, 'exact-short-tail')
    with connection as ws:
        assert ws.receive_json()['type'] == 'ready'
        ws.send_bytes(frame(0, 123))
        assert ws.receive_json()['type'] == 'audio.ack'
        ws.send_bytes(frame(1, 4000))
        events, code = drain_until_close(ws)
    assert code == 4400
    assert_error(events, 'AUDIO_PROTOCOL_ERROR')
    assert not any(e['type'] in ('audio.ack', 'result.ready') for e in events)
    no_result(app_client, headers, session)
    assert state_of(app_client, headers, session)['received_samples'] == 123


@pytest.mark.parametrize('variant', ['binary-8009', 'control-4097', 'invalid-json', 'unknown-field'])
def test_api22_all_envelope_variants_have_exact_close_and_no_result(app_client, variant):
    headers = auth_headers(register_and_login(app_client))
    session, connection = ws_session(app_client, headers, 'exact-envelope')
    with connection as ws:
        assert ws.receive_json()['type'] == 'ready'
        ws.send_bytes(frame(0))
        while ws.receive_json()['type'] != 'audio.ack':
            pass
        if variant == 'binary-8009':
            ws.send_bytes(b'\x00'*8009)
        elif variant == 'control-4097':
            value = ' '*4097
            assert len(value.encode()) == 4097
            ws.send_text(value)
        elif variant == 'invalid-json':
            ws.send_text('{')
        else:
            ws.send_json({'type': 'audio.end', 'last_seq': 0, 'total_samples': 4000, 'extra': True})
        events, code = drain_until_close(ws)
    large = variant in ('binary-8009', 'control-4097')
    assert code == (1009 if large else 4400)
    assert_error(events, 'MESSAGE_TOO_LARGE' if large else 'AUDIO_PROTOCOL_ERROR')
    assert not any(e['type'] in ('audio.end_ack', 'result.ready') for e in events)
    assert 'Traceback' not in json.dumps(events)
    no_result(app_client, headers, session)


@pytest.mark.parametrize('variant,status,code', [
    ('unauth', 401, 'AUTH_INVALID'), ('schema', 422, 'VALIDATION_ERROR'),
    ('capacity', 429, 'CAPACITY_EXCEEDED'), ('invalid-json', 400, 'INVALID_JSON'),
    ('content-type', 415, 'UNSUPPORTED_MEDIA_TYPE'),
])
def test_api18_error_envelopes_at_create_endpoint(app_client, monkeypatch, variant, status, code):
    headers = auth_headers(register_and_login(app_client))
    ident = key()
    headers = {**headers, 'Idempotency-Key': ident}
    payload = {'client_request_id': ident, 'mode': 'dictation', 'learning_enabled': False}
    if variant == 'capacity':
        async def full(db, *, locked_capacity=None):
            assert locked_capacity is not None
            raise CapacityError('active_limit')
        monkeypatch.setattr(app_client.app.state.sessions.capacity, 'admit', full)
    if variant in ('invalid-json', 'content-type'):
        response = app_client.post('/api/v1/sessions', content='{' if variant == 'invalid-json' else json.dumps(payload),
            headers={**headers, 'Content-Type': 'application/json' if variant == 'invalid-json' else 'text/plain'})
    else:
        if variant == 'unauth':
            headers.pop('Authorization')
        response = app_client.post('/api/v1/sessions',
            json={'client_request_id': 'x'} if variant == 'schema' else payload, headers=headers)
    assert response.status_code == status
    error = response.json()['error']
    assert error['code'] == code and error['request_id']
    assert 'Traceback' not in response.text
    assert fetch_one('select count(*) from sessions') == 0
