"""Fictional native-state control; these tests do not load Qwen/MLX."""
import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from app.core.clock import loop_time
from app.core.config import AsrConfig, Config
from app.providers import build_asr
from app.providers.asr.base import AsrOpenOptions, ProviderError
from app.providers.asr.qwen3_mlx import Qwen3MlxProvider
from app.providers.asr.qwen_manifest import validate_model_manifest
from app.services.asr_core import DecodeWorker
from tests.qwen_native_fixtures import native_checkpoint


class Process:
    def is_alive(self):return True


class NativeControl:
    stop_incomplete=False
    def __init__(self):
        self.process=None;self.states={};self.calls=[];self.block=None;self.entered=asyncio.Event()
        self.bad=False;self.stops=0
        self.epoch=0;self.exited=asyncio.Event()
    async def decode(self,args,deadline):
        pcm,cmd=args;action=cmd['action']
        self.calls.append((bytes(pcm),dict(cmd)))
        if action=='prepare':
            self.exited=asyncio.Event()
            self.process=Process();return {'ready':True,'runtime':'fictional','mlx':'fictional'}
        if action=='drop':
            self.states.pop(cmd['owner'],None);return {'dropped':True}
        key=cmd['owner']
        if cmd.get('checkpoint') is not None:
            assert key not in self.states
            checkpoint=cmd['checkpoint']
            assert checkpoint['owner']==key and checkpoint['samples']==cmd['start_sample']
            self.states[key]=checkpoint['samples']
        prior=self.states.get(key,0)
        assert cmd['start_sample']==prior
        self.entered.set()
        epoch=self.epoch;exited=self.exited
        if self.block:
            waits=[asyncio.create_task(self.block.wait()),asyncio.create_task(exited.wait())]
            try:await asyncio.wait(waits,return_when=asyncio.FIRST_COMPLETED)
            finally:
                for wait in waits:wait.cancel()
                await asyncio.gather(*waits,return_exceptions=True)
        if epoch!=self.epoch:raise RuntimeError('fictional owned generation exited')
        count=prior+len(pcm)//2
        if isinstance(pcm,bytearray):pcm.clear()
        self.states[key]=count
        text=key[0]+'-'+str(count)
        # Stable prefix independent of tail size, unlike a mutable numeric suffix.
        stable=key[0] if count else ''
        if self.bad:stable='FOREIGN'
        if action=='finish':self.states.pop(key,None)
        value={'text':text,'stable':stable,'samples':count}
        if action=='feed':value['checkpoint']=native_checkpoint(key,count,text,stable,cmd['options']['context'])
        return value
    async def stop(self):
        self.stops+=1;self.states.clear();self.process=None;self.epoch+=1;self.exited.set()


def manifest(tmp_path):
    folder=tmp_path/'model';folder.mkdir()
    body=b'{"fixture":true}';(folder/'config.json').write_bytes(body)
    path=tmp_path/'manifest.json'
    path.write_text(json.dumps({'schema':'svi-qwen3-asr-manifest-v1','files':[
        {'file':'config.json','bytes':len(body),'sha256':hashlib.sha256(body).hexdigest()}]}))
    return folder,path


async def prepared(tmp_path):
    folder,path=manifest(tmp_path);worker=NativeControl()
    p=Qwen3MlxProvider(AsrConfig(provider='qwen3_mlx',model_id='fictional-qwen',
        model_path=str(folder),model_manifest_path=str(path)),worker=worker)
    await p.prepare()
    return p,worker


async def until(check):
    async with asyncio.timeout(2):
        while not check():await asyncio.sleep(.001)


async def feed(stream,samples,start=0):
    for pos in range(start,start+samples,4000):
        n=min(4000,start+samples-pos)
        await stream.feed(b'\x01\x00'*n,pos)


