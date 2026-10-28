"""Real API/PG UTC-Z output controls; original Mock models, no native claim.

Separate from frozen history batch. Never installs a trusted resource identity
or a valid capacity report, seeds timestamps, patches clocks, or rewrites text.
"""
from __future__ import annotations
import datetime as dt
import hashlib
import json

from tests.cases.test_dict_cases import fetch_one
from tests.conftest import auth_headers, register_and_login
from tests.database_instants import database_instant
from tests.integration.test_capacity_window_metrics import metrics_client
from tests.test_admin import login_admin
from tests.test_dictionary import create_term
from tests.test_insertions_learning import claim, receipt, wait_event, key
from tests.test_sessions import create_session, dictation


def instant(value):
    assert isinstance(value, str)
    answer = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    assert answer.tzinfo is not None and answer.utcoffset() == dt.timedelta(0)
    return answer


def observe(rows, label, body, expected=None, fields=('created_at', 'updated_at')):
    values = {field: body[field] for field in fields}
    for value in values.values():
        if value is not None:
            instant(value)
    if expected is not None:
        for field, value in values.items():
            if value is None:
                assert expected[field] is None
            else:
                assert instant(value) == database_instant(expected[field])
    rows.append({'label': label, 'values': values})


def finish(rows, record_property):
    record_property('outward_utc_z_wire', json.dumps({'observed': rows,
        'precise_instants_checked': True,
        'scope': 'explicit outward UTC-Z fields only; not history/valid-capacity/native/model quality'}, sort_keys=True))
    assert all(value is None or value.endswith('Z')
               for row in rows for value in row['values'].values()), rows


def term_times(user_id, term_id):
    return json.loads(fetch_one("SELECT json_build_object('created_at',created_at,'updated_at',updated_at)::text "
        "FROM dictionary_terms WHERE user_id=$1 AND id=$2", user_id, term_id))


def event_times(user_id, event_id):
    return json.loads(fetch_one("SELECT json_build_object('created_at',created_at)::text "
        "FROM learning_events WHERE user_id=$1 AND id=$2", user_id, event_id))


def user_times(user_id):
    return json.loads(fetch_one("SELECT json_build_object('created_at',created_at,'last_login_at',last_login_at)::text "
        "FROM users WHERE id=$1", user_id))


def test_dictionary_write_replay_and_list_share_utc_z(app_client, record_property):
    tokens = register_and_login(app_client); uid = tokens['user']['id']; headers = auth_headers(tokens)
    rows = []; literal = 'Wire+00:00Z'; idem = key()
    made = create_term(app_client, headers, literal, ['literal+00:00Z'], idem)
    assert made.status_code == 201; made = made.json(); tid = made['id']
    observe(rows, 'created', made, term_times(uid, tid))
    replay = create_term(app_client, headers, literal, ['literal+00:00Z'], idem)
    assert replay.status_code == 200 and replay.json()['replayed'] is True
    observe(rows, 'create-replay', replay.json(), term_times(uid, tid))
    patch_key = key(); payload = {'version': made['version'], 'aliases': ['literal+00:00Z', 'aliasZ']}
    changed = app_client.patch('/api/v1/dictionary/terms/'+tid, json=payload,
        headers={**headers, 'Idempotency-Key': patch_key})
    assert changed.status_code == 200
    observe(rows, 'patched', changed.json(), term_times(uid, tid))
    repeated = app_client.patch('/api/v1/dictionary/terms/'+tid, json=payload,
        headers={**headers, 'Idempotency-Key': patch_key})
    assert repeated.status_code == 200 and repeated.json()['replayed'] is True
    observe(rows, 'patch-replay', repeated.json(), term_times(uid, tid))
    listed = app_client.get('/api/v1/dictionary/terms', headers=headers)
    assert listed.status_code == 200 and len(listed.json()['items']) == 1
    final = listed.json()['items'][0]; observe(rows, 'listed', final, term_times(uid, tid))
    assert all(body['term'] == literal for body in (made, replay.json(), changed.json(), repeated.json(), final))
    assert set(final['aliases']) == {'literal+00:00Z', 'aliasZ'} and len(final['aliases']) == 2
    finish(rows, record_property)


