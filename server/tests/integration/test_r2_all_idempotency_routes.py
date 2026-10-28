"""Original TC-API-17: real API/PG, declared Mock physical providers only."""
from __future__ import annotations

import time

import pytest

from app.providers.asr.base import ProviderError
from tests.conftest import auth_headers, register_and_login
from tests.cases.test_api_cases import dictation_ready, state_of
from tests.cases.test_dict_cases import create_term, fetch_one, key
from tests.cases.test_learn_cases import (
    attach_insertion, list_dict, prepare_inserted, set_classifier, submit, wait_event,
)
from tests.test_sessions import create_session, dictation


def conflict(response):
    assert response.status_code == 409, response.text
    assert response.json()['error']['code'] == 'IDEMPOTENCY_CONFLICT'


def snapshot():
    """Read persisted side effects, including aggregate and logical versions."""
    return tuple(fetch_one(query) for query in (
        'select count(*) from sessions', 'select count(*) from session_attempts',
        'select count(*) from results', 'select count(*) from insertions',
        'select count(*) from learning_events', 'select count(*) from operation_requests',
        'select count(*) from dictionary_terms',
        'select coalesce(sum(version),0) from dictionary_terms',
        'select coalesce(sum(version),0) from dictionary_state',
        'select coalesce(sum(inserted_sessions),0) from usage_daily',
        'select coalesce(sum(learning_events),0) from usage_daily',
    ))


def wait_ready(client, headers, session):
    until = time.monotonic()+10
    while time.monotonic() < until:
        body = state_of(client, headers, session)
        if body['state'] == 'ready':
            return body
        assert body['state'] in ('finalizing', 'refining'), body
        time.sleep(0.02)
    raise AssertionError('retry did not reach ready')


def observe_classifier(client, monkeypatch, decision='term', ambiguous=False):
    set_classifier(client, monkeypatch, decision=decision, ambiguous=ambiguous)
    actual = client.app.state.classifier.classify
    calls = []
    async def observed(**payload):
        calls.append(payload)
        return await actual(**payload)
    monkeypatch.setattr(client.app.state.classifier, 'classify', observed)
    return calls


def test_api17_create_replay_returns_current_ready_without_new_open(app_client, monkeypatch):
    headers = auth_headers(register_and_login(app_client))
    provider = app_client.app.state.sessions.asr_provider
    original, opens = provider.open, []
    async def observed(options):
        opens.append((options.session_id, options.attempt_no))
        return await original(options)
    monkeypatch.setattr(provider, 'open', observed)
    ident = key()
    payload = {'client_request_id': ident, 'learning_enabled': True}
    path = '/api/v1/sessions'
    first = app_client.post(path, json=payload, headers={**headers, 'Idempotency-Key': ident})
    assert first.status_code == 201
    session = first.json()
    result = dictation(app_client, headers, session['session_id'])[-1]['result']
    before = snapshot()
    for _ in range(3):
        replay = app_client.post(path, json=payload, headers={**headers, 'Idempotency-Key': ident})
        assert replay.status_code == 200, replay.text
        current = replay.json()
        assert current['session_id'] == session['session_id'] and current['state'] == 'ready'
        assert current['current_attempt_no'] == 1 and current['replayed'] is True
        assert current['result'] == result
        assert snapshot() == before and len(opens) == 1
    conflict(app_client.post(path, json={**payload, 'learning_enabled': False},
        headers={**headers, 'Idempotency-Key': ident}))
    assert snapshot() == before and len(opens) == 1


def test_api17_retry_replay_after_completion_does_not_create_third_attempt(app_client, monkeypatch):
    headers = auth_headers(register_and_login(app_client))
    provider = app_client.app.state.sessions.asr_provider
    original, opens = provider.open, []
    async def observed(options):
        stream = await original(options)
        opens.append((options.session_id, options.attempt_no))
        if options.attempt_no == 1:
            async def fail(total_samples, deadline):
                raise ProviderError('ASR_FAILED', 'explicit fictional first-attempt failure')
            stream.finish = fail
        return stream
    monkeypatch.setattr(provider, 'open', observed)
    session = create_session(app_client, headers, 'api17-retry').json()
    events = dictation(app_client, headers, session['session_id'])
    assert events[-1]['type'] == 'error' and events[-1]['error']['code'] == 'ASR_FAILED'
    assert state_of(app_client, headers, session)['retry_kind'] == 'asr'
    path = '/api/v1/sessions/'+session['session_id']+'/retry'
    ident, payload = key(), {'retry_kind': 'asr'}
    first = app_client.post(path, json=payload, headers={**headers, 'Idempotency-Key': ident})
    assert first.status_code == 202, first.text
    ready = wait_ready(app_client, headers, session)
    assert ready['current_attempt_no'] == 2
    before = snapshot()
    for _ in range(3):
        replay = app_client.post(path, json=payload, headers={**headers, 'Idempotency-Key': ident})
        assert replay.status_code == 200, replay.text
        current = replay.json()
        assert current['replayed'] is True and current['state'] == 'ready'
        assert current['session_id'] == session['session_id'] and current['current_attempt_no'] == 2
        assert current['result'] == ready['result']
        assert snapshot() == before and len(opens) == 2
    conflict(app_client.post(path, json={'retry_kind': 'refine'},
        headers={**headers, 'Idempotency-Key': ident}))
    assert snapshot() == before and [attempt for _, attempt in opens] == [1, 2]
    assert fetch_one('select count(*) from session_attempts') == 2
    assert fetch_one('select count(*) from results') == 1


