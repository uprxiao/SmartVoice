"""Database/API regressions; prepared offline, not an E2E acceptance substitute."""
from __future__ import annotations

import asyncio
import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from uuid import uuid4

import asyncpg
import pytest
from sqlalchemy import select

from app.models import OperationRequest, User
from app.services import cleanup, idempotency, learning_service
from app.services.retention import content_expired
from app.services.classifier import Classification
from tests.conftest import auth_headers, execute_sql, register_and_login
from tests.cases.test_dict_cases import create_term, fetch_one, login
from tests.cases.test_learn_cases import (
    prepare_inserted, set_classifier, submit, wait_event,
)
from tests.test_insertions_learning import claim, complete_dictation, receipt
from tests.test_sessions import create_session, dictation


def key():
    return str(uuid4())


def concurrent(*calls):
    gate = Barrier(len(calls))

    def run(call):
        gate.wait(timeout=10)
        return call()

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        return list(pool.map(run, calls))


def test_claim_is_single_winner_under_concurrent_requests(app_client):
    headers, session, result = complete_dictation(app_client, key())
    responses = concurrent(*[
        lambda: claim(app_client, headers, session['session_id'], result, key())
        for _ in range(2)
    ])
    assert sorted(r.status_code for r in responses) == [201, 409]
    loser = next(r for r in responses if r.status_code == 409)
    assert loser.json()['error']['code'] == 'INSERTION_ALREADY_CLAIMED'
    assert fetch_one('select count(*) from insertions where session_id=$1', session['session_id']) == 1


def test_receipt_terminal_cas_and_nonobservable_insert_count(app_client):
    headers, session, result = complete_dictation(app_client, key())
    insertion = claim(app_client, headers, session['session_id'], result, key()).json()['insertion_id']
    responses = concurrent(
        lambda: receipt(app_client, headers, session['session_id'], insertion, 'inserted', False),
        lambda: receipt(app_client, headers, session['session_id'], insertion, 'failed', False, 'ZERO_WRITE'),
    )
    assert sorted(r.status_code for r in responses) == [200, 409]
    winner = next(r for r in responses if r.status_code == 200).json()
    loser = next(r for r in responses if r.status_code == 409)
    assert loser.json()['error']['code'] == 'INSERTION_STATE_CONFLICT'
    assert fetch_one('select status from insertions where id=$1', insertion) == winner['status']
    assert fetch_one('select inserted from session_contributions where session_id=$1', session['session_id']) == (winner['status'] == 'inserted')
    replay = receipt(app_client, headers, session['session_id'], insertion,
                     winner['status'], False, winner['error_code'])
    assert replay.status_code == 200 and replay.json()['replayed'] is True


def test_claim_and_retry_cannot_both_win(app_client):
    headers, session, result = complete_dictation(app_client, key())
    execute_sql("update results set review_required=true, review_reason='llm_unavailable' where id=$1", result['result_id'])
    claimed, retried = concurrent(
        lambda: claim(app_client, headers, session['session_id'], result, key(), review_confirmed=True),
        lambda: app_client.post(f"/api/v1/sessions/{session['session_id']}/retry",
            json={'retry_kind': 'refine'}, headers={**headers, 'Idempotency-Key': key()}),
    )
    if claimed.status_code == 201:
        assert retried.status_code == 409
        assert retried.json()['error']['code'] == 'RETRY_UNAVAILABLE'
        assert retried.json()['error']['details']['reason'] == 'insertion_reserved'
    else:
        assert claimed.status_code == 409
        assert claimed.json()['error']['code'] == 'STALE_RESULT'
        assert retried.status_code == 202
    assert fetch_one("select count(*) from insertions i join sessions s on s.id=i.session_id and s.user_id=i.user_id where i.status='reserved' and i.result_id<>s.current_result_id") == 0


