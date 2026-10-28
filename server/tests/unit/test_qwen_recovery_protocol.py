"""Recovery/cleanup controls; physical worker is explicitly fictional except PID."""
import asyncio
import json
import os
from pathlib import Path
import struct
import time

import numpy as np
import pytest

from app.core.clock import loop_time
from app.core.config import AsrConfig
from app.providers.asr.base import AsrOpenOptions, ProviderError
from app.providers.asr.qwen3_mlx import Qwen3MlxProvider
from app.providers.asr.qwen_checkpoint import clear, restore
from app.services.asr_core import DecodeWorker
from tests.qwen_native_fixtures import native_checkpoint, native_state
from tests.unit.test_qwen_native_provider import feed, manifest, prepared, until


@pytest.mark.parametrize('fault', ['owner', 'samples', 'extra', 'window', 'byte_type', 'context', 'body'])
async def test_invalid_acknowledged_checkpoint_fails_only_its_owner(tmp_path, fault):
    provider, worker = await prepared(tmp_path)
    a = await provider.open(AsrOpenOptions('a', 1, user_id='A', hint_terms=['React']))
    b = await provider.open(AsrOpenOptions('b', 1, user_id='B', hint_terms=['Docker']))
    original = worker.decode
    try:
        await feed(a, 32000); await feed(b, 32000)
        await until(lambda: a.decoded_samples == b.decoded_samples == 32000)
        await anext(a.partials())
        old = a.checkpoint
        old['arrays']['audio_accum'].extend(b'\0' * 128)
        old_arrays = list(old['arrays'].values())
        healthy = b.checkpoint

        async def corrupt(args, deadline):
            result = await original(args, deadline)
            if args[1].get('owner') == a.owner and args[1]['action'] == 'feed':
                cp = result['checkpoint']
                if fault == 'owner': cp['owner'] = b.owner
                elif fault == 'samples': cp['samples'] += 1
                elif fault == 'extra': cp['fields']['not_a_native_field'] = 'forbidden'
                elif fault == 'window': cp['arrays']['audio_accum'] = bytearray(480001 * 4)
                elif fault == 'byte_type': cp['arrays']['buffer'] = bytes(4)
                elif fault == 'context': cp['fields']['context'] = 'ForeignTerm'
                elif fault == 'body': cp['fields']['text'] = 'foreign body'
            return result

        worker.decode = corrupt
        await feed(a, 32000, start=32000)
        with pytest.raises(ProviderError) as failed:
            async with asyncio.timeout(3):
                await anext(a.partials())
        assert failed.value.code == 'invalid_output'
        assert a.error is failed.value and not a.pending and a.checkpoint is None
        assert not old and all(not data for data in old_arrays)
        assert b.error is None and b.checkpoint is healthy and b.decoded_samples == 32000
        worker.decode = original
        await until(lambda: provider.engine.ready)
        result = await b.finish(32000, loop_time()+3)
        assert result.text == 'B-32000' and result.processed_samples == 32000
        assert not healthy and b.checkpoint is None
    finally:
        worker.decode = original
        await provider.close()


def test_restore_rejects_nonfinite_acoustic_state():
    owner = ('A', 'a', 1)
    checkpoint = native_checkpoint(owner, 32000, 'A-32000', 'A')
    checkpoint['arrays']['audio_accum'] = bytearray(struct.pack('<f', float('nan')))
    class Model:
        def init_streaming(self, **options):
            return native_state(options['context'], options['language'])
    with pytest.raises(ValueError, match='acoustic values'):
        restore(checkpoint, owner, 32000, Model(), np)
    clear(checkpoint)
    assert not checkpoint


