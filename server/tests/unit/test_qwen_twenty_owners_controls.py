"""Exactly 20 native Qwen owners, production scheduler, fictional physical worker."""
import asyncio
import json

from app.core.clock import loop_time
from app.core.config import AsrConfig
from app.providers.asr.base import AsrOpenOptions
from app.providers.asr.qwen3_mlx import Qwen3MlxProvider
from tests.unit.test_qwen_native_provider import NativeControl, feed, manifest, until


class Held(NativeControl):
    def __init__(self):
        super().__init__();self.entered_first=asyncio.Event();self.release_first=asyncio.Event()
        self.entered_owners=[];self.original_audio=[];self.provider=None
    async def decode(self,args,deadline):
        audio,cmd=args;owner=cmd.get('owner')
        if cmd['action'] in ('feed','finish'):
            running=self.provider.scheduler.running
            assert len(running)==1 and owner in running
            assert cmd['options']['context']==f'Term{owner[0].split("-")[-1]}'
            self.entered_owners.append((owner,cmd['action'],len(audio)//2))
            self.original_audio.append(audio)
            if owner[0]=='user-0' and cmd['action']=='feed' and cmd['start_sample']==0:
                self.entered_first.set()
                async with asyncio.timeout_at(deadline):await self.release_first.wait()
        return await super().decode(args,deadline)


async def test_exact_twenty_qwen_owners_hint_isolation_finish_fifo_weighting_and_pending_release(tmp_path,record_property):
    folder,path=manifest(tmp_path);worker=Held()
    provider=Qwen3MlxProvider(AsrConfig(provider='qwen3_mlx',model_id='fictional-twenty-owners',
        model_path=str(folder),model_manifest_path=str(path)),queue_limit=20,worker=worker)
    worker.provider=provider;await provider.prepare();streams=[];jobs=[];old_jobs=[]
    try:
        for i in range(20):
            streams.append(await provider.open(AsrOpenOptions('session-'+str(i),1,user_id='user-'+str(i),hint_terms=['Term'+str(i)])))
        await feed(streams[0],32000);await worker.entered_first.wait()
        await feed(streams[0],32123,start=32000)
        for stream in streams[1:]:await feed(stream,64123)
        await until(lambda:provider.scheduler.snapshot()=={'running':1,'high_waiting':0,'low_waiting':19})
        assert len(provider.engine.sessions)==20
        old_jobs=list(provider.scheduler.queues['low'].values())
        high=[s.owner for s in streams[1::2]];low=[s.owner for s in streams[2::2]]
        for stream in streams[1::2]:jobs.append(asyncio.create_task(stream.finish(64123,loop_time()+10)))
        await until(lambda:all(s.finishing and s.task.done() for s in streams[1::2]) and
            provider.scheduler.snapshot()=={'running':1,'high_waiting':10,'low_waiting':9})
        assert list(provider.scheduler.queues['high'])==high and list(provider.scheduler.queues['low'])==low
        owners=list(provider.scheduler.running)+[o for q in provider.scheduler.queues.values() for o in q]
        assert len(owners)==len(set(owners))==20
        assert all(len(s.pending)//2==64123 and not s.error for s in streams)
        for job in old_jobs:
            if job.owner in high:assert job.done.is_set() and job.operation is None
        worker.release_first.set();finals=await asyncio.gather(*jobs)
        await until(lambda:all(s.decoded_samples==64000 for s in streams[::2]))
        first=[entry for entry in worker.entered_owners if entry[0]!=streams[0].owner]
        assert [o for o,a,n in first if o in high]==high
        assert [o for o,a,n in first if o in low and a=='feed' and n==32000][:9]==low
        assert [index for index,(owner,_,_) in enumerate(first) if owner in low][:3]==[3,7,11]
        assert [r.text for r in finals]==[s.options.user_id+'-64123' for s in streams[1::2]]
        for stream in streams[::2]:
            answer=await stream.finish(64123,loop_time()+10)
            assert answer.text==stream.options.user_id+'-64123' and answer.processed_samples==64123
        assert all(not s.pending and s.done for s in streams)
        assert all(not audio for audio in worker.original_audio)
        assert not worker.states and not provider.engine.sessions and not provider.engine._audio_inputs
        assert provider.scheduler.snapshot()=={'running':0,'high_waiting':0,'low_waiting':0}
        assert worker.stops==0
        record_property('exact_twenty_qwen_owners',json.dumps({'owners':20,'sample_count_each':64123,
            'high_owners':10,'queued_low_owners':9,'initial_running':1,'low_positions_while_both_nonempty':[3,7,11],
            'each_native_context_checked':True,'native_outputs_owner_bound':True,
            'old_low_payloads_released':True,'all_pending_and_work_inputs_clear':True,
            'states_remaining':0,'model_weights_or_native_os':False}))
    finally:
        worker.release_first.set();await asyncio.gather(*jobs,return_exceptions=True);await provider.close()