def test_dictionary_same_key_is_one_resource_and_cross_path_conflicts(app_client):
    headers = auth_headers(register_and_login(app_client))
    idem = key()
    first, second = concurrent(
        lambda: create_term(app_client, headers, 'React', idem=idem),
        lambda: create_term(app_client, headers, 'React', idem=idem),
    )
    assert sorted([first.status_code, second.status_code]) == [200, 201]
    assert first.json()['id'] == second.json()['id']
    assert sum(r.json()['replayed'] for r in (first, second)) == 1
    other = create_term(app_client, headers, 'TypeScript').json()
    patch_key = key()
    original = first.json()
    no_op = app_client.patch(f"/api/v1/dictionary/terms/{original['id']}",
        json={'version': original['version']}, headers={**headers, 'Idempotency-Key': patch_key})
    assert no_op.status_code == 200
    crossed = app_client.patch(f"/api/v1/dictionary/terms/{other['id']}",
        json={'version': other['version']}, headers={**headers, 'Idempotency-Key': patch_key})
    assert crossed.status_code == 409
    assert crossed.json()['error']['code'] == 'IDEMPOTENCY_CONFLICT'


def test_dictionary_version_cas_and_settings_version_cas(app_client):
    headers = auth_headers(register_and_login(app_client))
    term = create_term(app_client, headers, 'React').json()
    responses = concurrent(*[
        lambda value=value: app_client.patch(f"/api/v1/dictionary/terms/{term['id']}",
            json={'version': term['version'], 'term': value},
            headers={**headers, 'Idempotency-Key': key()})
        for value in ('ReactJS', 'ReactNative')
    ])
    assert sorted(r.status_code for r in responses) == [200, 409]
    assert next(r for r in responses if r.status_code == 409).json()['error']['code'] == 'VERSION_CONFLICT'
    settings = app_client.get('/api/v1/settings', headers=headers).json()
    responses = concurrent(*[
        lambda enabled=enabled: app_client.patch('/api/v1/settings',
            json={'version': settings['version'], 'learning_enabled': enabled}, headers=headers)
        for enabled in (False, True)
    ])
    assert sorted(r.status_code for r in responses) == [200, 409]
    assert next(r for r in responses if r.status_code == 409).json()['error']['code'] == 'VERSION_CONFLICT'


def test_idempotency_reservation_rollback_releases_key(app_client):
    tokens = register_and_login(app_client)
    user = tokens['user']['id']
    idem = key()
    target = key()

    async def check():
        async with app_client.app.state.db.session_factory() as db:
            replayed, _ = await idempotency.begin(db, 'test-secret', user, 'dictionary.term.create', idem, {'term': 'React'}, 'dictionary_term', target)
            assert replayed is False
            await db.rollback()
        async with app_client.app.state.db.session_factory() as db:
            replayed, actual = await idempotency.begin(db, 'test-secret', user, 'dictionary.term.create', idem, {'term': 'React'}, 'dictionary_term', target)
            assert replayed is False and actual == target
            await db.commit()
        async with app_client.app.state.db.session_factory() as db:
            rows = list((await db.scalars(select(OperationRequest).where(OperationRequest.user_id == user))).all())
            assert len(rows) == 1
    app_client.portal.call(check)


def test_correction_is_bound_to_claim_device_even_on_replay(app_client, monkeypatch):
    request_id = key()
    set_classifier(app_client, monkeypatch, decision='suggest')
    headers, session, result, insertion = prepare_inserted(app_client, '请使用 cloud 保存', request_id)
    session['insertion_id'] = insertion
    correction_id = key()
    first = submit(app_client, headers, session, result, 'cloud', 'Claude', correction_id=correction_id)
    assert first.status_code == 202
    other = login(app_client, f'{request_id}@example.com', key()).json()
    for supplied_id in (correction_id, key()):
        rejected = submit(app_client, auth_headers(other), session, result, 'cloud', 'Claude', correction_id=supplied_id)
        assert rejected.status_code == 403
        assert rejected.json()['error']['code'] == 'FORBIDDEN'


