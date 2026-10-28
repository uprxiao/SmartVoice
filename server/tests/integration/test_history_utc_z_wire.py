"""UTC-Z HTTP wire regressions; real API/WS/PG, explicit original Mock audio.

History first batch only: explicit WS/capacity and other outward dumps remain
separate pending surfaces. No timestamp seed, clock/TTL change or body rewrite.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json

from tests.conftest import auth_headers, register_and_login
from tests.database_instants import database_instant
from tests.integration.test_capacity_admission_atomicity import AudioFixture, _failed_audio, atomic_client
from tests.integration.test_insertion_device_role_matrix import business_snapshot
from tests.test_sessions import create_session, dictation

TIMES = ('created_at', 'first_terminal_at', 'content_expires_at', 'retry_expires_at')
RAW = '保留时区文本 +00:00 与 Z 原样。'


def instant(value):
    assert isinstance(value, str)
    parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    assert parsed.tzinfo is not None and parsed.utcoffset() == dt.timedelta(0)
    return parsed


def read_same_sid(client, headers, sid):
    detail = client.get('/api/v1/sessions/'+sid, headers=headers)
    history = client.get('/api/v1/history', headers=headers)
    assert detail.status_code == history.status_code == 200
    selected = [row for row in history.json()['items'] if row['session_id'] == sid]
    assert len(selected) == 1
    detail, history = detail.json(), selected[0]
    # History must retain its current outward field set, without a new default.
    assert set(history) == set(detail)-{'replayed'}
    assert detail['replayed'] is False
    return detail, history


def time_values(body):
    values = {key: body[key] for key in TIMES}
    values['result.created_at'] = None if body['result'] is None else body['result']['created_at']
    return values


def equal_instants(left, right):
    assert set(left) == set(right)
    for key in left:
        if left[key] is None:
            assert right[key] is None
        else:
            assert instant(left[key]) == instant(right[key]), key


def strict_z(values):
    return all(value is None or (isinstance(value, str) and value.endswith('Z'))
               for value in values.values())


def test_history_failed_asr_deadline_uses_same_utc_z_instant(atomic_client, monkeypatch, record_property):
    client = atomic_client
    tokens = register_and_login(client, email='wire-failed@example.invalid')
    owner, headers = tokens['user']['id'], auth_headers(tokens)
    physical = AudioFixture(client, monkeypatch, {owner: RAW})
    retained = _failed_audio(client, headers, physical)
    before = business_snapshot(owner)
    cutoff = retained.runtime.retry_expires_at
    pcm_sha = hashlib.sha256(retained.audio).hexdigest()
    opens = list(physical.opened)
    detail, history = read_same_sid(client, headers, retained.sid)
    dtimes, htimes = time_values(detail), time_values(history)
    equal_instants(dtimes, htimes)
    session = before['sessions'][0]
    ttl = client.app.state.config.retry.audio_ttl_seconds
    assert ttl == 600
    assert instant(dtimes['retry_expires_at']) == database_instant(session['first_asr_failed_at'])+dt.timedelta(seconds=ttl)
    assert instant(dtimes['created_at']) == database_instant(session['created_at'])
    assert detail['state'] == history['state'] == 'failed'
    assert detail['retry_kind'] == history['retry_kind'] == 'asr'
    assert detail['result'] is history['result'] is None
    assert detail['received_samples'] == history['received_samples'] == 8000
    assert business_snapshot(owner) == before
    assert retained.runtime.audio is retained.audio and bytes(retained.audio) == retained.pcm
    assert retained.runtime.retry_expires_at == cutoff and physical.opened == opens
    record_property('history_utc_z_wire', json.dumps({'session_id':retained.sid,
        'detail_times':dtimes,'history_times':htimes,'same_precise_instants':True,
        'original_ttl_seconds':ttl,'same_original_pcm_sha256':pcm_sha,
        'unchanged_original_cutoff_and_business':True,'no_additional_asr_open':True,
        'scope':'HTTP history/detail only; explicit original Mock first-ASR failure; not WS/capacity/all wire surfaces'}, sort_keys=True))
    assert strict_z(dtimes) and strict_z(htimes), {'detail_times':dtimes,'history_times':htimes}
    assert dtimes == htimes


def test_history_ready_result_uses_same_utc_z_without_touching_text(atomic_client, monkeypatch, record_property):
    client = atomic_client
    tokens = register_and_login(client, email='wire-ready@example.invalid')
    owner, headers = tokens['user']['id'], auth_headers(tokens)
    physical = AudioFixture(client, monkeypatch, {owner: RAW})
    created = create_session(client, headers, 'wire-ready')
    assert created.status_code == 201
    sid = created.json()['session_id']
    runtime = client.app.state.sessions.runtimes[sid]
    audio = runtime.audio
    terminal = dictation(client, headers, sid)[-1]
    assert terminal['type'] == 'result.ready'
    before = business_snapshot(owner)
    detail, history = read_same_sid(client, headers, sid)
    dtimes, htimes = time_values(detail), time_values(history)
    equal_instants(dtimes, htimes)
    row = next(row for row in before['sessions'] if row['id'] == sid)
    result = next(record for record in before['results'] if record['id'] == row['current_result_id'] and record['session_id'] == sid)
    assert instant(dtimes['created_at']) == database_instant(row['created_at'])
    assert instant(dtimes['first_terminal_at']) == database_instant(row['first_terminal_at'])
    assert instant(dtimes['result.created_at']) == database_instant(result['created_at'])
    assert instant(terminal['result']['created_at']) == database_instant(result['created_at'])
    assert detail['state'] == history['state'] == 'ready'
    assert detail['result']['result_id'] == history['result']['result_id'] == result['id']
    assert detail['result']['raw_text'] == detail['result']['refined_text'] == RAW
    assert history['result']['raw_text'] == history['result']['refined_text'] == RAW
    assert detail['retry_expires_at'] is history['retry_expires_at'] is None
    assert business_snapshot(owner) == before
    assert audio is runtime.audio and bytes(audio) == b'' and runtime.asr is None
    assert len(physical.opened) == 1
    record_property('history_utc_z_wire', json.dumps({'session_id':sid,
        'detail_times':dtimes,'history_times':htimes,'same_precise_PG_instants':True,
        'text_sha256':hashlib.sha256(RAW.encode()).hexdigest(),'original_text_unchanged':True,
        'original_audio_cleared':True,'WS_created_at_observed_but_Z_not_claimed':terminal['result']['created_at'],
        'scope':'HTTP history/detail only; original Mock ASR/LLM; no global string rewrite or WS/capacity/all wire claim'}, sort_keys=True))
    assert strict_z(dtimes) and strict_z(htimes), {'detail_times':dtimes,'history_times':htimes}
    assert dtimes == htimes
