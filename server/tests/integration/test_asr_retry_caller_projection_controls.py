"""Prepared meaningful caller controls; real formal API/PG and declared Mock ASR.

The existing admin-role fixture changes only that declared role. No session,
result, expiry, ready/review or output state is patched by these new controls.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from tests.conftest import auth_headers
from tests.cases.test_dict_cases import key
from tests.ids import fixture_uuid
from tests.integration.test_asr_retry_caller_projection import capability, read_surfaces
from tests.integration.test_capacity_admission_atomicity import AudioFixture, _failed_audio, atomic_client
from tests.integration.test_hist10_other_desktop import RAW, pair, retry
from tests.integration.test_insertion_device_role_matrix import business_snapshot
from tests.test_admin import login_admin
from tests.test_sessions import create_session, dictation

BASE = '请帮我添加按钮并检查缓存。'


def test_same_account_admin_history_hides_only_available_asr(atomic_client, monkeypatch, record_property):
    client = atomic_client
    admin = login_admin(client)  # Existing, explicit admin-role fixture only.
    owner, admin_h = admin['user']['id'], auth_headers(admin)
    logged_in = client.post('/api/auth/login', json={
        'email': 'admin@example.com', 'password': 'SmokePass123', 'client_type': 'desktop',
        'device': {'installation_id':fixture_uuid(key()), 'name':'projection-control-mac',
                   'platform':'macos', 'client_version':'0.0.1'},
    })
    assert logged_in.status_code == 200, logged_in.text
    assert logged_in.json()['user']['id'] == owner
    desktop_h = auth_headers(logged_in.json())
    physical = AudioFixture(client, monkeypatch, {owner:RAW})
    retained = _failed_audio(client, desktop_h, physical)
    before = business_snapshot(owner)
    cutoff, pcm_sha = retained.runtime.retry_expires_at, hashlib.sha256(retained.audio).hexdigest()
    opened = list(physical.opened)
    desktop = read_surfaces(client, desktop_h, retained.sid)
    denied_detail = client.get(f'/api/v1/sessions/{retained.sid}', headers=admin_h)
    denied_retry = retry(client, admin_h, retained.sid, key(), 'asr')
    for response in (denied_detail,denied_retry):
        assert response.status_code == 403 and response.json()['error']['code'] == 'FORBIDDEN'
    history = client.get('/api/v1/history', headers=admin_h)
    assert history.status_code == 200, history.text
    items = [row for row in history.json()['items'] if row['session_id'] == retained.sid]
    assert len(items) == 1
    assert capability(items[0]) == {'retry_kind':'none', 'retry_expires_at':None, 'retry_unavailable_reason':None}
    assert all(row['retry_kind'] == 'asr' and row['retry_expires_at'] is not None for row in desktop.values())
    assert business_snapshot(owner) == before
    assert retained.runtime.audio is retained.audio and hashlib.sha256(retained.audio).hexdigest() == pcm_sha
    assert retained.runtime.retry_expires_at == cutoff and physical.opened == opened
    record_property('same_account_admin_projection', json.dumps({'session_id':retained.sid,
        'same_account_history_allowed':True, 'admin_available_asr_hidden':True,
        'desktop_available_asr_preserved':True, 'admin_GET_and_retry_403':True,
        'business_and_original_cache_unchanged':True}))


@pytest.mark.parametrize('global_state', ['ready','cancelled'])
def test_other_device_retains_original_global_reason(atomic_client, monkeypatch, global_state, record_property):
    client = atomic_client
    owner, headers, _, _ = pair(client)
    physical = AudioFixture(client, monkeypatch, {owner:BASE})
    created = create_session(client, headers[0], key())
    assert created.status_code == 201, created.text
    sid = created.json()['session_id']
    if global_state == 'ready':
        events = dictation(client, headers[0], sid)
        assert events[-1]['type'] == 'result.ready'
        assert events[-1]['result']['review_required'] is False
        reason = 'not_needed'
    else:
        cancelled = client.post(f'/api/v1/sessions/{sid}/cancel', headers=headers[0])
        assert cancelled.status_code == 200 and cancelled.json()['state'] == 'cancelled'
        reason = 'cancelled'
    before = business_snapshot(owner)
    opened = list(physical.opened)
    for h in headers:
        seen = read_surfaces(client, h, sid)
        assert all(row == {'retry_kind':'none', 'retry_expires_at':None,
                           'retry_unavailable_reason':reason} for row in seen.values())
    assert business_snapshot(owner) == before and physical.opened == opened
    record_property('original_global_reason', json.dumps({'session_id':sid,'state':global_state,
        'reason':reason,'D1_and_D2_both_surfaces_unchanged':True,'business_unchanged':True}))