def test_classifier_completion_after_retention_deadline_cannot_learn(app_client, monkeypatch):
    entered = Event()
    released = asyncio.Event()

    async def classify(before, after, left, right):
        entered.set()
        await released.wait()
        return Classification(decision='term', ambiguous=False, category='proper_term',
            rationale='controlled test', evidence={'before_start': 0, 'before_end': len(before), 'before': before, 'after': after})

    monkeypatch.setattr(app_client.app.state.classifier, 'classify', classify)
    headers, session, result, insertion = prepare_inserted(app_client, '请使用 cloud 保存', key())
    session['insertion_id'] = insertion
    pending = submit(app_client, headers, session, result, 'cloud', 'Claude')
    assert pending.status_code == 202 and entered.wait(5)
    execute_sql("update users set history_retention_days=7 where id=(select user_id from sessions where id=$1)", session['session_id'])
    deadline_now = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=8)
    monkeypatch.setattr(learning_service, 'content_expired',
                        lambda row, user: content_expired(row, user, deadline_now))
    app_client.portal.call(released.set)
    event = wait_event(app_client, headers, pending.json()['event_id'])
    assert event['status'] == 'failed' and event['reason'] == 'CONTENT_EXPIRED'
    assert fetch_one('select count(*) from dictionary_terms') == 0
    assert fetch_one('select content_cleared_at from sessions where id=$1', session['session_id']) is None


def test_cleanup_exact_deadline_removes_attempt_and_frozen_hints(app_client):
    headers, session, _ = complete_dictation(app_client, key())
    now = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
    execute_sql('update users set history_retention_days=7 where id=(select user_id from sessions where id=$1)', session['session_id'])
    execute_sql("update sessions set first_terminal_at=$2, asr_hints='[\"CANARY_ASR\"]', llm_hints='[\"CANARY_LLM\"]' where id=$1", session['session_id'], now-dt.timedelta(days=7))
    execute_sql("update session_attempts set raw_text='CANARY_ATTEMPT' where session_id=$1", session['session_id'])
    async def run_cleanup():
        async with app_client.app.state.db.session_factory() as db:
            user_id = fetch_user_id
            user = await db.scalar(select(User).where(User.id == user_id).with_for_update())
            count = await cleanup._cleanup_user(db, user, now)
            await db.commit()
            return count
    fetch_user_id = fetch_one('select user_id from sessions where id=$1', session['session_id'])
    assert app_client.portal.call(run_cleanup) == 1
    assert fetch_one('select raw_text from session_attempts where session_id=$1', session['session_id']) is None
    assert fetch_one('select asr_hints from sessions where id=$1', session['session_id']) is None
    assert fetch_one('select llm_hints from sessions where id=$1', session['session_id']) is None
    assert fetch_one('select raw_text from results where session_id=$1', session['session_id']) is None
    assert app_client.portal.call(run_cleanup) == 0


def test_pending_and_suggested_learning_are_not_success_counts(app_client, monkeypatch):
    set_classifier(app_client, monkeypatch, decision='suggest')
    headers, session, result, insertion = prepare_inserted(app_client, '请使用 cloud 保存', key())
    session['insertion_id'] = insertion
    pending = submit(app_client, headers, session, result, 'cloud', 'Claude')
    event = wait_event(app_client, headers, pending.json()['event_id'])
    assert event['status'] == 'suggested'
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date().isoformat()
    url = '/api/v1/stats/me'
    params = {'date_from': today, 'date_to': today}
    stats = app_client.get(url, params=params, headers=headers).json()
    assert stats['totals']['learning_events'] == stats['cumulative_learning_events'] == 0
    confirmed = app_client.post(f"/api/v1/learning-events/{event['event_id']}/confirm", headers={**headers, 'Idempotency-Key': key()})
    assert confirmed.status_code == 200
    stats = app_client.get(url, params=params, headers=headers).json()
    assert stats['totals']['learning_events'] == stats['cumulative_learning_events'] == 1


