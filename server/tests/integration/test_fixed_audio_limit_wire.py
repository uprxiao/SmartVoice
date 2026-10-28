"""Exact configured sample boundary, real ASGI WS/PG, synthetic PCM/providers.

No clock compression claim: 180 seconds of samples are sent without real-time
pacing to isolate the parser/count contract, not microphone/model performance.
"""
import pytest

from tests.conftest import auth_headers, register_and_login
from tests.cases.test_api_cases import drain_until_close, state_of
from tests.cases.test_dict_cases import fetch_one
from tests.test_sessions import create_session, frame


@pytest.mark.parametrize('overflow', [False, True], ids=['exact-2880000', 'reject-sample-2880001'])
def test_actual_180_second_sample_limit_without_lowering_constants(app_client, overflow):
    from app.services import session_runtime

    assert session_runtime.MAX_SAMPLES == 2_880_000
    tokens = register_and_login(app_client, email=f'fixed-audio-limit-{overflow}@example.invalid')
    headers = auth_headers(tokens)
    made = create_session(app_client, headers, f'fixed-limit-{overflow}')
    assert made.status_code == 201
    session = made.json()
    sid = session['session_id']
    runtime = app_client.app.state.sessions.runtimes[sid]
    audio = runtime.audio
    with app_client.websocket_connect(f'/ws/v1/sessions/{sid}/audio', headers=headers) as ws:
        ready = ws.receive_json()
        assert ready['type'] == 'ready'
        assert ready['audio']['max_samples'] == 2_880_000
        for seq in range(720):
            ws.send_bytes(frame(seq))
            ack = ws.receive_json()
            assert ack['type'] == 'audio.ack'
            assert (ack['seq'], ack['total_samples']) == (seq, (seq+1)*4000)
        assert runtime.samples == 2_880_000
        if overflow:
            ws.send_bytes(frame(720, 1))
        else:
            ws.send_json({'type': 'audio.end', 'last_seq': 719, 'total_samples': 2_880_000})
        events, close_code = drain_until_close(ws)
    assert not audio
    detail = state_of(app_client, headers, session)
    assert detail['received_samples'] == 2_880_000
    assert fetch_one('select received_samples from sessions where id=$1', sid) == 2_880_000
    if overflow:
        assert close_code == 4400
        assert [e['error']['code'] for e in events if e['type'] == 'error'] == ['AUDIO_LIMIT_EXCEEDED']
        assert not any(e['type'] in ('audio.ack', 'audio.end_ack', 'asr.final', 'result.ready') for e in events)
        assert detail['state'] == 'failed' and detail['result'] is None
        assert detail['audio_complete'] is False
        assert fetch_one('select count(*) from results where session_id=$1', sid) == 0
    else:
        assert close_code == 1000
        assert events[0]['type'] == 'audio.end_ack'
        assert (events[0]['last_seq'], events[0]['total_samples']) == (719, 2_880_000)
        assert events[-1]['type'] == 'result.ready'
        assert len([e for e in events if e['type'] == 'asr.final']) == 1
        assert detail['state'] == 'ready' and detail['audio_complete'] is True
        assert detail['result']['result_id'] == events[-1]['result']['result_id']
        assert fetch_one('select count(*) from results where session_id=$1', sid) == 1
    for table in ('insertions', 'learning_events'):
        assert fetch_one(f'select count(*) from {table} where session_id=$1', sid) == 0