async def test_replaced_finished_cancelled_and_closed_checkpoints_clear_all_aliases(tmp_path):
    provider, _ = await prepared(tmp_path)
    try:
        a = await provider.open(AsrOpenOptions('a', 1, user_id='A'))
        await feed(a, 32000); await until(lambda: a.decoded_samples == 32000)
        previous = a.checkpoint; previous['arrays']['audio_accum'].extend(b'\0' * 128)
        old_arrays = list(previous['arrays'].values())
        await feed(a, 32000, start=32000); await until(lambda: a.decoded_samples == 64000)
        assert not previous and all(not value for value in old_arrays)
        final_checkpoint = a.checkpoint
        final_checkpoint['arrays']['audio_accum'].extend(b'\0' * 128)
        final_arrays = list(final_checkpoint['arrays'].values())
        assert (await a.finish(64000, loop_time()+3)).processed_samples == 64000
        assert a.checkpoint is None and not final_checkpoint and all(not value for value in final_arrays)
        for action, user in [('cancel', 'B'), ('close', 'C')]:
            stream = await provider.open(AsrOpenOptions(action, 1, user_id=user))
            await feed(stream, 32000); await until(lambda: stream.decoded_samples == 32000)
            snapshot = stream.checkpoint; snapshot['arrays']['audio_accum'].extend(b'\0' * 128)
            arrays = list(snapshot['arrays'].values())
            if action == 'cancel': await stream.cancel()
            else: await provider.close()
            assert not snapshot and stream.checkpoint is None and all(not value for value in arrays)
    finally:
        await provider.close()


@pytest.mark.parametrize('finish_action', ['original_deadline', 'cancel_waiter'])
async def test_recovery_wait_never_resets_deadline_or_cancels_shared_prepare(tmp_path, finish_action):
    provider, worker = await prepared(tmp_path)
    ready = asyncio.Event(); entered = asyncio.Event()
    original = worker.decode
    async def gated_prepare(args, deadline):
        if args[1]['action'] == 'prepare':
            entered.set(); await ready.wait()
        return await original(args, deadline)
    try:
        a = await provider.open(AsrOpenOptions('a', 1, user_id='A'))
        b = await provider.open(AsrOpenOptions('b', 1, user_id='B'))
        await feed(b, 32000); await until(lambda: b.decoded_samples == 32000)
        worker.decode = gated_prepare; worker.block = asyncio.Event(); worker.entered.clear()
        await feed(a, 32000); await worker.entered.wait(); await a.cancel(); await entered.wait()
        repair = provider.engine.repair
        if finish_action == 'original_deadline':
            started = loop_time()
            with pytest.raises(ProviderError) as failed:
                await b.finish(32000, started+.05)
            assert failed.value.code == 'timeout' and loop_time()-started < .5
        else:
            task = asyncio.create_task(b.finish(32000, loop_time()+3))
            await until(lambda: b.owner in provider.scheduler.running)
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
            await b.cancel()
        assert not repair.done() and not repair.cancelled()
        assert not b.pending and b.checkpoint is None
        worker.block = None; ready.set(); await until(lambda: provider.engine.ready)
        c = await provider.open(AsrOpenOptions('c', 1, user_id='C'))
        await feed(c, 123)
        assert (await c.finish(123, loop_time()+3)).text == 'C-123'
    finally:
        worker.block = None; ready.set(); await provider.close()


def owned_control_worker(connection):
    """Actual child process, fictional sample-count inference, no MLX/model."""
    states = {}
    try:
        while True:
            message = connection.recv()
            if message is None: return
            audio, command = message
            action = command['action']; owner = command.get('owner')
            if action == 'prepare':
                connection.send((True, dict(ready=True, runtime='fictional-child', mlx='none', pid=os.getpid())))
                continue
            if action == 'drop':
                states.pop(owner, None); connection.send((True, dict(dropped=True))); continue
            snapshot = command.get('checkpoint')
            if snapshot is not None:
                assert owner not in states and snapshot['owner'] == owner
                assert snapshot['samples'] == command['start_sample']
                states[owner] = snapshot['samples']
            assert states.get(owner, 0) == command['start_sample']
            marker = command.get('fictional_marker')
            if marker and command['start_sample'] == 32000 and not Path(marker).exists():
                with Path(marker).open('x') as f:
                    json.dump(dict(pid=os.getpid(), start_sample=32000, owner=owner), f)
                time.sleep(60)  # Cancel must kill/reap this exact owned process.
            count = states.get(owner, 0) + len(audio)//2
            states[owner] = count; text = owner[0]+'-'+str(count)
            result = dict(text=text, stable=owner[0], samples=count)
            if action == 'feed':
                result['checkpoint'] = native_checkpoint(owner, count, text, owner[0], command['options']['context'])
            else: states.pop(owner, None)
            connection.send((True, result))
    finally:
        states.clear(); connection.close()