def test_api17_claim_replay_reads_current_inserted_and_changed_body_conflicts(app_client):
    headers = auth_headers(register_and_login(app_client))
    session, result = dictation_ready(app_client, headers, 'api17-claim')
    ident = key()
    path = '/api/v1/sessions/'+session['session_id']+'/insertions/'
    payload = {'result_id': result['result_id'], 'insertion_id': ident,
        'text_hash': result['text_hash'], 'review_confirmed': False}
    first = app_client.post(path+'claim', json=payload, headers=headers)
    assert first.status_code == 201
    receipt = app_client.post(path+ident+'/receipt', headers=headers,
        json={'status': 'inserted', 'observable': True, 'error_code': None})
    assert receipt.status_code == 200
    before = snapshot()
    for _ in range(3):
        replay = app_client.post(path+'claim', json=payload, headers=headers)
        assert replay.status_code == 200
        assert replay.json()['insertion_id'] == ident and replay.json()['replayed'] is True
        assert replay.json()['status'] == 'inserted' and snapshot() == before
    conflict(app_client.post(path+'claim', json={**payload, 'review_confirmed': True}, headers=headers))
    assert snapshot() == before
    assert fetch_one('select sum(inserted_sessions) from usage_daily') == 1


def test_api17_correction_replay_returns_learned_without_second_classify(app_client, monkeypatch):
    calls = observe_classifier(app_client, monkeypatch)
    headers, session, result, insertion = prepare_inserted(app_client,
        '请使用 SQL lite 数据库。', 'api17-correction')
    attach_insertion(session, insertion)
    ident = key()
    first = submit(app_client, headers, session, result, 'SQL lite', 'SQLite', correction_id=ident)
    assert first.status_code == 202
    event = wait_event(app_client, headers, first.json()['event_id'])
    assert event['status'] == 'learned' and len(calls) == 1
    before = snapshot()
    for _ in range(3):
        replay = submit(app_client, headers, session, result, 'SQL lite', 'SQLite', correction_id=ident)
        assert replay.status_code == 200 and replay.json()['replayed'] is True
        assert replay.json()['event_id'] == event['event_id'] and replay.json()['status'] == 'learned'
        assert snapshot() == before and len(calls) == 1
    conflict(submit(app_client, headers, session, result, 'SQL lite', 'SQLiteX', correction_id=ident))
    assert snapshot() == before and len(calls) == 1


@pytest.mark.parametrize('operation', ['confirm', 'undo'])
def test_api17_confirm_undo_key_also_binds_real_event_path(app_client, monkeypatch, operation):
    suggest = operation == 'confirm'
    calls = observe_classifier(app_client, monkeypatch,
        decision='suggest' if suggest else 'term', ambiguous=suggest)
    headers, session, result, insertion = prepare_inserted(app_client,
        '请用 cloud 和 SQL lite 保存。', 'api17-'+operation)
    attach_insertion(session, insertion)
    events = []
    for before, after in [('cloud', 'Claude'), ('SQL lite', 'SQLite')]:
        response = submit(app_client, headers, session, result, before, after)
        assert response.status_code == 202
        event = wait_event(app_client, headers, response.json()['event_id'])
        assert event['status'] == ('suggested' if suggest else 'learned'), event
        events.append(event)
    ident = key()
    path = '/api/v1/learning-events/'+events[0]['event_id']+'/'+operation
    first = app_client.post(path, headers={**headers, 'Idempotency-Key': ident})
    assert first.status_code == 200
    if suggest:
        assert first.json()['status'] == 'learned'
    else:
        assert first.json()['undone'] is True
    before = snapshot()
    for _ in range(3):
        replay = app_client.post(path, headers={**headers, 'Idempotency-Key': ident})
        assert replay.status_code == 200 and replay.json()['replayed'] is True
        assert replay.json()['event_id'] == events[0]['event_id']
        assert snapshot() == before and len(calls) == 2
    conflict(app_client.post('/api/v1/learning-events/'+events[1]['event_id']+'/'+operation,
        headers={**headers, 'Idempotency-Key': ident}))
    assert snapshot() == before and len(calls) == 2
    other = app_client.get('/api/v1/learning-events/'+events[1]['event_id'], headers=headers).json()
    assert other['status'] == ('suggested' if suggest else 'learned') and not other['undone']
    assert [item['term'] for item in list_dict(app_client, headers)['items']] == (
        ['Claude'] if suggest else ['SQLite'])
    assert fetch_one('select sum(learning_events) from usage_daily') == (1 if suggest else 2)


