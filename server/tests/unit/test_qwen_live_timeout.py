"""Original recoverable-live-partial contract; explicit physical Timeout stub."""
import asyncio
import json

import pytest

from app.core.clock import loop_time
from app.providers.asr.base import AsrOpenOptions, ProviderError
from tests.unit.test_qwen_native_provider import feed, prepared, until


def one_timeout(worker, stream, start, entered):
    original=worker.decode;used=False
    async def once(args, deadline):
        nonlocal used
        if not used and args[1].get('owner')==stream.owner and args[1]['action']=='feed' and args[1]['start_sample']==start:
            used=True;entered.set()
            raise TimeoutError('declared physical live deadline, no model inference')
        return await original(args,deadline)
    worker.decode=once
    return original


@pytest.mark.parametrize('prior_samples',[0,32000])
async def test_live_timeout_keeps_exact_unconfirmed_pcm_and_acknowledged_cursor(tmp_path,record_property,prior_samples):
    provider,worker=await prepared(tmp_path)
    stream=await provider.open(AsrOpenOptions('live',1,user_id='A',hint_terms=['React']))
    try:
        if prior_samples:
            await feed(stream,prior_samples);await until(lambda:stream.decoded_samples==prior_samples)
        snapshot=stream.checkpoint;entered=asyncio.Event()
        one_timeout(worker,stream,prior_samples,entered)
        await feed(stream,32000,start=prior_samples);await entered.wait();await until(lambda:provider.engine.ready)
        record_property('live_timeout',json.dumps(dict(prior_samples=prior_samples,
            received=stream.received_samples,decoded=stream.decoded_samples,pending_samples=len(stream.pending)//2,
            checkpoint_preserved=stream.checkpoint is snapshot,error=getattr(stream.error,'code',None),
            generation=stream.generation,engine_generation=provider.engine.generation,physical_model=False)))
        assert stream.error is None and not stream.cancelled
        assert stream.generation==provider.engine.generation
        assert stream.decoded_samples==prior_samples and stream.received_samples==prior_samples+32000
        assert len(stream.pending)==64000 and stream.checkpoint is snapshot
        assert stream.native_options['context']=='React'
        if snapshot:assert snapshot['samples']==prior_samples
        await feed(stream,123,start=prior_samples+32000)
        result=await stream.finish(prior_samples+32123,loop_time()+3)
        assert result.processed_samples==prior_samples+32123 and result.text==f'A-{prior_samples+32123}'
        assert not stream.pending and stream.checkpoint is None
        if snapshot is not None:assert not snapshot
    finally:await provider.close()


async def test_live_timeout_waits_for_new_audio_or_finish_instead_of_busy_retrying(tmp_path):
    provider,worker=await prepared(tmp_path)
    stream=await provider.open(AsrOpenOptions('quiet',1,user_id='A'))
    try:
        entered=asyncio.Event();calls=[];original=worker.decode
        async def timeout_first(args,deadline):
            if args[1]['action']=='feed':
                calls.append(args[1]['start_sample'])
                if len(calls)==1:entered.set();raise TimeoutError('declared live timeout')
            return await original(args,deadline)
        worker.decode=timeout_first
        await feed(stream,32000);await entered.wait();await until(lambda:provider.engine.ready)
        await asyncio.sleep(.03)
        assert calls==[0] and stream.error is None
        result=await stream.finish(32000,loop_time()+3)
        assert result.text=='A-32000' and result.processed_samples==32000 and calls==[0]
    finally:await provider.close()


async def test_cached_replay_timeout_does_not_gain_a_new_budget_or_live_resume(tmp_path):
    provider,worker=await prepared(tmp_path);deadline=loop_time()+2
    stream=await provider.open(AsrOpenOptions('cached',2,user_id='A',outcome_deadline=deadline,eligibility_deadline=deadline))
    try:
        entered=asyncio.Event();one_timeout(worker,stream,0,entered)
        await feed(stream,32000);await entered.wait();await until(lambda:stream.error is not None)
        assert stream.options.outcome_deadline==deadline and stream.options.eligibility_deadline==deadline
        assert not stream.pending and stream.checkpoint is None
        with pytest.raises(ProviderError):await stream.finish(32000,deadline)
    finally:await provider.close()


async def test_cancel_during_live_recovery_stop_clears_before_join_and_never_resurrects(tmp_path):
    provider,worker=await prepared(tmp_path);entered=asyncio.Event();stopping=asyncio.Event();release=asyncio.Event()
    original_stop=worker.stop;cancel=None
    stream=await provider.open(AsrOpenOptions('cancel',1,user_id='A'))
    try:
        await feed(stream,32000);await until(lambda:stream.decoded_samples==32000)
        checkpoint=stream.checkpoint;checkpoint['arrays']['audio_accum'].extend(b'\0'*128)
        arrays=list(checkpoint['arrays'].values())
        async def stopped():stopping.set();await release.wait();await original_stop()
        worker.stop=stopped;one_timeout(worker,stream,32000,entered)
        await feed(stream,32000,start=32000);await entered.wait();await stopping.wait()
        cancel=asyncio.create_task(stream.cancel());await until(lambda:stream.cancelled)
        assert not stream.pending and stream.checkpoint is None and not checkpoint and all(not a for a in arrays)
        assert not cancel.done()
        release.set();await cancel;await until(lambda:provider.engine.ready)
        assert stream.owner not in provider.engine.sessions
        with pytest.raises(ProviderError):await stream.feed(b'\0\0',64000)
        healthy=await provider.open(AsrOpenOptions('next',1,user_id='B'))
        await feed(healthy,123);assert (await healthy.finish(123,loop_time()+3)).text=='B-123'
    finally:
        release.set()
        if cancel:await asyncio.gather(cancel,return_exceptions=True)
        worker.stop=original_stop;await provider.close()


async def test_stop_before_live_timeout_uses_authoritative_finish_without_a_new_live_call(tmp_path):
    provider,worker=await prepared(tmp_path);entered=asyncio.Event();expired=asyncio.Event()
    original=worker.decode;finish=None;actions=[]
    stream=await provider.open(AsrOpenOptions('stop-overlap',1,user_id='A',hint_terms=['React']))
    async def controlled(args,deadline):
        action=args[1]['action']
        if action in ('feed','finish'):actions.append(action)
        if action=='feed':
            entered.set();await expired.wait();raise TimeoutError('declared live deadline while stop is pending')
        return await original(args,deadline)
    try:
        worker.decode=controlled
        await feed(stream,32000);await entered.wait()
        deadline=loop_time()+3
        finish=asyncio.create_task(stream.finish(32000,deadline));await until(lambda:stream.finishing)
        expired.set();result=await finish
        assert result.text=='A-32000' and result.processed_samples==32000
        assert actions==['feed','finish'] and loop_time()<deadline
        assert not stream.pending and stream.checkpoint is None and not provider.engine.sessions
    finally:
        expired.set()
        if finish:await asyncio.gather(finish,return_exceptions=True)
        worker.decode=original;await provider.close()
