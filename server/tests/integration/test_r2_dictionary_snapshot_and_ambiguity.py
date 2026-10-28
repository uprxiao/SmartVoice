"""Whole API dictionary controls; declared Mock models, not audio quality."""
from __future__ import annotations

from copy import deepcopy

import pytest

from app.providers.llm.mock import MockLlmProvider
from app.services.inference_context import current_scope
from app.services.r2_prompt import R2_SYSTEM, R2_VERSION
from tests.conftest import auth_headers
from tests.cases.test_api_cases import claim, dictation_ready
from tests.cases.test_dict_cases import create_term, fetch_one, key, new_user
from tests.cases.test_learn_cases import attach_insertion, list_dict, set_classifier, submit, wait_event
from tests.test_sessions import create_session, dictation


def spy_inputs(client, monkeypatch):
    provider = client.app.state.sessions.asr_provider
    actual_open, actual_generate = provider.open, MockLlmProvider.generate
    opens, requests = {}, {}
    async def opened(options):
        opens[options.session_id] = deepcopy(options)
        return await actual_open(options)
    async def generated(self, request, deadline):
        if request.purpose == 'refine':
            scope = current_scope.get()
            requests.setdefault(scope.session_id, []).append(deepcopy(request))
            assert request.system_prompt == R2_SYSTEM
            assert request.system_prompt_version == R2_VERSION and request.output_mode == 'text'
        return await actual_generate(self, request, deadline)
    monkeypatch.setattr(provider, 'open', opened)
    monkeypatch.setattr(MockLlmProvider, 'generate', generated)
    return opens, requests


def test_dict15_real_wire_inputs_stay_version4_then5_and_separate_users(app_client, monkeypatch):
    opens, requests = spy_inputs(app_client, monkeypatch)
    h1 = auth_headers(new_user(app_client, 'r2-dict15-u1@example.test', 'r2-dict15-d1'))
    h2 = auth_headers(new_user(app_client, 'r2-dict15-u2@example.test', 'r2-dict15-d2'))
    for term, aliases in [('React', ['reactt']), ('Docker', ['container']),
                          ('TypeScript', ['ts']), ('CloudKit', ['clowd'])]:
        assert create_term(app_client, h1, term, aliases).status_code == 201
    assert create_term(app_client, h2, 'UserOnly', ['reactt']).status_code == 201
    raw = 'reactt、SQL lite。'
    app_client.app.state.sessions.asr_provider.text = raw
    s1 = create_session(app_client, h1, 'r2-freeze-v4').json()
    frozen_open = deepcopy(opens[s1['session_id']])
    added = create_term(app_client, h1, 'SQLite', ['SQL lite'])
    assert added.status_code == 201 and added.json()['dictionary_version'] == 5
    r1 = dictation(app_client, h1, s1['session_id'])[-1]['result']
    s2 = create_session(app_client, h1, 'r2-freeze-v5').json()
    r2 = dictation(app_client, h1, s2['session_id'])[-1]['result']
    s3 = create_session(app_client, h2, 'r2-freeze-other').json()
    r3 = dictation(app_client, h2, s3['session_id'])[-1]['result']
    for session, result, version, expected_terms, output in [
        (s1, r1, 4, {'React'}, 'React、SQL lite。'),
        (s2, r2, 5, {'React', 'SQLite'}, 'React、SQLite。'),
        (s3, r3, 1, {'UserOnly'}, 'UserOnly、SQL lite。'),
    ]:
        sid = session['session_id']
        assert result['dictionary_version'] == version and result['raw_text'] == raw
        assert result['refined_text'] == output and result['review_required'] is False
        assert len(requests[sid]) == 1
        assert requests[sid][0].input == {'transcript': raw,
            'confirmed_terms': requests[sid][0].input['confirmed_terms']}
        assert set(requests[sid][0].input['confirmed_terms']) == expected_terms
        assert fetch_one('select dictionary_version from sessions where id=$1', sid) == version
    assert opens[s1['session_id']] == frozen_open
    assert set(frozen_open.hint_terms) == {'React', 'Docker', 'TypeScript', 'CloudKit'}
    assert set(opens[s2['session_id']].hint_terms) == set(frozen_open.hint_terms) | {'SQLite'}
    assert opens[s3['session_id']].hint_terms == ['UserOnly']
    assert fetch_one('select count(*) from results') == 3


