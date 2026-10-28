"""Synthetic protocol diagnostics, never human/native/model capacity evidence."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import struct
import sys
import time
from types import SimpleNamespace
import uuid
import wave

import httpx
from fastapi import FastAPI, Request, WebSocket
import pytest
import uvicorn

PATH = Path(__file__).resolve().parents[3]/'scripts/acceptance/capacity_stream_runner.py'
spec = importlib.util.spec_from_file_location('capacity_stream_runner_under_test', PATH)
T = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = T
spec.loader.exec_module(T)


class Log:
    def __init__(self): self.events = []
    def write(self, lane, event, **fields): self.events.append((lane,event,fields))


def identity(n=0):
    return T.Identity(f'lane-{n}', T.CODES[n], f'private-access-{n}', f'private-refresh-{n}', time.monotonic()+900)


def clip(n=0, kind='A10', samples=10000):
    pcm = b'\x01\x00'*samples
    return T.Audio(f'audio-{n}',identity(n).code,kind,'synthetic','0'*64,pcm)


def test_failed_and_fallback_observations_stay_in_denominator():
    rows=[{'outcome':'ready','server_stop_to_result_ms':i} for i in range(19)]
    rows.append({'outcome':'fallback','server_stop_to_result_ms':1})
    r=T.group_summary(rows)
    assert (r['observations'],r['ready'],r['failed_or_fallback'],r['p50_ms'],r['p95_ms'])==(20,19,1,9,18)
    rows.append({'outcome':'error'})
    assert T.group_summary(rows)['p95_ms'] is None
    assert r['capacity_validation']=='unverified' and r['semantic_review']=='pending'


@pytest.mark.parametrize('url', ['http://127.0.0.1:0','http://localhost:80','http://192.168.1.1',
    'http://user:secret@127.0.0.1','https://example.com/?secret=x','http://127.0.0.1/private'])
def test_reject_credential_or_unsafe_destination_before_network(url):
    with pytest.raises(ValueError): T.base_url(url)


@pytest.mark.parametrize('url', ['http://127.0.0.1:1234','http://[::1]:1234','https://example.com'])
def test_valid_configured_destination(url): assert T.base_url(url)==url


@pytest.mark.asyncio
async def test_login_rate_limit_is_preparation_and_never_logged(monkeypatch):
    requests=[];delays=[]
    async def sleep(delay): delays.append(delay)
    monkeypatch.setattr(T.asyncio,'sleep',sleep)
    def handler(request):
        requests.append(request)
        if len(requests)==1: return httpx.Response(429,headers={'Retry-After':'2'})
        return httpx.Response(200,json={'access_token':'private-access','refresh_token':'private-refresh','expires_in':900})
    async with httpx.AsyncClient(base_url='http://127.0.0.1',transport=httpx.MockTransport(handler)) as client:
        account={'lane':'a','capacity_code':'青松','email':'synthetic@example.test','password':'private-password','device':{}}
        first=await T.authenticate(client,account=account)
        second=await T.authenticate(client,previous=first)
    assert delays==[2] and len(requests)==3
    assert requests[-1].url.path=='/api/auth/refresh'
    assert json.loads(requests[-1].content)=={'refresh_token':'private-refresh'}
    assert 'private' not in repr(first) and 'private' not in repr(second)


class Peer:
    """In-process counterexamples plus a real-time ACK-loss boundary."""
    close_code=1000
    def __init__(self, mode='ok'):
        self.queue=asyncio.Queue();self.mode=mode;self.frames=[]
    async def recv(self): return json.dumps(await self.queue.get())
    async def send(self, value):
        if isinstance(value,bytes):
            seq,count=struct.unpack('<II',value[:8]);self.frames.append((seq,count,time.monotonic(),value[8:]))
            if self.mode=='no_ack': return
            message={'type':'audio.ack','session_id':'sid','seq':seq,'total_samples':sum(f[1] for f in self.frames)}
            if self.mode=='cross': message['session_id']='wrong'
            if self.mode=='ack_range': message['total_samples']+=1
            await self.queue.put(message)
            if self.mode=='prefix':
                for revision,text in ((1,'青松'),(2,'白杨')):
                    await self.queue.put({'type':'asr.partial','session_id':'sid','revision':revision,'stable_text':text,
                        'received_samples':count,'decoded_samples':count})
        elif self.mode!='no_ack':
            end=json.loads(value)
            for message in ({'type':'audio.end_ack','last_seq':end['last_seq'],'total_samples':end['total_samples']},
                {'type':'asr.final','text':'青松'},
                {'type':'result.ready','result':{'attempt_no':1,'attempt_kind':'initial','raw_text':'青松'}}):
                await self.queue.put({'session_id':'sid',**message})
    async def wait_closed(self): pass


@pytest.mark.asyncio
async def test_natural_partial_tail_not_padded_or_compressed():
    peer=Peer();start=time.monotonic()
    _,r=await T.stream_audio(peer,clip(),start,Log(),identity(),'sid')
    assert [f[:2] for f in peer.frames]==[(0,4000),(1,4000),(2,2000)]
    assert .625<=r['end_monotonic']-r['record_start_monotonic']<1.2
    assert peer.frames[-1][2]-peer.frames[-2][2]>=.125
    assert r['sent_samples']==r['acked_samples']==10000


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,code',[('cross','CROSS_SESSION_EVENT'),('ack_range','ACK_RANGE'),('prefix','STABLE_PREFIX')])
async def test_bad_protocol_cannot_be_counted_as_success(mode,code):
    with pytest.raises(T.LoadError,match=code):
        await T.stream_audio(Peer(mode),clip(),time.monotonic(),Log(),identity(),'sid')


@pytest.mark.asyncio
async def test_last_frame_ack_loss_stops_at_original_two_second_deadline():
    peer=Peer('no_ack');start=time.monotonic()
    with pytest.raises(T.LoadError,match='ACK_DEADLINE'):
        await T.stream_audio(peer,clip(samples=4000),start,Log(),identity(),'sid')
    elapsed=time.monotonic()-peer.frames[0][2]
    assert 2<=elapsed<3, 'A receiver already waiting before the first frame must still enforce ACK deadline'


@asynccontextmanager
async def endpoint(mode='ok'):
    """Actual loopback TCP/HTTP/WS with invented responses, no production DB/model."""
    app=FastAPI();state={};frames={};cancelled=[];disconnected=[]
    @app.post('/api/auth/login')
    async def login(request:Request):
        body=await request.json();n=int(body['email'].split('@')[0].rsplit('-',1)[1])
        assert body['client_type']=='desktop' and body['password']=='private-password'
        return {'access_token':f'private-access-{n}','refresh_token':f'private-refresh-{n}','expires_in':900}
    @app.post('/api/v1/sessions')
    async def create(request:Request):
        body=await request.json()
        assert request.headers['Idempotency-Key']==body['client_request_id']
        n=int(request.headers['Authorization'].rsplit('-',1)[1])
        sid=str(uuid.uuid4());state[sid]={'state':'created','n':n};frames[sid]=[]
        from fastapi.responses import JSONResponse
        return JSONResponse({'session_id':sid},status_code=201)
    @app.get('/api/v1/sessions/{sid}')
    async def get(sid:str):
        if mode=='cleanup_fail' and state[sid].get('read'):
            from fastapi.responses import JSONResponse
            return JSONResponse({},status_code=503)
        state[sid]['read']=True
        return state[sid]
    @app.post('/api/v1/sessions/{sid}/cancel')
    async def cancel(sid:str):
        cancelled.append(sid);state[sid]['state']='cancelled';return state[sid]
    @app.websocket('/ws/v1/sessions/{sid}/audio')
    async def ws(websocket:WebSocket,sid:str):
        await websocket.accept()
        async def emit(kind,**fields): await websocket.send_json({'type':kind,'session_id':sid,**fields})
        try:
            await emit('ready',attempt_no=1)
            state[sid]['state']='streaming';total=0
            while True:
                message=await websocket.receive()
                if message['type']=='websocket.disconnect': return
                if message.get('bytes') is not None:
                    raw=message['bytes'];seq,count=struct.unpack('<II',raw[:8]);total+=count
                    assert seq==len(frames[sid]) and count*2==len(raw)-8
                    if mode=='delay_first_observation' and not frames[sid]:
                        # Declared receiver scheduling delay, not a faster
                        # recording or a change to the sender's 250 ms rule.
                        await asyncio.sleep(.1)
                    frames[sid].append((time.monotonic(),count))
                    if mode=='cross':
                        await websocket.send_json({'type':'audio.ack','session_id':'foreign','seq':seq,'total_samples':total})
                        # Deliberately leave this own session streaming. Wait
                        # for driver disconnect, then verify HTTP cancellation.
                        while (await websocket.receive())['type']!='websocket.disconnect': pass
                        return
                    else: await emit('audio.ack',seq=seq,total_samples=total)
                    if len(frames[sid])==1:
                        await emit('asr.partial',revision=1,stable_text=identity(state[sid]['n']).code,
                                   received_samples=total,decoded_samples=total)
                else:
                    end=json.loads(message['text']);assert end=={'type':'audio.end','last_seq':len(frames[sid])-1,'total_samples':total}
                    n=state[sid]['n'];text=identity(n).code+'虚构测试'
                    if mode=='foreign_text':text+='白杨' if n==0 else '青松'
                    result={'result_id':str(uuid.uuid4()),'attempt_no':1,'attempt_kind':'initial','raw_text':text,'refined_text':text,
                            'review_required':False,'text_hash':hashlib.sha256(text.encode()).hexdigest(),'stop_to_result_ms':3}
                    state[sid].update(state='ready',current_attempt_no=1,received_samples=total,audio_complete=True,result=result)
                    await emit('audio.end_ack',last_seq=end['last_seq'],total_samples=total)
                    await emit('asr.final',text=text)
                    await emit('result.ready',result=result)
                    await websocket.close(1000);return
        finally: disconnected.append(sid)
    # Linux keeps closed accepted connections in TIME_WAIT. Both the listener
    # and the independent rebind probe need reuse enabled; the probe still
    # fails if an actual listener remains alive.
    sock=socket.socket();sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
    sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,log_level='error',lifespan='off'))
    task=asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done(): await task
                await asyncio.sleep(.01)
        async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}',trust_env=False) as client:
            yield client,str(client.base_url).rstrip('/'),state,frames,cancelled,disconnected
    finally:
        server.should_exit=True
        async with asyncio.timeout(5): await task
        sock.close()
        check=socket.socket()
        try:
            check.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);check.bind(('127.0.0.1',port))
        finally: check.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('load',['staggered','burst'])
@pytest.mark.parametrize('mode',['ok','delay_first_observation'])
async def test_real_tcp_two_lanes_with_actual_pacing_and_readback(load,mode,record_property):
    async with endpoint(mode) as (client,url,state,frames,cancelled,disconnected):
        journal=Log()
        r=await T.run_round(client,url,[identity(0),identity(1)],[clip(0,'A30',8000),clip(1,'A30',12000)],load,journal)
        assert r['summary']['ready']==2 and r['all_recordings_overlap']
        assert r['all_prepared'] and r['burst_window_valid']
        assert r['summary']['capacity_validation']=='unverified'
        assert all(row['session_terminal_confirmed'] and row['stable_before_end'] for row in r['lanes'])
        assert sorted(sum(x[1] for x in f) for f in frames.values())==[8000,12000]
        gaps=[b[0]-a[0] for f in frames.values() for a,b in zip(f,f[1:])]
        sent={lane:[entry for name,event,entry in journal.events if name==lane and event=='ws.send_frame']
              for lane in ('lane-0','lane-1')}
        send_gaps=[b['send_started_at']-a['send_started_at'] for f in sent.values() for a,b in zip(f,f[1:])]
        assert all(gap>=.25 for gap in send_gaps), {'actual_send_intervals':send_gaps}
        assert all(entry['send_started_at']>=entry['target'] for f in sent.values() for entry in f)
        if mode=='delay_first_observation':
            assert any(gap<.25 for gap in gaps), 'control must expose receiver scheduling compression'
        record_property('pacing_observation',json.dumps({'mode':mode,'load':load,
            'actual_send_intervals':send_gaps,'server_receive_intervals':gaps}))
        assert len(disconnected)==2 and not cancelled and all(s['state']=='ready' for s in state.values())


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,reason',[('cross','CROSS_SESSION_EVENT'),('foreign_text','CAPACITY_CODE_MISMATCH'),
                                      ('cleanup_fail','SESSION_TERMINAL_UNCONFIRMED')])
async def test_real_tcp_errors_stay_failed_and_owned_sessions_close(mode,reason):
    async with endpoint(mode) as (client,url,state,frames,cancelled,disconnected):
        r=await T.run_round(client,url,[identity(0),identity(1)],[clip(0,samples=4000),clip(1,samples=4000)],'burst',Log())
        assert r['summary']['ready']==0 and r['summary']['failed_or_fallback']==2 and r['summary']['p95_ms'] is None
        assert all(row['reason']==reason for row in r['lanes'])
        if mode=='cross': assert set(cancelled)==set(state) and all(s['state']=='cancelled' for s in state.values())
        if mode=='cleanup_fail': assert not any(row['session_terminal_confirmed'] for row in r['lanes'])
        assert set(disconnected)==set(state)


@pytest.mark.asyncio
async def test_no_silent_zip_truncation():
    with pytest.raises(T.LoadError,match='ROUND_INPUTS'):
        await T.run_round(None,'unused',[identity() ],[],'burst',Log())


def inputs(folder,sessions=2):
    recordings=[];accounts=[]
    for n in range(sessions):
        for kind in ('A10','A30'):
            path=folder/f'synthetic-{n}-{kind}.wav'
            with wave.open(str(path),'wb') as stream:
                stream.setnchannels(1);stream.setsampwidth(2);stream.setframerate(16000)
                stream.writeframes(b'\x01\x00'*8000)
            recordings.append({'id':path.stem,'speaker_id':f'synthetic-{n}','kind':kind,'capacity_code':identity(n).code,
                               'file':path.name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'samples':8000})
        accounts.append({'lane':identity(n).lane,'capacity_code':identity(n).code,'email':f'synthetic-{n}@example.test',
                         'password':'private-password','device':{'installation_id':str(uuid.uuid4()),'name':'synthetic',
                         'platform':'macos','client_version':'0.0.1'}})
    manifest=folder/'inventory.json'
    manifest.write_text(json.dumps({'schema':'svi-capacity-audio-inventory-v1','recordings':recordings,'extra_capacity_codes':[]}))
    credentials=folder/'accounts.private.json'
    credentials.write_text(json.dumps({'schema':'svi-capacity-run-accounts-v1','accounts':accounts}));credentials.chmod(0o600)
    return manifest,credentials


@pytest.mark.parametrize('corruption,reason',[('order','ACCOUNT_CODE_ORDER'),('duplicate','ACCOUNT_DUPLICATE'),
                                           ('device','DEVICE_SCHEMA'),('changed_wave','AUDIO_DIGEST_MISMATCH')])
def test_invalid_inputs_fail_before_any_login(tmp_path,corruption,reason):
    manifest,credentials=inputs(tmp_path);data=json.loads(credentials.read_text())
    if corruption=='order':data['accounts'].reverse()
    if corruption=='duplicate':data['accounts'][1]['email']=data['accounts'][0]['email']
    if corruption=='device':data['accounts'][0]['device']={}
    if corruption=='changed_wave':
        path=tmp_path/json.loads(manifest.read_text())['recordings'][0]['file']
        with path.open('ab') as stream:stream.write(b'changed')
    credentials.write_text(json.dumps(data))
    with pytest.raises(ValueError,match=reason):T.load_inputs(manifest,credentials,2)


@pytest.mark.asyncio
@pytest.mark.parametrize('sessions',[2,20])
async def test_four_group_real_tcp_execution_never_grants_capacity(tmp_path,capsys,sessions):
    manifest,credentials=inputs(tmp_path,sessions)
    output=tmp_path/'new-run'
    async with endpoint() as (client,url,state,frames,cancelled,disconnected):
        result=await T.execute(SimpleNamespace(base_url=url,manifest=manifest,credentials=credentials,
                           sessions=sessions,rounds=1,out_dir=output))
        assert result['completed'] and result['protocol_run_passed']
        assert [(g['load'],g['kind']) for g in result['groups']]==[
            ('staggered','A10'),('staggered','A30'),('burst','A10'),('burst','A30')]
        assert len(state)==len(disconnected)==4*sessions and not cancelled
        assert all(g['summary']['observations']==sessions and g['summary']['ready']==sessions for g in result['groups'])
    assert result['capacity_validation']=='unverified' and not result['acceptance_pass_claimed']
    assert len(result['not_verified'])==4
    public=(output/'result.json').read_text()+capsys.readouterr().out
    private=(output/'trace.private.jsonl').read_text()
    assert '虚构测试' in private and '虚构测试' not in public
    assert all(secret not in public+private for secret in ('private-access','private-refresh','private-password','example.test'))
    assert output.stat().st_mode&0o777==0o700
    assert (output/'trace.private.jsonl').stat().st_mode&0o777==0o600
    original=(output/'result.json').read_bytes()
    with pytest.raises(FileExistsError):
        await T.execute(SimpleNamespace(base_url='http://127.0.0.1:1',manifest=manifest,credentials=credentials,
                                       sessions=sessions,rounds=1,out_dir=output))
    assert (output/'result.json').read_bytes()==original