def test_learning_fk_cannot_point_to_another_session_insertion(app_client, monkeypatch):
    set_classifier(app_client, monkeypatch, decision='suggest')
    headers, session, result, insertion = prepare_inserted(app_client, '请使用 cloud 保存', key())
    session['insertion_id'] = insertion
    event = submit(app_client, headers, session, result, 'cloud', 'Claude').json()
    wait_event(app_client, headers, event['event_id'])
    other = create_session(app_client, headers, key()).json()
    dictation(app_client, headers, other['session_id'])
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        execute_sql('update learning_events set session_id=$2 where id=$1', event['event_id'], other['session_id'])
    assert fetch_one('select session_id from learning_events where id=$1', event['event_id']) == session['session_id']


@pytest.mark.parametrize('bad_id', ['x', 'a'*100, 42, None, {}, []])
def test_request_uuid_shape_is_rejected_before_persistence(app_client, bad_id):
    headers = auth_headers(register_and_login(app_client))
    response = app_client.post('/api/v1/sessions', json={'client_request_id': bad_id},
        headers={**headers, 'Idempotency-Key': key()})
    assert response.status_code == 422
    assert response.json()['error']['code'] == 'VALIDATION_ERROR'
    assert fetch_one('select count(*) from sessions') == 0


@pytest.mark.parametrize('bad_key', ['not-a-uuid', 'a'*100])
def test_idempotency_header_requires_uuid(app_client, bad_key):
    headers = auth_headers(register_and_login(app_client))
    response = app_client.post('/api/v1/dictionary/terms', json={'term': 'React'},
        headers={**headers, 'Idempotency-Key': bad_key})
    assert response.status_code == 422
    assert response.json()['error']['code'] == 'VALIDATION_ERROR'
    assert fetch_one('select count(*) from operation_requests') == 0


def test_nonobservable_success_still_counts_as_inserted(app_client):
    headers, session, result = complete_dictation(app_client, key())
    insertion = claim(app_client, headers, session['session_id'], result).json()['insertion_id']
    saved = receipt(app_client, headers, session['session_id'], insertion, 'inserted', False)
    assert saved.status_code == 200 and saved.json()['observable'] is False
    assert fetch_one('select inserted from session_contributions where session_id=$1', session['session_id']) is True


def test_uuid_inputs_normalize_to_string_before_db_binding(app_client):
    headers = auth_headers(register_and_login(app_client))
    identifier = uuid4()
    response = app_client.post('/api/v1/sessions',
        json={'client_request_id': identifier.hex},
        headers={**headers, 'Idempotency-Key': str(identifier).upper()})
    assert response.status_code == 201
    assert fetch_one('select client_request_id from sessions where id=$1', response.json()['session_id']) == str(identifier)


@pytest.mark.parametrize('value', ['React\u0085', '\nReact', 'Re\u2028act'])
def test_terms_reject_controls_before_trim(app_client, value):
    headers = auth_headers(register_and_login(app_client))
    rejected = create_term(app_client, headers, value)
    assert rejected.status_code == 422
    assert rejected.json()['error']['code'] == 'VALIDATION_ERROR'
    allowed = create_term(app_client, headers, 'Visual Studio Code')
    assert allowed.status_code == 201


def test_learning_cannot_create_term_over_another_terms_alias(app_client, monkeypatch):
    set_classifier(app_client, monkeypatch, decision='term')
    headers, session, result, insertion = prepare_inserted(app_client, '请使用 cloud 保存', key())
    existing = create_term(app_client, headers, 'OtherProduct', aliases=['Claude'])
    assert existing.status_code == 201
    session['insertion_id'] = insertion
    response = submit(app_client, headers, session, result, 'cloud', 'Claude')
    assert response.status_code == 202
    event = wait_event(app_client, headers, response.json()['event_id'])
    assert event['status'] == 'failed' and event['reason'] == 'ALIAS_CONFLICT'
    assert fetch_one('select count(*) from dictionary_terms') == 1