def test_learning_event_confirm_and_undo_term_share_utc_z(app_client, record_property):
    tokens = register_and_login(app_client); uid = tokens['user']['id']; headers = auth_headers(tokens)
    # Existing manual term yields a non-null term in the original undo response.
    original = create_term(app_client, headers, '按键', [])
    assert original.status_code == 201; tid = original.json()['id']
    created = create_session(app_client, headers, 'wire-learning'); assert created.status_code == 201
    sid = created.json()['session_id']; terminal = dictation(app_client, headers, sid)[-1]
    assert terminal['type'] == 'result.ready'; result = terminal['result']
    reserved = claim(app_client, headers, sid, result, review_confirmed=True)
    assert reserved.status_code == 201; iid = reserved.json()['insertion_id']
    assert receipt(app_client, headers, sid, iid, 'inserted', True).status_code == 200
    text = result['refined_text']; offset = text.index('按钮'); cid = key()
    posted = app_client.post('/api/v1/corrections', json={'session_id': sid, 'insertion_id': iid,
        'correction_id': cid, 'offset': offset, 'before': '按钮', 'after': '按键',
        'left': text[max(0,offset-4):offset], 'right': text[offset+2:offset+6]}, headers=headers)
    assert posted.status_code == 202; eid = posted.json()['event_id']; rows = []
    observe(rows, 'correction-post', posted.json(), event_times(uid,eid), fields=('created_at',))
    event = wait_event(app_client, headers, eid)
    assert event['status'] == 'suggested'
    observe(rows, 'event-get', event, event_times(uid,eid), fields=('created_at',))
    confirm_key = key()
    confirmed = app_client.post('/api/v1/learning-events/'+eid+'/confirm',
        headers={**headers, 'Idempotency-Key': confirm_key})
    assert confirmed.status_code == 200 and confirmed.json()['status'] == 'learned'
    observe(rows, 'event-confirm', confirmed.json(), event_times(uid,eid), fields=('created_at',))
    assert confirmed.json()['term']['id'] == tid
    observe(rows, 'confirmed-term', confirmed.json()['term'], term_times(uid,tid))
    replay = app_client.post('/api/v1/learning-events/'+eid+'/confirm',
        headers={**headers, 'Idempotency-Key': confirm_key})
    assert replay.status_code == 200 and replay.json()['replayed'] is True
    observe(rows, 'confirm-replay-event', replay.json(), event_times(uid,eid), fields=('created_at',))
    observe(rows, 'confirm-replay-term', replay.json()['term'], term_times(uid,tid))
    undo_key = key()
    undone = app_client.post('/api/v1/learning-events/'+eid+'/undo',
        headers={**headers, 'Idempotency-Key': undo_key})
    assert undone.status_code == 200 and undone.json()['undone'] is True and undone.json()['term'] is not None
    observe(rows, 'undo-restored-term', undone.json()['term'], term_times(uid,tid))
    undo_replay = app_client.post('/api/v1/learning-events/'+eid+'/undo',
        headers={**headers, 'Idempotency-Key': undo_key})
    assert undo_replay.status_code == 200 and undo_replay.json()['replayed'] is True
    observe(rows, 'undo-replayed-term', undo_replay.json()['term'], term_times(uid,tid))
    assert undone.json()['term']['term'] == '按键' and undone.json()['term']['aliases'] == []
    finish(rows, record_property)