@pytest.mark.parametrize('decision,ambiguous', [('term', False), ('suggest', True)])
def test_dict16_shared_alias_neither_replaced_nor_auto_learned(app_client, monkeypatch, decision, ambiguous):
    _, requests = spy_inputs(app_client, monkeypatch)
    h = auth_headers(new_user(app_client, 'r2-ambiguous@example.test', 'r2-ambiguous-device'))
    for term in ('Claude', 'Claw'):
        assert create_term(app_client, h, term, ['cloud']).status_code == 201
    before = list_dict(app_client, h)
    assert all('cloud' in item['ambiguous_aliases'] for item in before['items'])
    app_client.app.state.sessions.asr_provider.text = '请用 cloud 保存。'
    session, result = dictation_ready(app_client, h, 'r2-shared-alias')
    assert result['raw_text'] == result['refined_text'] == '请用 cloud 保存。'
    assert set(requests[session['session_id']][0].input['confirmed_terms']) == {'Claude', 'Claw'}
    claimed = claim(app_client, h, session, result)
    assert claimed.status_code == 201
    insertion = claimed.json()['insertion_id']
    receipt = app_client.post('/api/v1/sessions/'+session['session_id']+'/insertions/'+insertion+'/receipt',
        json={'status': 'inserted', 'observable': True, 'error_code': None}, headers=h)
    assert receipt.status_code == 200
    attach_insertion(session, insertion)
    set_classifier(app_client, monkeypatch, decision=decision, ambiguous=ambiguous)
    response = submit(app_client, h, session, result, 'cloud', 'Claude')
    assert response.status_code == 202
    event = wait_event(app_client, h, response.json()['event_id'])
    assert event['status'] == ('failed' if decision == 'term' else 'suggested')
    if decision == 'term':
        assert event['reason'] == 'ALIAS_CONFLICT'
    else:
        confirmed = app_client.post('/api/v1/learning-events/'+event['event_id']+'/confirm',
            headers={**h, 'Idempotency-Key': key()})
        assert confirmed.status_code == 409 and confirmed.json()['error']['code'] == 'LEARNING_CONFLICT'
        assert confirmed.json()['error']['details']['reason'] == 'ALIAS_CONFLICT'
    assert list_dict(app_client, h) == before
    assert fetch_one("select count(*) from learning_events where status='learned'") == 0
    assert fetch_one('select coalesce(sum(learning_events),0) from usage_daily') == 0
    current = app_client.get('/api/v1/sessions/'+session['session_id'], headers=h).json()
    assert current['result'] == result


@pytest.mark.parametrize('raw,expected', [
    ('请用 cloud 保存，cloudy不改。', '请用 claw 保存，cloudy不改。'),
    ('cloud 和 CLOUD，cloudy不改。', 'claw 和 claw，cloudy不改。'),
])
def test_dict07_unique_alias_one_and_two_occurrences_through_r2_api(app_client, monkeypatch, raw, expected):
    spy_inputs(app_client, monkeypatch)
    h = auth_headers(new_user(app_client, 'r2-unique@example.test', 'r2-unique-device'))
    assert create_term(app_client, h, 'claw', ['cloud']).status_code == 201
    app_client.app.state.sessions.asr_provider.text = raw
    session, result = dictation_ready(app_client, h, 'r2-unique-alias')
    assert result['raw_text'] == raw and result['refined_text'] == expected
    assert result['review_required'] is False and result['dictionary_version'] == 1
    current = app_client.get('/api/v1/sessions/'+session['session_id'], headers=h).json()
    assert current['result'] == result and list_dict(app_client, h)['dictionary_version'] == 1