class ObservedOwnedWorker(DecodeWorker):
    def __init__(self, marker, held_user):
        super().__init__(target=owned_control_worker)
        self.marker, self.held_user = marker, held_user
        self.observations = []
    def _start(self):
        super()._start()
        self.observations.append(dict(event='start', pid=self.process.pid, at=loop_time()))
    async def decode(self, args, deadline):
        audio, command = args
        if command['action'] == 'feed' and command['owner'][0] == self.held_user:
            command = dict(command, fictional_marker=str(self.marker))
        if command.get('checkpoint') is not None:
            self.observations.append(dict(event='restore-dispatch', owner=command['owner'],
                samples=command['start_sample'], at=loop_time()))
        return await super().decode((audio, command), deadline)
    async def stop(self):
        process = self.process; pid = process.pid if process is not None else None
        await super().stop()
        if pid is not None:
            with pytest.raises(ProcessLookupError): os.kill(pid, 0)
            self.observations.append(dict(event='confirmed-exit', pid=pid, at=loop_time()))


@pytest.mark.parametrize('position', ['running_owner', 'idle_behind_other_running'])
async def test_real_owned_pid_exits_before_restart_and_healthy_owner_keeps_samples(tmp_path, record_property, position):
    model, identity = manifest(tmp_path)
    marker = tmp_path/'actual-child-entered.json'
    worker = ObservedOwnedWorker(marker, 'A' if position == 'running_owner' else 'B')
    provider = Qwen3MlxProvider(AsrConfig(provider='qwen3_mlx', model_path=str(model),
        model_manifest_path=str(identity)), worker=worker)
    await provider.prepare()
    try:
        a = await provider.open(AsrOpenOptions('a', 1, user_id='A', hint_terms=['React']))
        b = await provider.open(AsrOpenOptions('b', 1, user_id='B', hint_terms=['Docker']))
        await feed(a, 32000); await feed(b, 32000)
        await until(lambda: a.decoded_samples == b.decoded_samples == 32000)
        held = a if position == 'running_owner' else b
        await feed(held, 32000, start=32000)
        async with asyncio.timeout(3):
            while not marker.exists(): await asyncio.sleep(.005)
        old_pid = json.loads(marker.read_text())['pid']
        assert old_pid == worker.process.pid
        await feed(b, 123, start=b.received_samples)
        own_snapshot = a.checkpoint; own_arrays = list(own_snapshot['arrays'].values())
        healthy_snapshot = b.checkpoint
        await a.cancel()
        assert a.checkpoint is None and not own_snapshot and all(not data for data in own_arrays)
        assert b.error is None and b.checkpoint is healthy_snapshot
        result = await b.finish(b.received_samples, loop_time()+3)
        expected = 32123 if position == 'running_owner' else 64123
        assert result.processed_samples == expected and result.text == f'B-{expected}'
        assert not healthy_snapshot and not provider.engine.sessions and not provider.engine._audio_inputs
        starts = [row for row in worker.observations if row['event'] == 'start']
        exits = [row for row in worker.observations if row['event'] == 'confirmed-exit' and row['pid'] == old_pid]
        assert len(starts) == 2 and starts[0]['pid'] == old_pid and starts[1]['pid'] != old_pid
        assert exits and exits[0]['at'] <= starts[1]['at']
        assert any(row['event'] == 'restore-dispatch' and row['owner'] == b.owner and row['samples'] == 32000
                   for row in worker.observations)
        record_property('owned_pid_recovery', json.dumps(dict(position=position,
            events=worker.observations, healthy_samples=expected, physical_model=False, actual_os_process=True)))
    finally:
        await provider.close()
