"""Prepared red regression: original API/PG/WS and original controlled ASR.

Only the existing AudioFixture injects the declared first-ASR physical failure.
No state SQL UPDATE, fake ready, clock/TTL patch or output monkeypatch.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json

import pytest

from tests.cases.test_dict_cases import key
from tests.database_instants import database_instant
from tests.integration.test_capacity_admission_atomicity import (
    AudioFixture,
    _failed_audio,
    atomic_client,
)
from tests.integration.test_hist10_other_desktop import RAW, pair, retry
from tests.integration.test_insertion_device_role_matrix import business_snapshot


def capability(body):
    return {key: body[key] for key in
            ('retry_kind', 'retry_expires_at', 'retry_unavailable_reason')}


def instant(value):
    assert isinstance(value, str)
    parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    assert parsed.tzinfo is not None and parsed.utcoffset() == dt.timedelta(0)
    return parsed


def comparable_capability(body):
    answer = dict(body)
    if answer['retry_expires_at'] is not None:
        answer['retry_expires_at'] = instant(answer['retry_expires_at'])
    return answer


def read_surfaces(client, headers, sid):
    detail = client.get(f'/api/v1/sessions/{sid}', headers=headers)
    assert detail.status_code == 200, detail.text
    history = client.get('/api/v1/history', headers=headers)
    assert history.status_code == 200, history.text
    items = [item for item in history.json()['items'] if item['session_id'] == sid]
    assert len(items) == 1
    return {'detail': capability(detail.json()), 'history': capability(items[0])}


@pytest.mark.parametrize('surface', ['detail', 'history'])
def test_cached_asr_retry_projection_is_caller_device_bound(
    atomic_client, monkeypatch, record_property, surface,
):
    client = atomic_client
    owner, headers, devices, login_sids = pair(client)
    physical = AudioFixture(client, monkeypatch, {owner: RAW})
    retained = _failed_audio(client, headers[0], physical)
    before = business_snapshot(owner)
    cutoff = retained.runtime.retry_expires_at
    pcm_sha256 = hashlib.sha256(retained.audio).hexdigest()
    opened = list(physical.opened)
    assert len(before['sessions']) == len(before['session_attempts']) == 1
    assert before['sessions'][0]['state'] == 'failed'
    assert before['sessions'][0]['audio_complete'] is True
    assert before['sessions'][0]['received_samples'] == 8000
    assert before['results'] == before['insertions'] == before['learning_events'] == before['model_calls'] == []

    d1 = read_surfaces(client, headers[0], retained.sid)
    assert comparable_capability(d1['detail']) == comparable_capability(d1['history'])
    assert d1['detail']['retry_kind'] == 'asr'
    assert d1['detail']['retry_unavailable_reason'] is None
    deadline = instant(d1['detail']['retry_expires_at'])
    first_failed = database_instant(before['sessions'][0]['first_asr_failed_at'])
    assert deadline == first_failed + dt.timedelta(seconds=client.app.state.config.retry.audio_ttl_seconds)

    d2 = read_surfaces(client, headers[1], retained.sid)
    denied = retry(client, headers[1], retained.sid, key(), 'asr')
    assert denied.status_code == 403, denied.text
    assert denied.json()['error']['code'] == 'FORBIDDEN'
    assert business_snapshot(owner) == before
    assert retained.runtime.audio is retained.audio and bytes(retained.audio) == retained.pcm
    assert hashlib.sha256(retained.audio).hexdigest() == pcm_sha256
    assert retained.runtime.retry_expires_at == cutoff and physical.opened == opened

    # Preserve safe observed values before the intended red assertion. This
    # diagnoses each actual endpoint independently without leaking credentials.
    record_property('asr_retry_caller_projection', json.dumps({
        'surface': surface, 'session_id': retained.sid, 'user_id': owner,
        'origin_device_id': devices[0], 'caller_device_id': devices[1],
        'origin_login_sid': login_sids[0], 'caller_login_sid': login_sids[1],
        'd1': d1, 'd2': d2, 'denied_status': denied.status_code,
        'denied_code': denied.json()['error']['code'], 'received_samples': 8000,
        'pcm_sha256': pcm_sha256, 'business_unchanged': True,
        'same_original_pcm_and_cutoff': True, 'no_additional_asr_open': True,
    }, sort_keys=True))
    assert d2[surface] == {
        'retry_kind': 'none',
        'retry_expires_at': None,
        'retry_unavailable_reason': None,
    }, {'surface': surface, 'actual': d2[surface]}