def test_admin_user_write_list_login_and_last_session_use_utc_z(app_client, record_property):
    admin = login_admin(app_client); headers = auth_headers(admin); rows = []
    email, password, idem = 'utc-member@example.invalid', 'WireMemberPass123', key()
    payload = {'email': email, 'password': password, 'display_name': '时区+00:00Z'}
    created = app_client.post('/api/admin/users', json=payload,
        headers={**headers, 'Idempotency-Key': idem})
    assert created.status_code == 201; uid = created.json()['user']['id']
    observe(rows, 'admin-create-user', created.json()['user'], user_times(uid), fields=('created_at','last_login_at'))
    assert created.json()['user']['last_login_at'] is None
    replay = app_client.post('/api/admin/users', json=payload,
        headers={**headers, 'Idempotency-Key': idem})
    assert replay.status_code == 200 and replay.json()['replayed'] is True
    observe(rows, 'admin-create-replay', replay.json()['user'], user_times(uid), fields=('created_at','last_login_at'))
    logged = app_client.post('/api/auth/login', json={'email':email,'password':password,'client_type':'desktop',
        'device':{'installation_id':key(),'name':'UTC owned device','platform':'macos','client_version':'0.0.1'}})
    assert logged.status_code == 200; user_headers = auth_headers(logged.json())
    observe(rows, 'typed-auth-user', logged.json()['user'], user_times(uid), fields=('created_at','last_login_at'))
    session = create_session(app_client, user_headers, 'wire-admin-last-session'); assert session.status_code == 201
    sid = session.json()['session_id']; assert dictation(app_client,user_headers,sid)[-1]['type'] == 'result.ready'
    listed = app_client.get('/api/admin/users', headers=headers); assert listed.status_code == 200
    item = next(item for item in listed.json()['items'] if item['user']['id'] == uid)
    observe(rows, 'admin-listed-user', item['user'], user_times(uid), fields=('created_at','last_login_at'))
    last = json.loads(fetch_one("SELECT json_build_object('last_session_at',created_at)::text FROM sessions WHERE user_id=$1 AND id=$2",uid,sid))
    observe(rows, 'admin-last-session', item['totals'], last, fields=('last_session_at',))
    disabled = app_client.patch('/api/admin/users/'+uid, json={'status':'disabled'}, headers=headers)
    assert disabled.status_code == 200 and disabled.json()['user']['status'] == 'disabled'
    observe(rows, 'admin-patched-user', disabled.json()['user'], user_times(uid), fields=('created_at','last_login_at'))
    assert item['user']['display_name'] == disabled.json()['user']['display_name'] == '时区+00:00Z'
    finish(rows, record_property)


def test_ws_result_created_at_matches_typed_http_and_pg(app_client, record_property):
    tokens = register_and_login(app_client); uid=tokens['user']['id']; headers=auth_headers(tokens)
    made = create_session(app_client, headers, 'wire-ws'); assert made.status_code == 201
    sid=made.json()['session_id']; terminal=dictation(app_client,headers,sid)[-1]; assert terminal['type']=='result.ready'
    result=terminal['result']; detail=app_client.get('/api/v1/sessions/'+sid,headers=headers)
    assert detail.status_code==200 and detail.json()['result']['result_id']==result['result_id']
    pg=json.loads(fetch_one("SELECT json_build_object('created_at',created_at)::text FROM results WHERE user_id=$1 AND session_id=$2 AND id=$3",uid,sid,result['result_id']))
    rows=[];observe(rows,'actual-ws-result',result,pg,fields=('created_at',));observe(rows,'typed-http-result',detail.json()['result'],pg,fields=('created_at',))
    assert result['raw_text']==detail.json()['result']['raw_text'] and result['refined_text']==detail.json()['result']['refined_text']
    finish(rows,record_property)
    assert result['created_at']==detail.json()['result']['created_at']


def test_capacity_explicit_times_use_utc_z_without_attesting_report(metrics_client, record_property):
    client=metrics_client; headers=auth_headers(login_admin(client)); rows=[]
    first=client.get('/api/admin/capacity',headers=headers); assert first.status_code==200; body=first.json()
    assert body['validation']=={'status':'unverified','report':None} and body['runtime'] is not None
    def capacity_times(label, value):
        stored=json.loads(fetch_one("SELECT json_build_object('updated_at',updated_at)::text FROM runtime_capacity WHERE id=1"))
        observe(rows,label+'-config',value['config'],stored,fields=('updated_at',))
        observe(rows,label+'-sample',value['runtime'],fields=('sampled_at',))
    capacity_times('get',body)
    updated=client.patch('/api/admin/capacity',headers=headers,
        json={'version':body['config']['version'],'max_active_sessions':10})
    assert updated.status_code==200 and updated.json()['config']['max_active_sessions']==10
    assert updated.json()['validation']=={'status':'unverified','report':None}
    capacity_times('patch',updated.json())
    finish(rows,record_property)
