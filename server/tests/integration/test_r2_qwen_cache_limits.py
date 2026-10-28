"""Qwen native adapter cache limits; fictional decoder, real API/WS/PG."""
import asyncio
import struct

import pytest

from app.core.config import Config, SESSION_PCM_RESERVE
from app.providers.asr.base import ProviderError
from app.providers.asr.qwen3_mlx import Qwen3MlxProvider
from tests.conftest import auth_headers, register_and_login
from tests.cases.test_dict_cases import key, fetch_one
from tests.cases.test_hist_cases import wait_state
from tests.test_sessions import create_session
from tests.unit.test_qwen_native_provider import manifest, Process
from tests.qwen_native_fixtures import native_checkpoint


class Worker:
    stop_incomplete=False
    def __init__(self):
        self.process=None; self.states={}; self.active=None; self.calls=[]
        self.gate=None; self.entered=asyncio.Event(); self.release=asyncio.Event()
    async def decode(self,args,deadline):
        audio,cmd=args; action=cmd['action'];owner=cmd.get('owner')
        self.calls.append({'action':action,'owner':owner,'samples':len(audio)//2})
        if action=='prepare':self.process=Process();return {'ready':True,'runtime':'fictional-cache-worker'}
        if action=='drop':self.states.pop(owner,None);return {'dropped':True}
        if cmd.get('checkpoint') is not None:
            checkpoint=cmd['checkpoint']
            assert owner not in self.states and checkpoint['owner']==owner
            assert checkpoint['samples']==cmd['start_sample']
            self.states[owner]=checkpoint['samples']
        prior=self.states.get(owner,0);assert cmd['start_sample']==prior
        count=prior+len(audio)//2;self.active=audio
        try:
            if owner==self.gate:
                self.entered.set();await self.release.wait()
            self.states[owner]=count
            if isinstance(audio,bytearray):audio.clear()
            if action=='finish':self.states.pop(owner,None)
            value={'samples':count,'text':'缓存验证。','stable':'缓存'}
            if action=='feed':
                value['checkpoint']=native_checkpoint(owner,count,value['text'],value['stable'],cmd['options']['context'])
            return value
        finally:self.active=None
    async def stop(self):
        self.states.clear();self.process=None


@pytest.fixture
def qwen_cache(clean_database,monkeypatch,tmp_path,request):
    from app import main
    folder,path=manifest(tmp_path);worker=Worker();streams={};originals={};successful=set()
    original_load=main.load_config
    def configured(path=None):
        base=original_load(path);data=base.model_dump()
        data['asr'].update(provider='qwen3_mlx',model_id='fixture-qwen-cache',execution_slots=1,
            model_path=str(folder),model_manifest_path=str(tmp_path/'manifest.json'))
        data['retry'].update(max_cached_sessions=20,audio_ttl_seconds=600,audio_budget_bytes=SESSION_PCM_RESERVE)
        data['llm']['provider']='mock'
        checked=Config.model_validate(data)
        return base.model_copy(update={'asr':checked.asr,'retry':checked.retry,'llm':checked.llm})
    def build(config):
        p=Qwen3MlxProvider(config.asr,queue_limit=config.effective_admission_ceiling,worker=worker)
        actual_open=p.open
        async def opened(options):
            stream=await actual_open(options);streams[(options.session_id,options.attempt_no)]=stream
            finish=stream.finish
            async def failed_after_native(total,deadline):
                answer=await finish(total,deadline)
                if (options.session_id,options.attempt_no) not in successful:
                    raise ProviderError('ASR_FAILED','declared fictional final-delivery failure')
                return answer
            stream.finish=failed_after_native
            return stream
        p.open=opened;return p
    monkeypatch.setattr(main,'load_config',configured);monkeypatch.setattr(main,'build_asr',build)
    client=request.getfixturevalue('app_client');manager=client.app.state.sessions
    create=manager.create
    async def captured(*args,**kwargs):
        row,created=await create(*args,**kwargs)
        if created:originals[row.id]=manager.runtimes[row.id].audio
        return row,created
    monkeypatch.setattr(manager,'create',captured)
    assert client.app.state.config.retry.audio_ttl_seconds==600
    assert client.app.state.config.retry.max_cached_sessions==20
    return client,worker,streams,originals,successful


def failed(client,headers,samples):
    response=create_session(client,headers,key());assert response.status_code==201,response.text
    session=response.json();events=[]
    with client.websocket_connect('/ws/v1/sessions/'+session['session_id']+'/audio',headers=headers) as ws:
        assert ws.receive_json()['type']=='ready'
        seq=0
        for offset in range(0,samples,4000):
            n=min(4000,samples-offset)
            ws.send_bytes(struct.pack('<II',seq,n)+struct.pack('<h',100)*n)
            while True:
                event=ws.receive_json();events.append(event)
                assert event['type']!='error', {'seq':seq,'offset_samples':offset,'frame_samples':n,
                    'error':event.get('error'),'runtime_samples':client.app.state.sessions.runtimes.get(session['session_id']).samples
                        if session['session_id'] in client.app.state.sessions.runtimes else None}
                if event['type']=='audio.ack':break
            assert event['seq']==seq and event['total_samples']==offset+n
            seq+=1
            if (offset+n)%32000==0:
                # Capacity/byte-boundary fixture, not real-time throughput:
                # let the actual adapter consume each full native block.
                async def decoded():
                    async with asyncio.timeout(3):
                        stream=client.app.state.sessions.runtimes[session['session_id']].asr
                        while stream.decoded_samples<offset+n:
                            stream._check();await asyncio.sleep(.001)
                client.portal.call(decoded)
        ws.send_json({'type':'audio.end','last_seq':seq-1,'total_samples':samples})
        while True:
            event=ws.receive_json();events.append(event)
            if event['type']=='error':break
            assert event['type']!='result.ready'
    assert events[-1]['error']['code']=='ASR_FAILED'
    detail=wait_state(client,headers,session['session_id'],{'failed'})
    assert detail['received_samples']==samples and detail['retry_kind']=='asr'
    runtime=client.app.state.sessions.runtimes[session['session_id']]
    assert len(runtime.audio)==samples*2 and runtime.asr is None
    return session,runtime


def assert_released(worker,streams,sid,audio):
    assert len(audio)==0
    assert all(not stream.pending and stream.cancelled for (s,_),stream in streams.items() if s==sid)
    assert not any(owner[1]==sid for owner in worker.states)


def test_qwen_failure_cache_count_twenty_then_twenty_one_evicts_original_objects(qwen_cache):
    client,worker,streams,originals,_=qwen_cache
    headers=auth_headers(register_and_login(client,email='qwen-cache-count@example.invalid'))
    sessions=[];expiries={}
    for i in range(21):
        session,runtime=failed(client,headers,8000);sid=session['session_id']
        sessions.append(sid);expiries[sid]=runtime.audio_lease.expires_at
        if i==19:assert sum(bool(r.audio) for r in client.app.state.sessions.runtimes.values())==20
    oldest=min(sessions,key=lambda sid:(expiries[sid],sid));assert oldest==sessions[0]
    assert_released(worker,streams,oldest,originals[oldest])
    cached={sid for sid,r in client.app.state.sessions.runtimes.items() if r.audio and r.state=='failed'}
    assert cached==set(sessions[1:]) and len(cached)==20
    detail=client.get('/api/v1/sessions/'+oldest,headers=headers).json()
    assert detail['retry_kind']=='none' and detail['retry_unavailable_reason']=='audio_evicted'
    assert all(len(originals[sid])==16000 for sid in cached)


@pytest.mark.parametrize('tail',[1,4000])
def test_qwen_cache_exact_byte_budget_and_one_sample_excess(qwen_cache,tail):
    client,worker,streams,originals,_=qwen_cache
    headers=auth_headers(register_and_login(client,email='qwen-cache-bytes@example.invalid'))
    long,a=failed(client,headers,2880000-tail);short,b=failed(client,headers,tail)
    assert len(a.audio)+len(b.audio)==SESSION_PCM_RESERVE==5760000
    assert a.audio_lease.expires_at<b.audio_lease.expires_at
    extra,c=failed(client,headers,1)
    assert_released(worker,streams,long['session_id'],originals[long['session_id']])
    cached=[r for r in client.app.state.sessions.runtimes.values() if r.audio and r.state=='failed']
    assert {r.session_id for r in cached}=={short['session_id'],extra['session_id']}
    assert sum(len(r.audio) for r in cached)==2*(tail+1) and len(cached)<20
    assert len(b.audio)==2*tail and len(c.audio)==2
    detail=client.get('/api/v1/sessions/'+long['session_id'],headers=headers).json()
    assert detail['retry_kind']=='none' and detail['retry_unavailable_reason']=='audio_evicted'


@pytest.mark.parametrize('outcome',['cancel','success'])
def test_qwen_retry_pool_transfer_repeat_failure_then_release_keeps_original_lease(qwen_cache,outcome):
    client,worker,streams,originals,successful=qwen_cache
    headers=auth_headers(register_and_login(client,email='qwen-cache-transfer@example.invalid'))
    session,first=failed(client,headers,8000);sid=session['session_id'];audio=originals[sid]
    lease=first.audio_lease;d=lease.deadline;epoch=lease.expires_at
    first_failed=fetch_one('select first_asr_failed_at from sessions where id=$1',sid)
    assert epoch==first_failed.timestamp()+600
    for attempt in (2,3):
        worker.entered=asyncio.Event();worker.release=asyncio.Event()
        worker.gate=(first.user_id,sid,attempt)
        if attempt==3 and outcome=='success':successful.add((sid,attempt))
        idem=key();response=client.post('/api/v1/sessions/'+sid+'/retry',
            json={'retry_kind':'asr'},headers={**headers,'Idempotency-Key':idem})
        assert response.status_code==202 and response.json()['current_attempt_no']==attempt
        async def entered():
            async with asyncio.timeout(3):await worker.entered.wait()
        client.portal.call(entered)
        runtime=client.app.state.sessions.runtimes[sid]
        assert runtime.audio is audio and runtime.audio_lease is lease and lease.owner is runtime
        assert lease.deadline==d and lease.expires_at==epoch and first.audio==b''
        assert len(audio)==16000 and runtime.state=='finalizing'
        assert sum(len(r.audio) for r in client.app.state.sessions.runtimes.values() if r.state=='failed')==0
        assert sum(len(r.audio) for r in client.app.state.sessions.runtimes.values() if r.state in {'created','streaming','finalizing','refining'})==16000
        assert fetch_one('select first_asr_failed_at from sessions where id=$1',sid)==first_failed
        if attempt==2:
            client.portal.call(worker.release.set)
            state=wait_state(client,headers,sid,{'failed'})
            assert state['retry_kind']=='asr' and runtime.audio is audio and runtime.audio_lease is lease
            assert len(audio)==16000 and lease.expires_at==epoch and lease.deadline==d
            assert not streams[(sid,attempt)].pending and worker.active is None
            before=len(worker.calls)
            replay=client.post('/api/v1/sessions/'+sid+'/retry',json={'retry_kind':'asr'},
                headers={**headers,'Idempotency-Key':idem})
            assert replay.status_code==200 and replay.json()['replayed'] is True
            assert len(worker.calls)==before
        elif outcome=='cancel':
            response=client.post('/api/v1/sessions/'+sid+'/cancel',headers=headers)
            assert response.status_code==200 and response.json()['state']=='cancelled'
        else:
            client.portal.call(worker.release.set)
            assert wait_state(client,headers,sid,{'ready'})['result']['review_required'] is False
        first=runtime
    assert_released(worker,streams,sid,audio)
    assert worker.active is None and client.app.state.asr.engine._audio_inputs=={}
    assert client.app.state.asr.scheduler.snapshot()=={'running':0,'high_waiting':0,'low_waiting':0}
    assert fetch_one('select received_samples from sessions where id=$1',sid)==8000
    assert fetch_one('select counted_session from session_contributions where session_id=$1',sid)==1
    assert fetch_one('select count(*) from results where session_id=$1',sid)==(1 if outcome=='success' else 0)
