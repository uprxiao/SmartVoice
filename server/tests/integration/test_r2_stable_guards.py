"""ASR19 capability and Qwen stable-regression protocol, no physical model."""
from types import SimpleNamespace
import json
import struct

from fastapi.testclient import TestClient
import pytest

from app.providers.asr.mock import MockAsrProvider
from tests.conftest import SERVER_ROOT, auth_headers, register_and_login, wait_startup_completion
from tests.cases.test_dict_cases import fetch_one
from tests.test_sessions import create_session
from tests.integration.test_r2_qwen_cache_limits import qwen_cache


@pytest.mark.parametrize('kind',['missing','false'])
def test_required_stable_capability_rejects_before_open_without_fallback(
    clean_database,monkeypatch,record_property,kind,
):
    from app import main
    provider=MockAsrProvider();provider.capabilities=SimpleNamespace(backend_version='fictional-invalid-capability')
    if kind=='false':provider.capabilities.stable_partial=False
    counts={'prepare':0,'open':0,'close':0,'llm':0}
    prepare,close=provider.prepare,provider.close
    async def prepared():counts['prepare']+=1;return await prepare()
    async def closed():counts['close']+=1;return await close()
    async def never_open(*args,**kwargs):counts['open']+=1;raise AssertionError('invalid capability opened')
    provider.prepare=prepared;provider.close=closed;provider.open=never_open
    monkeypatch.setattr(main,'build_asr',lambda config:provider)
    app=main.create_app(str(SERVER_ROOT/'config/app.yaml'))
    with TestClient(app) as client:
        assert app.state.config.asr.stable_partial_required is True
        wait_startup_completion(client)
        response=client.get('/health/ready');assert response.status_code==503
        assert response.json()['reason_code']=='ASR_UNAVAILABLE'
        actor=register_and_login(client,email='stable-cap-'+kind+'@example.invalid');h=auth_headers(actor)
        caps=client.get('/api/v1/capabilities',headers=h).json();assert caps['ready'] is False
        made=create_session(client,h,'stable-capability-'+kind)
        assert made.status_code==503 and made.json()['error']['code']=='SERVICE_UNAVAILABLE'
        assert counts['prepare']>=1 and counts['close']>=1 and counts['open']==0
        assert fetch_one('select count(*) from sessions')==fetch_one('select count(*) from results')==0
        assert fetch_one('select count(*) from model_calls')==0
        assert app.state.asr is None and not app.state.asr_prepared
        record_property('stable_capability',json.dumps({'kind':kind,'readiness':503,'session_created':0,
            'physical_dispatch':0,'no_fallback':True,'counts':counts}))


def test_actual_qwen_second_native_block_regressing_stable_fails_ws_and_clears_original_audio(
    qwen_cache,monkeypatch,record_property,caplog,
):
    client,worker,streams,originals,_=qwen_cache;actual=worker.decode;blocks=[]
    async def regressing(args,deadline):
        value=await actual(args,deadline)
        if args[1]['action']=='feed':
            blocks.append(value['samples'])
            if len(blocks)==2:value.update(text='别的文字',stable='别的')
        return value
    monkeypatch.setattr(worker,'decode',regressing)
    h=auth_headers(register_and_login(client,email='qwen-stable-regression@example.invalid'))
    made=create_session(client,h,'qwen-regressing-stable');assert made.status_code==201
    sid=made.json()['session_id'];runtime=client.app.state.sessions.runtimes[sid]
    audio=originals[sid];events=[]
    with client.websocket_connect('/ws/v1/sessions/'+sid+'/audio',headers=h) as ws:
        assert ws.receive_json()['type']=='ready'
        for seq in range(8):
            ws.send_bytes(struct.pack('<II',seq,4000)+struct.pack('<h',100)*4000)
            while True:
                event=ws.receive_json();events.append(event);assert event['type']!='error'
                if event['type']=='audio.ack':break
        while not any(e['type']=='asr.partial' for e in events):
            event=ws.receive_json();events.append(event);assert event['type']!='error'
        assert any(e['type']=='asr.partial' and e['stable_text']=='缓存' for e in events)
        for seq in range(8,16):
            ws.send_bytes(struct.pack('<II',seq,4000)+struct.pack('<h',100)*4000)
            while True:
                event=ws.receive_json();events.append(event)
                if event['type'] in ('audio.ack','error'):break
            if event['type']=='error':assert seq==15;break
        while events[-1]['type']!='error':events.append(ws.receive_json())
    assert blocks==[32000,64000]
    assert events[-1]['error']['code']=='ASR_FAILED'
    assert not any(e['type']=='asr.partial' and e['stable_text']=='别的' for e in events)
    assert not any(e['type'] in ('asr.final','result.ready') for e in events)
    detail=client.get('/api/v1/sessions/'+sid,headers=h).json()
    assert detail['state']=='failed' and detail['current_result_id'] is None and detail['result'] is None
    assert detail['audio_complete'] is False and detail['retry_kind']=='none'
    stream=streams[(sid,1)]
    assert stream.cancelled and not stream.pending and stream.native_options=={'context':'','language':None}
    assert audio==runtime.audio==b'' and runtime.asr is None and sid not in client.app.state.sessions.runtimes
    assert worker.active is None and not worker.states and not client.app.state.asr.engine._audio_inputs
    assert fetch_one('select count(*) from results')==fetch_one('select count(*) from insertions')==0
    assert fetch_one('select count(*) from learning_events')==0
    assert fetch_one("select count(*) from model_calls where purpose in ('refine','audit','classify')")==0
    assert '别的文字' not in caplog.text and '缓存验证。' not in caplog.text
    record_property('qwen_stable_regression',json.dumps({'native_blocks':blocks,'valid_stable_published':True,
        'regressing_stable_published':False,'result_rows':0,'original_pcm_bytes':len(audio),
        'pending_bytes':len(stream.pending),'working_copy':0,'llm_calls':0,'physical_worker':'fictional'}))
