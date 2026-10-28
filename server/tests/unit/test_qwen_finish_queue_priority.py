"""Native-session queue contract; fictional physical worker, no Qwen weights."""
import asyncio

from app.core.clock import loop_time
from app.core.config import AsrConfig
from app.providers.asr.base import AsrOpenOptions
from app.providers.asr.qwen3_mlx import Qwen3MlxProvider
from tests.unit.test_qwen_native_provider import NativeControl, feed, manifest, until


class HeldWorker(NativeControl):
    def __init__(self):
        super().__init__()
        self.held = asyncio.Event()
        self.release = asyncio.Event()
    async def decode(self, args, deadline):
        _, command = args
        if command.get('owner', (None,))[0] == 'blocker' and command['action'] == 'feed':
            self.held.set()
            async with asyncio.timeout_at(deadline):
                await self.release.wait()
        return await super().decode(args, deadline)


async def setup(tmp_path):
    folder, path = manifest(tmp_path)
    worker = HeldWorker()
    provider = Qwen3MlxProvider(AsrConfig(provider='qwen3_mlx', model_id='fictional-finish-queue',
        model_path=str(folder), model_manifest_path=str(path)), worker=worker)
    await provider.prepare()
    blocker = await provider.open(AsrOpenOptions('block', 1, user_id='blocker'))
    await feed(blocker, 32000)
    await worker.held.wait()
    return provider, worker, blocker


async def test_stopped_whole_chunk_and_short_tail_promote_without_discarding_pcm(tmp_path):
    p, w, blocker = await setup(tmp_path)
    jobs = []
    try:
        a = await p.open(AsrOpenOptions('target', 1, user_id='target'))
        await feed(a, 32000)
        await until(lambda: a.owner in p.scheduler.queues['low'])
        original = p.scheduler.queues['low'][a.owner]
        await feed(a, 123, start=32000)
        job = asyncio.create_task(a.finish(32123, loop_time()+10))
        jobs.append(job)
        await until(lambda: a.finishing and a.task.done() and (
            a.owner in p.scheduler.queues['high'] or a.owner in p.scheduler.queues['low']))
        assert a.owner in p.scheduler.queues['high'], p.scheduler.snapshot()
        assert a.owner not in p.scheduler.queues['low']
        assert original.done.is_set() and original.operation is None
        assert len(a.pending)//2 == 32123 and a.decoded_samples == 0
        assert w.stops == 0 and blocker.error is None
        w.release.set()
        result = await job
        assert result.text == 'target-32123' and result.processed_samples == 32123
        assert [(len(pcm)//2, cmd['action']) for pcm, cmd in w.calls if cmd.get('owner') == a.owner] == [(32123, 'finish')]
        assert not a.pending and a.owner not in w.states
        await until(lambda: blocker.decoded_samples == 32000)
        await blocker.finish(32000, loop_time()+10)
    finally:
        w.release.set()
        await asyncio.gather(*jobs, return_exceptions=True)
        await p.close()


async def test_twenty_native_sessions_finish_live_weighting_fifo_and_exact_samples(tmp_path):
    p, w, blocker = await setup(tmp_path)
    streams, jobs = [], []
    try:
        for index in range(20):
            stream = await p.open(AsrOpenOptions('session-'+str(index), 1,
                user_id='user-'+str(index), hint_terms=['Term'+str(index)]))
            streams.append(stream)
            await feed(stream, 32000)
        await until(lambda: p.scheduler.snapshot() == {'running': 1, 'high_waiting': 0, 'low_waiting': 20})
        for stream in streams[::2]:
            jobs.append(asyncio.create_task(stream.finish(32000, loop_time()+10)))
        await until(lambda: all(s.finishing and s.task.done() for s in streams[::2]) and
            len(p.scheduler.queues['high'])+len(p.scheduler.queues['low']) == 20)
        assert p.scheduler.snapshot() == {'running': 1, 'high_waiting': 10, 'low_waiting': 10}
        high, low = [s.owner for s in streams[::2]], [s.owner for s in streams[1::2]]
        assert list(p.scheduler.queues['high']) == high and list(p.scheduler.queues['low']) == low
        assert all(len(s.pending)//2 == 32000 and s.error is None for s in streams)
        w.release.set()
        finals = await asyncio.gather(*jobs)
        await until(lambda: all(s.decoded_samples == 32000 for s in streams[1::2]))
        dispatched = [(cmd['owner'], cmd['action'], len(pcm)//2)
            for pcm, cmd in w.calls if cmd.get('owner') in high+low]
        assert len(dispatched) == 20
        assert [owner for owner, _, _ in dispatched if owner in high] == high
        assert [owner for owner, _, _ in dispatched if owner in low] == low
        assert [i for i, (owner, _, _) in enumerate(dispatched) if owner in low][:3] == [3, 7, 11]
        assert all(count == 32000 and action == ('finish' if owner in high else 'feed')
            for owner, action, count in dispatched)
        assert [r.text for r in finals] == [s.options.user_id+'-32000' for s in streams[::2]]
        for s in streams[1::2]:
            assert (await s.finish(32000, loop_time()+10)).processed_samples == 32000
        await blocker.finish(32000, loop_time()+10)
        assert all(not s.pending for s in streams) and not w.states and w.stops == 0
        assert p.scheduler.snapshot() == {'running': 0, 'high_waiting': 0, 'low_waiting': 0}
    finally:
        w.release.set()
        await asyncio.gather(*jobs, return_exceptions=True)
        await p.close()