async def test_real_streaming_counts_tail_once_and_isolates_two_users(tmp_path):
    p,w=await prepared(tmp_path)
    try:
        a=await p.open(AsrOpenOptions('same-session',1,user_id='A',hint_terms=['React']))
        b=await p.open(AsrOpenOptions('same-session',1,user_id='B',hint_terms=['Docker']))
        await feed(a,32000);await feed(b,32000)
        await until(lambda:a.decoded_samples==b.decoded_samples==32000)
        pa=await anext(a.partials());pb=await anext(b.partials())
        assert pa.stable_text=='A' and pb.stable_text=='B'
        await feed(a,123,start=32000)
        ra=await a.finish(32123,loop_time()+2);rb=await b.finish(32000,loop_time()+2)
        assert (ra.text,ra.processed_samples)==('A-32123',32123)
        assert (rb.text,rb.processed_samples)==('B-32000',32000)
        assert not a.pending and not b.pending and w.states=={}
        calls=[(len(x)//2,c['action'],c['owner'][0]) for x,c in w.calls if c['action'] in {'feed','finish'}]
        assert sorted(calls)==sorted([(32000,'feed','A'),(123,'finish','A'),(32000,'feed','B'),(0,'finish','B')])
        assert p.scheduler.snapshot()=={'running':0,'high_waiting':0,'low_waiting':0}
    finally:await p.close()


async def test_feed_ack_does_not_wait_for_model_and_cancel_clears_before_exit(tmp_path):
    p,w=await prepared(tmp_path);w.block=asyncio.Event()
    a=await p.open(AsrOpenOptions('first',1,user_id='A'))
    b=await p.open(AsrOpenOptions('second',1,user_id='B'))
    try:
        started=loop_time();await feed(a,32000)
        assert loop_time()-started<.1
        await w.entered.wait()
        buffer=p.engine._audio_inputs[a.owner]
        await a.cancel()
        assert len(buffer)==0 and a.pending==b.pending==b'' and w.stops>=1
        assert b.error is None and b.received_samples==b.decoded_samples==0
        await until(lambda:p.engine.ready)
        w.block=None;w.entered.clear()
        await feed(b,123)
        rb=await b.finish(123,loop_time()+2)
        assert rb.text=='B-123' and rb.processed_samples==123
        c=await p.open(AsrOpenOptions('third',1,user_id='C'))
        await feed(c,123)
        r=await c.finish(123,loop_time()+2)
        assert r.text=='C-123' and r.processed_samples==123
    finally:w.block=None;await p.close()


async def test_cancel_idle_session_drops_only_its_state(tmp_path):
    p,w=await prepared(tmp_path)
    try:
        a=await p.open(AsrOpenOptions('a',1,user_id='A'));b=await p.open(AsrOpenOptions('b',1,user_id='B'))
        await feed(a,32000);await feed(b,32000)
        await until(lambda:a.decoded_samples==b.decoded_samples==32000)
        await a.cancel()
        assert a.owner not in w.states and b.owner in w.states and w.stops==0
        assert (await b.finish(32000,loop_time()+2)).text=='B-32000'
    finally:await p.close()


async def test_invalid_stable_prefix_is_not_published_as_success(tmp_path):
    p,w=await prepared(tmp_path);w.bad=True
    try:
        a=await p.open(AsrOpenOptions('a',1,user_id='A'))
        await feed(a,32000)
        with pytest.raises(ProviderError):await anext(a.partials())
        assert a.error and not a.pending
        assert a.error.code == 'invalid_output'
        with pytest.raises(ProviderError):await a.finish(32000,loop_time()+2)
    finally:await p.close()


@pytest.mark.parametrize('kind',['sequence','odd','limit','finish_count','duplicate_open'])
async def test_native_input_boundaries_fail_explicitly(tmp_path,kind):
    p,w=await prepared(tmp_path)
    try:
        options=AsrOpenOptions('a',1,user_id='A',max_samples=100)
        a=await p.open(options)
        with pytest.raises(ProviderError):
            if kind=='sequence':await a.feed(b'\0\0',1)
            elif kind=='odd':await a.feed(b'\0',0)
            elif kind=='limit':await a.feed(bytes(202),0)
            elif kind=='finish_count':await a.finish(1,loop_time()+2)
            else:await p.open(options)
        assert len([c for _,c in w.calls if c['action']!='prepare'])==0
    finally:await p.close()


async def test_exact_chunk_finish_has_no_fictitious_second_model_call(tmp_path):
    p,w=await prepared(tmp_path)
    class Recorder:
        def __init__(self):self.started_calls=[];self.outcomes=[]
        async def started(self,*args):self.started_calls.append(args);return 'fictional-id'
        async def finished(self,*args,**kwargs):self.outcomes.append((args,kwargs))
    recorder=Recorder();p.recorder=recorder
    try:
        a=await p.open(AsrOpenOptions('a',1,user_id='A'))
        await feed(a,32000);await until(lambda:a.decoded_samples==32000)
        await a.finish(32000,loop_time()+2)
        assert len(recorder.started_calls)==len(recorder.outcomes)==1
        assert recorder.started_calls[0][1:]==('qwen3_mlx',p.engine.model_id,'asr_decode',32000)
    finally:await p.close()


@pytest.mark.parametrize('mutation',['digest','extra','escape','size','manifest_sha'])
def test_model_manifest_confines_exact_loader_files(tmp_path,mutation):
    folder,path=manifest(tmp_path)
    original=json.loads(path.read_text())
    assert len(validate_model_manifest(folder,path))==64
    if mutation=='digest':original['files'][0]['sha256']='0'*64
    elif mutation=='extra':(folder/'model.safetensors').write_bytes(b'fictional')
    elif mutation=='escape':original['files'][0]['file']='../other.json'
    elif mutation=='size':original['files'][0]['bytes']+=1
    path.write_text(json.dumps(original))
    with pytest.raises(ValueError):validate_model_manifest(folder,path,'0'*64 if mutation=='manifest_sha' else '')


def test_qwen_factory_does_not_silently_support_multiple_execution_slots(tmp_path):
    folder,path=manifest(tmp_path)
    with pytest.raises(ValueError,match='execution_slots'):
        build_asr(Config(asr={'provider':'qwen3_mlx','model_path':str(folder),
            'model_manifest_path':str(path),'execution_slots':2}))


async def test_live_backlog_is_bounded_without_blocking_ack(tmp_path):
    p,w=await prepared(tmp_path);w.block=asyncio.Event()
    try:
        a=await p.open(AsrOpenOptions('a',1,user_id='A'))
        await feed(a,80000)
        with pytest.raises(ProviderError,match='backlog'):await a.feed(b'\1\0'*4000,80000)
        assert not a.pending and a.error
        await a.cancel()
    finally:w.block=None;await p.close()


async def test_retry_replay_uses_backpressure_within_original_deadline(tmp_path):
    p,w=await prepared(tmp_path)
    try:
        a=await p.open(AsrOpenOptions('a',2,user_id='A',
            outcome_deadline=loop_time()+2,eligibility_deadline=loop_time()+1.8))
        await feed(a,128123)
        r=await a.finish(128123,loop_time()+1)
        assert r.processed_samples==128123 and r.text=='A-128123' and not a.pending
    finally:await p.close()


def transport_control(connection):
    try:
        while True:
            pcm,metadata=connection.recv()
            connection.send((True,{'samples':len(pcm)//2,'identity':metadata['identity']}))
    except EOFError:pass
    finally:connection.close()


async def test_owned_transport_can_run_a_native_target_and_confirm_exit():
    import os
    w=DecodeWorker(target=transport_control)
    try:
        assert await w.decode((bytes(246),{'identity':'fictional-A'}),loop_time()+3)=={'samples':123,'identity':'fictional-A'}
        pid=w.process.pid
        assert await w.decode((bytes(400),{'identity':'fictional-B'}),loop_time()+3)=={'samples':200,'identity':'fictional-B'}
        assert w.process.pid==pid
        await w.stop()
        assert w.process is w.connection is None
        with pytest.raises(ProcessLookupError):os.kill(pid,0)
    finally:await w.stop()


async def test_retry_eligibility_does_not_lose_its_last_second_to_ledger_reserve(tmp_path):
    p,w=await prepared(tmp_path)
    class Recorder:
        def __init__(self):self.calls=[];self.finished_calls=[]
        async def started(self,*args):self.calls.append(args);return 'fictional-call'
        async def finished(self,*args,**kwargs):self.finished_calls.append((args,kwargs))
    r=Recorder();p.recorder=r
    try:
        b,e=loop_time()+2,loop_time()+.2
        a=await p.open(AsrOpenOptions('a',2,user_id='A',outcome_deadline=b,eligibility_deadline=e))
        await feed(a,123)
        out=await a.finish(123,e)
        assert out.processed_samples==123 and len(r.calls)==len(r.finished_calls)==1
        assert r.finished_calls[0][1]['status']=='succeeded'
    finally:await p.close()