@pytest.mark.parametrize('operation', ['create', 'patch', 'delete', 'batch-delete', 'import'])
def test_api17_dictionary_all_write_routes_replay_current_without_mutation(app_client, operation):
    headers = auth_headers(register_and_login(app_client))
    ident = key()
    base = '/api/v1/dictionary'
    if operation == 'create':
        payload = {'term': 'TypeScript', 'aliases': ['type script'], 'scope': 'default'}
        def send(changed=False):
            return app_client.post(base+'/terms', json={**payload, 'term': 'Docker'} if changed else payload,
                headers={**headers, 'Idempotency-Key': ident})
        first = send()
        assert first.status_code == 201
        target = first.json()['id']
        changed = app_client.patch(base+'/terms/'+target, json={'version': 1, 'aliases': ['ts']},
            headers={**headers, 'Idempotency-Key': key()})
        assert changed.status_code == 200 and changed.json()['version'] == 2
        expected = changed.json()
        fields = ('id', 'term', 'aliases', 'version', 'dictionary_version')
    elif operation == 'patch':
        created = create_term(app_client, headers, 'TypeScript', ['type script']).json()
        path = base+'/terms/'+created['id']
        payload = {'version': 1, 'aliases': ['ts']}
        def send(changed=False):
            return app_client.patch(path, json={**payload, 'aliases': ['different']} if changed else payload,
                headers={**headers, 'Idempotency-Key': ident})
        first = send()
        assert first.status_code == 200
        changed = app_client.patch(path, json={'version': 2, 'aliases': ['ts', 'type script']},
            headers={**headers, 'Idempotency-Key': key()})
        assert changed.status_code == 200 and changed.json()['version'] == 3
        expected, fields = changed.json(), ('id', 'aliases', 'version', 'dictionary_version')
    elif operation == 'delete':
        created = create_term(app_client, headers, 'TypeScript').json()
        def send(changed=False):
            return app_client.delete(base+'/terms/'+created['id'], params={'version': 2 if changed else 1},
                headers={**headers, 'Idempotency-Key': ident})
        first = send()
        assert first.status_code == 200 and first.json()['deleted'] is True
        expected, fields = first.json(), ('id', 'deleted', 'version', 'dictionary_version')
    elif operation == 'batch-delete':
        created = [create_term(app_client, headers, term).json() for term in ('TypeScript', 'Docker')]
        payload = {'items': [{'id': item['id'], 'version': 1} for item in created]}
        def send(changed=False):
            items = [dict(item, version=2) for item in payload['items']] if changed else list(reversed(payload['items']))
            return app_client.post(base+'/terms/batch-delete', json={'items': items},
                headers={**headers, 'Idempotency-Key': ident})
        first = app_client.post(base+'/terms/batch-delete', json=payload,
            headers={**headers, 'Idempotency-Key': ident})
        assert first.status_code == 200
        expected, fields = first.json(), ('deleted_ids', 'dictionary_version')
    else:
        raw = b'term,aliases\nTypeScript,type script\n'
        def send(changed=False):
            return app_client.post(base+'/import',
                files={'file': ('terms.csv', b'term,aliases\nDocker,container\n' if changed else raw, 'text/csv')},
                headers={**headers, 'Idempotency-Key': ident})
        first = send()
        assert first.status_code == 200 and first.json()['created'] == 1
        added = create_term(app_client, headers, 'Docker')
        assert added.status_code == 201
        expected = {**first.json(), 'dictionary_version': added.json()['dictionary_version']}
        fields = ('import_id', 'created', 'skipped', 'conflicts', 'invalid', 'rows', 'dictionary_version')
    assert first.json()['replayed'] is False
    before = snapshot()
    for _ in range(3):
        replay = send()
        assert replay.status_code == 200, replay.text
        assert replay.json()['replayed'] is True
        for field in fields:
            assert replay.json()[field] == expected[field], field
        assert snapshot() == before
    conflict(send(changed=True))
    assert snapshot() == before
    if operation in ('delete', 'batch-delete'):
        assert list_dict(app_client, headers)['items'] == []
    if operation == 'import':
        assert fetch_one('select count(*) from dictionary_imports') == 1
        assert fetch_one('select count(*) from dictionary_import_rows') == 1
