"""Real spawn/pipe/terminate/join/kill + PG regression candidate; NOT executed.

Run after finish's inside-deadline metadata reserve. No shortened 3/10/30 clock.
Only the spawned transcribe entry is synthetic; no NumPy/MLX/audio quality claim.
Explicit cancel API after actual physical timeout is the controlled second cancel.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import signal
import uuid

import httpx
import pytest
from sqlalchemy import select

from app.models import ModelCall, ModelUsageDaily
from app.providers.asr.mlx_whisper import MlxWhisperProvider
from app.services import asr_core
from tests.conftest import auth_headers, register_and_login
from tests.integration.asr_owned_process_fixture import bootstrap
from tests.integration.test_normal_shutdown_contract import (
    WsProbe, create_session, require_owned_database, safe_status,
)


def child_events(folder, owner):
    events = []
    for path in sorted(folder.glob('child-*.jsonl')):
        # One small append is one syscall. Ignore only a currently incomplete
        # final line; malformed completed lines fail the harness.
        content = path.read_text()
        for line in content.splitlines(keepends=True):
            if not line.endswith('\n'):
                continue
            row = json.loads(line)
            assert row['parent_pid'] == owner['parent_pid'] == os.getpid()
            assert row['nonce'] == owner['nonce']
            assert path.name == f"child-{row['pid']}.jsonl"
            events.append(row)
    return events


def alive(process, popen):
    try:
        return process.is_alive()
    except ValueError:  # Production stop may already have closed this object.
        assert popen.returncode is not None, 'closed process without an exit result'
        return False


@pytest.fixture
def real_process_client(monkeypatch, request, tmp_path):
    if os.name != 'posix':
        pytest.skip('This parameter is the macOS/POSIX SIGTERM/kill boundary')
    folder = tmp_path / 'owned-synthetic-asr'
    folder.mkdir()
    owner = {'parent_pid': os.getpid(), 'nonce': uuid.uuid4().hex}
    (folder / 'owner.json').write_text(json.dumps(owner))
    # Deliberately NOT model weights. Original prepare only hashes these files;
    # the child bootstrap replaces transcribe before any MLX import occurs.
    (folder / 'config.json').write_text('{"synthetic_test_only":true}\n')
    (folder / 'weights.npz').write_bytes(b'SVI synthetic physical-worker fixture; not weights\n')
    monkeypatch.setenv('SVI_OWNED_ASR_FIXTURE_DIR', str(folder))
    monkeypatch.setenv('SVI_ASR_PROVIDER', 'mlx_whisper')
    monkeypatch.setenv('SVI_ASR_MODEL_PATH', str(folder))
    monkeypatch.setenv('SVI_LLM_PROVIDER', 'mock')
    # Only process-target bootstrap is substituted. It immediately invokes the
    # original child _worker_main with only transcribe replaced in that child.
    monkeypatch.setattr(asr_core, '_worker_main', bootstrap)
    client = request.getfixturevalue('app_client')
    assert isinstance(client.app.state.asr, MlxWhisperProvider)
    assert type(client.app.state.asr.engine.workers[0]) is asr_core.DecodeWorker
    return client, folder, owner


async def until(predicate, seconds):
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(.005)


async def read_ledger(app, sid):
    async with app.state.db.session_factory() as db:
        rows = (await db.scalars(select(ModelCall).where(ModelCall.session_id == sid))).all()
        calls = [{name: getattr(row, name) for name in (
            'id', 'user_id', 'session_id', 'attempt_no', 'purpose', 'status',
            'error_code', 'processed_audio_samples', 'prompt_tokens',
            'completion_tokens', 'ended_at', 'aggregated_at')} for row in rows]
        daily = (await db.scalars(select(ModelUsageDaily).where(
            ModelUsageDaily.user_id == rows[0].user_id,
            ModelUsageDaily.purpose == 'asr_finish'))).all() if rows else []
        totals = [{name: getattr(row, name) for name in (
            'calls', 'successes', 'failures', 'unknown_calls', 'processed_audio_samples',
            'prompt_tokens_known', 'completion_tokens_known',
            'prompt_tokens_unknown_calls', 'completion_tokens_unknown_calls')} for row in daily]
        return {'calls': calls, 'daily': totals}


def test_finish_timeout_then_api_cancel_joins_real_owned_process_before_reuse(
    real_process_client, record_property,
):
    client, folder, owner = real_process_client
    require_owned_database()
    actor = register_and_login(client, email='owned-process-stop@example.invalid')
    headers = auth_headers(actor)

    async def run():
        app, loop = client.app, asyncio.get_running_loop()
        engine, manager = app.state.asr.engine, app.state.sessions
        assert engine.recorder is app.state.recorder
        assert type(engine.recorder).__name__ == 'DatabaseCallRecorder'
        assert app.state.config.asr.decode_timeout_ms == 3000
        assert app.state.config.asr.finish_timeout_ms == 10000
        assert app.state.config.pipeline.stop_to_result_budget_ms == 30000
        assert app.state.config.retry.audio_ttl_seconds == 600
        assert engine.scheduler.slots == 1 and len(engine.workers) == 1
        worker = engine.workers[0]
        old_process = worker.process
        assert old_process is not None and old_process.is_alive()
        old_pid, old_popen = old_process.pid, old_process._popen
        assert old_process._parent_pid == os.getpid()
        assert any(row['pid'] == old_pid and row['event'] == 'synthetic_warm_return'
                   for row in child_events(folder, owner))
        retained = {old_pid: (old_process, old_popen)}
        monitor_task = cancel_task = wire = job = None
        stop_monitor = False
        evidence = {'scope': 'real-owned-OS-spawn+pipe+PG+ASGI; synthetic-transcribe-only',
            'parent_pid': owner['parent_pid'], 'nonce': owner['nonce'], 'old_pid': old_pid,
            'config': {'partial_ms': 3000, 'finish_ms': 10000, 'stop_ms': 30000},
            'states': [], 'issues': [], 'cleanup': {}}

        def capture_owned():
            process = worker.process
            if process is not None:
                pid = process.pid
                assert process._parent_pid == os.getpid()
                retained.setdefault(pid, (process, process._popen))
            return process

        async def monitor():
            previous = None
            while not stop_monitor:
                process = capture_owned()
                present_pid = process.pid if process is not None else None
                state = (alive(old_process, old_popen),
                    engine.scheduler.running.get(job.owner) is job, job.done.is_set(), present_pid,
                    tuple(sorted(engine.repairs)))
                if state != previous:
                    evidence['states'].append({'observed_loop': loop.time(),
                        'old_alive': state[0], 'old_job_running': state[1], 'old_job_done': state[2],
                        'current_worker_pid': state[3], 'repair_slots': list(state[4])})
                    previous = state
                if state[0] and (not state[1] or state[2]):
                    evidence.setdefault('permit_released_while_old_alive', loop.time())
                if state[0] and state[3] not in (None, old_pid):
                    evidence.setdefault('repair_pid_while_old_alive', {'pid': state[3], 'observed_loop': loop.time()})
                await asyncio.sleep(.001)

        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as http:
                response = await create_session(http, headers)
                assert response.status_code == 201, safe_status(response)
                sid = response.json()['session_id']
                wire = WsProbe(app, headers, sid)
                await wire.start()
                runtime = manager.runtimes[sid]
                await wire.end()
                wire.drain_task = asyncio.create_task(wire.drain())
                end, finish, total = runtime.end_accepted_at, runtime.end_accepted_at + 10, runtime.deadline
                assert total == end + 30
                await until(lambda: any(row['pid'] == old_pid and row['event'] == 'synthetic_decode_enter'
                    for row in child_events(folder, owner)), 3)
                key = (actor['user']['id'], sid, 1)
                job = engine.scheduler.running[key]
                assert job.priority == 'high' and job.deadline == job.wait_deadline == finish
                # Read-only observation of production execute's actual argument;
                # absence means this probe needs rebase, not guessed timing.
                frame = job.task.get_coro().cr_frame
                assert frame is not None and 'actual_deadline' in frame.f_locals
                physical = frame.f_locals['actual_deadline']
                assert 0 < physical - loop.time() <= 10
                assert .5 <= finish - physical <= 1.01, 'requires reviewed finish reserve; no deadline monkeypatch'
                evidence.update(session_id=sid, original_end=end, original_finish=finish,
                    original_total=total, physical_deadline=physical,
                    before=(await read_ledger(app, sid)))
                assert len(evidence['before']['calls']) == 1
                assert evidence['before']['calls'][0]['status'] == 'running'
                monitor_task = asyncio.create_task(monitor())
                await until(lambda: any(row['pid'] == old_pid and row['event'] == 'sigterm_ignored'
                    for row in child_events(folder, owner)), 12)
                evidence['first_sigterm_observed_loop'] = loop.time()
                assert loop.time() >= physical
                assert alive(old_process, old_popen) and engine.scheduler.running.get(key) is job
                assert not job.done.is_set() and job.task.cancelling() == 0
                assert loop.time() < finish - .2, 'harness missed the second-cancel window'
                # Real HTTP cancel: production task cancellation is the second
                # cancellation after the physical timer, not direct stop mocking.
                cancel_task = asyncio.create_task(http.post(f'/api/v1/sessions/{sid}/cancel', headers=headers))
                async with asyncio.timeout(5):
                    cancelled = await asyncio.shield(cancel_task)
                evidence['cancel_return_loop'] = loop.time()
                evidence['old_alive_at_cancel_return'] = alive(old_process, old_popen)
                assert cancelled.status_code == 200, safe_status(cancelled)
                assert cancelled.json()['state'] == 'cancelled' and cancelled.json()['result'] is None
                evidence['at_cancel_return'] = await read_ledger(app, sid)
                evidence['ledger_read_loop'] = loop.time()
                async with asyncio.timeout(5):
                    await wire.drain_task
                assert wire.events[-1]['type'] == 'cancelled'
                await until(lambda: not alive(old_process, old_popen), 3)
                evidence['old_exit_observed_loop'] = loop.time()
                evidence['old_exitcode'] = old_popen.returncode
                evidence['after_exit'] = await read_ledger(app, sid)
                assert runtime.end_accepted_at == end and runtime.deadline == total
                evidence['children'] = child_events(folder, owner)

                # Collect every oracle before failing, rather than hiding the
                # ledger/repair result behind the first premature-release failure.
                issues = evidence['issues']
                if evidence.get('permit_released_while_old_alive'):
                    issues.append('execution permit returned before exact old PID exited')
                if evidence.get('repair_pid_while_old_alive'):
                    issues.append('new repair PID coexisted with the still-running old PID')
                if evidence['old_alive_at_cancel_return']:
                    issues.append('cancel API returned while exact old decode PID was alive')
                if old_popen.returncode != -signal.SIGKILL:
                    issues.append('SIGTERM-ignoring child did not follow real terminate-grace→kill path')
                rows = evidence['at_cancel_return']['calls']
                if len(rows) != 1 or rows[0]['status'] not in ('failed', 'cancelled', 'unknown') or any(
                    rows[0][name] is None for name in ('ended_at', 'aggregated_at')):
                    issues.append('actual dispatched ledger has no durable terminal outcome')
                if evidence['ledger_read_loop'] > finish:
                    issues.append('durable ledger not readable within original finish10')
                if len(rows) == 1:
                    assert rows[0]['prompt_tokens'] is rows[0]['completion_tokens'] is None
                    assert rows[0]['processed_audio_samples'] == 4000 and rows[0]['attempt_no'] == 1
                daily = evidence['at_cancel_return']['daily']
                if len(daily) != 1 or (daily[0]['calls'], daily[0]['successes'], daily[0]['failures']) != (1, 0, 1):
                    issues.append('real model daily terminal projection is missing or incorrect')
        finally:
            # Cleanup failures stay failures, but one failed join must not skip
            # cleanup of other Process objects or erase the safe evidence.
            cleanup_errors = []

            async def cleanup_await(label, awaitable, seconds):
                try:
                    async with asyncio.timeout(seconds):
                        return await awaitable
                except BaseException as exc:
                    cleanup_errors.append({'stage': label, 'type': type(exc).__name__})
                    return None

            stop_monitor = True
            if monitor_task is not None:
                await cleanup_await('monitor', asyncio.shield(monitor_task), 1)
                if not monitor_task.done():
                    monitor_task.cancel()
            try:
                capture_owned()
                evidence['children'] = child_events(folder, owner)
            except BaseException as exc:
                cleanup_errors.append({'stage': 'capture', 'type': type(exc).__name__})
            owned_tasks = [task for task in (cancel_task, getattr(wire, 'drain_task', None),
                getattr(wire, 'task', None)) if task is not None]
            for task in owned_tasks:
                if not task.done():
                    task.cancel()
            if owned_tasks:
                await cleanup_await('owned_requests', asyncio.gather(*owned_tasks, return_exceptions=True), 5)
            # Only joins tasks bound to this exact fixture worker. No unrelated
            # application task is cancelled, awaited or signalled.
            pending_stop = []
            for task in asyncio.all_tasks():
                coroutine = task.get_coro()
                frame = getattr(coroutine, 'cr_frame', None)
                code = getattr(coroutine, 'cr_code', None)
                if task is not asyncio.current_task() and code is not None and code.co_name == 'stop' and frame is not None and frame.f_locals.get('self') is worker:
                    pending_stop.append(task)
            if pending_stop:
                outcomes = await cleanup_await('stop_owners',
                    asyncio.shield(asyncio.gather(*pending_stop, return_exceptions=True)), 5)
                if outcomes is not None and any(isinstance(value, BaseException) for value in outcomes):
                    cleanup_errors.append({'stage': 'stop_owners', 'type': 'stop task failed'})
            await cleanup_await('engine_close', engine.close(), 6)
            # Emergency fallback is never success evidence. No raw PID kill:
            # use only Process objects retained from this exact engine and
            # validate their parent and PID. The initial process also has the
            # completed child-side nonce/parent handshake above. New processes
            # are created only by this worker's patched spawn bootstrap.
            for pid, (process, popen) in retained.items():
                try:
                    if alive(process, popen):
                        assert process._parent_pid == os.getpid() and process.pid == pid
                        evidence['cleanup'].setdefault('emergency_killed_owned_pids', []).append(pid)
                        cleanup_errors.append({'stage': 'emergency_kill', 'type': 'production cleanup left live child'})
                        process.kill()
                        await asyncio.to_thread(process.join, 2)
                        assert not process.is_alive(), 'test-owned child survived emergency cleanup'
                except BaseException as exc:
                    cleanup_errors.append({'stage': f'owned_process_{pid}', 'type': type(exc).__name__})
            evidence['cleanup']['scheduler'] = engine.scheduler.snapshot()
            evidence['cleanup']['all_retained_exited'] = all(not alive(p, popen) for p, popen in retained.values())
            evidence['cleanup']['pool_checkedout'] = app.state.db.engine.sync_engine.pool.checkedout()
            evidence['cleanup']['errors'] = cleanup_errors
            record_property('asr_real_stop_second_cancel', json.dumps(evidence, default=str))
        assert not evidence['cleanup']['errors'], evidence['cleanup']['errors']
        assert evidence['cleanup']['all_retained_exited']
        assert evidence['cleanup']['pool_checkedout'] == 0
        assert not evidence['issues'], evidence['issues']

    client.portal.call(run)
