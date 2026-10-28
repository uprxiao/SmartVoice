"""Natural600 + real owned OS worker; synthetic transcribe, not MLX/native.

No manual expire, DB aging, clock patch, altered 10/30/600 or outcome reserve.
D-4 API retry; D-1.05 two-second observer queue; observe naturally through D+2.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import signal
from types import SimpleNamespace
import uuid

import httpx
import pytest
from sqlalchemy import text

from tests.conftest import auth_headers, register_and_login
from tests.integration.test_asr_real_stop_second_cancel import (
    alive, child_events, read_ledger, real_process_client, until,
)
from tests.integration.test_capacity_retry_cache_boundaries import read_session
from tests.integration.test_capacity_retry_cache_confirmed600 import business_facts
from tests.integration.test_capacity_retry_cache_real600 import clock_sample, source_manifest
from tests.integration.test_normal_shutdown_contract import (
    WsProbe, create_session, require_owned_database, safe_status,
)


@pytest.fixture
def natural_owned600(request):
    if os.environ.get('SVI_RUN_REAL_600S') != '1':
        pytest.fail('explicit SVI_RUN_REAL_600S=1 required; this node really waits600')
    parent = os.environ.get('SVI_TTL_EVIDENCE_DIR')
    if not parent:
        pytest.fail('set private SVI_TTL_EVIDENCE_DIR')
    directory = Path(parent) / ('owned-os600-' + uuid.uuid4().hex)
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    client, folder, owner = request.getfixturevalue('real_process_client')
    return client, folder, owner, directory


async def permanent4000(app, user_id, sid):
    # Existing permanent_facts helper fixes 8000 samples; this WS helper sends
    # 4000, so keep its actual domain data and compare the same permanent fields.
    async with app.state.db.session_factory() as db:
        await db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ'))
        args = {'u': user_id, 's': sid}
        contribution = await db.scalar(text(
            "SELECT to_jsonb(t) - 'revision' FROM session_contributions t "
            'WHERE user_id=:u AND session_id=:s'), args)
        daily = list((await db.scalars(text(
            'SELECT to_jsonb(t) FROM usage_daily t WHERE user_id=:u ORDER BY date'), args)).all())
        metadata = dict((await db.execute(text(
            'SELECT id,user_id,device_id,created_login_sid,created_at,first_terminal_at,'
            'first_asr_failed_at,received_samples,audio_complete,dictionary_version '
            'FROM sessions WHERE user_id=:u AND id=:s'), args)).mappings().one())
        assert contribution['counted_session'] and contribution['outcome'] == 'failed'
        assert contribution['received_samples'] == 4000 and daily
        body = json.dumps({'contribution': contribution, 'daily': daily, 'metadata': metadata},
                          sort_keys=True, default=str).encode()
        return {'sha256': hashlib.sha256(body).hexdigest(), 'received_samples': 4000,
                'daily_rows': len(daily), 'counted_session': True}


def test_cap15_natural600_owned_worker_exits_before_execution_permit_reuse(
    natural_owned600, monkeypatch, record_property,
):
    client, folder, owner, directory = natural_owned600
    require_owned_database()
    account = register_and_login(client, email='natural-owned-os600@example.invalid')
    headers, user_id = auth_headers(account), account['user']['id']
    source_before = source_manifest()

    async def run():
        app, loop = client.app, asyncio.get_running_loop()
        manager, engine = app.state.sessions, app.state.asr.engine
        worker = engine.workers[0]
        assert app.state.config.retry.audio_ttl_seconds == 600
        assert app.state.config.asr.decode_timeout_ms == 3000
        assert app.state.config.asr.finish_timeout_ms == 10000
        assert app.state.config.pipeline.stop_to_result_budget_ms == 30000
        assert engine.scheduler.slots == len(engine.workers) == 1
        trace = {'kind': 'natural600_owned_os', 'completed': False, 'source_before': source_before,
            'scope': {'real_pg_api_asgi_ws': True, 'real_owned_os': True,
                      'physical_asr': 'synthetic_transcribe', 'real_mlx': False, 'native': False,
                      'retry_at_599': False, 'manual_expiry': False},
            'states': [], 'issues': [], 'cleanup': {}, 'refine_calls': []}
        processes, buffers, runtimes = {}, {}, []
        wire = monitor_task = probe_task = None
        initial_runtime = runtime = old_process = old_popen = job = None
        sid = expiry = key = None
        stop_monitor = False
        actual_fail, actual_open, actual_refine = manager._fail, app.state.asr.open, manager.refiner.refine
        actual_decode = engine.decode
        decode_deadlines = {}

        async def observed_decode(*args, **kwargs):
            # Capture the real adapter inputs; do not reconstruct B from D.
            if kwargs.get('purpose') == 'asr_finish':
                decode_deadlines[kwargs['owner']] = {
                    'eligibility': kwargs['deadline'], 'outcome': kwargs['outcome_deadline']}
            return await actual_decode(*args, **kwargs)

        def capture():
            process = worker.process
            if process is not None:
                assert process._parent_pid == os.getpid()
                processes.setdefault(process.pid, (process, process._popen))
            for owned in runtimes:
                buffers.setdefault(id(owned.audio), owned.audio)
            if sid is not None:
                current = manager.runtimes.get(sid)
                if current is not None:
                    buffers.setdefault(id(current.audio), current.audio)
            return process

        async def observed_open(options):
            stream = await actual_open(options)
            buffers.setdefault(id(stream.buffer), stream.buffer)
            return stream

        async def observed_fail(owned, *args, **kwargs):
            before = clock_sample()
            try:
                return await actual_fail(owned, *args, **kwargs)
            finally:
                if owned.attempt_no == 1:
                    trace.setdefault('first_failure_window', {'before': before, 'after': clock_sample()})

        async def observed_refine(*args, **kwargs):
            trace['refine_calls'].append(clock_sample())
            return await actual_refine(*args, **kwargs)

        monkeypatch.setattr(app.state.asr, 'open', observed_open)
        monkeypatch.setattr(manager, '_fail', observed_fail)
        monkeypatch.setattr(manager.refiner, 'refine', observed_refine)
        monkeypatch.setattr(engine, 'decode', observed_decode)

        async def monitor():
            previous = None
            last_saved = -1.0
            while not stop_monitor:
                before = clock_sample()
                current = capture()
                lengths = [len(value) for value in buffers.values()]
                current_pid = current.pid if current is not None else None
                old_alive = alive(old_process, old_popen) if old_process is not None else None
                running = engine.scheduler.running.get(key) is job if job is not None else None
                done = job.done.is_set() if job is not None else None
                after = clock_sample()
                row = {'before': before, 'after': after, 'old_alive': old_alive,
                    'old_job_running': running, 'old_job_done': done, 'current_pid': current_pid,
                    'old_exitcode': old_popen.returncode if old_popen is not None else None,
                    'pcm_lengths_bytes': lengths}
                state = (old_alive, running, done, current_pid, tuple(lengths))
                if state != previous or after['monotonic_after'] - last_saved >= 30:
                    trace['states'].append(row)
                    previous, last_saved = state, after['monotonic_after']
                if old_alive and (not running or done):
                    trace.setdefault('premature_permit_release', row)
                if old_alive and current_pid not in (None, old_process.pid):
                    trace.setdefault('new_generation_before_old_exit', row)
                if old_alive is False:
                    trace.setdefault('first_old_exit_observation', row)
                if expiry is not None:
                    if any(lengths):
                        trace['last_positive_pcm'] = row
                        if expiry - 1 <= before['epoch'] < expiry:
                            trace['last_second_positive_count'] = trace.get('last_second_positive_count', 0) + 1
                        if before['epoch'] >= expiry + 1:
                            trace['positive_after_D_plus_1'] = trace.get('positive_after_D_plus_1', 0) + 1
                    else:
                        trace.setdefault('first_all_local_zero', row)
                await asyncio.sleep(.001 if expiry is not None and before['epoch'] >= expiry - 6 else .05)

        async def wait_epoch(target):
            previous = None
            while True:
                if monitor_task.done():
                    monitor_task.result()
                    raise AssertionError('owned monitor ended early')
                observed = clock_sample()
                if observed['epoch'] >= target:
                    return {'threshold_epoch': target, 'previous': previous, 'first_due': observed}
                previous = observed
                await asyncio.sleep(.001 if target - observed['epoch'] < .2 else .02 if target - observed['epoch'] < 5 else .5)

        try:
            capture()
            monitor_task = asyncio.create_task(monitor())
            async with asyncio.timeout(635):  # Test anti-hang only, never a product deadline.
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as http:
                    response = await create_session(http, headers)
                    assert response.status_code == 201, safe_status(response)
                    sid = response.json()['session_id']
                    initial_runtime = manager.runtimes[sid]
                    runtimes.append(initial_runtime)
                    wire = WsProbe(app, headers, sid)
                    await wire.start()
                    await wire.end()
                    wire.drain_task = asyncio.create_task(wire.drain())
                    await wire.drain_task
                    before = await read_session(app, user_id, sid)
                    assert before['state'] == 'failed' and before['retry_cache_status'] == 'available'
                    assert before['received_samples'] == 4000 and len(initial_runtime.audio) == 8000
                    expiry = before['first_asr_failed_at'].timestamp() + 600
                    assert initial_runtime.retry_expires_at == expiry
                    item = SimpleNamespace(sid=sid, runtime=initial_runtime)
                    initial_facts = await permanent4000(app, user_id, sid)
                    trace.update(session_id=sid, expiry_epoch=expiry, before_permanent=initial_facts,
                                 first_failure=before['first_asr_failed_at'].isoformat())

                    trace['retry_due_observation'] = await wait_epoch(expiry - 4)
                    retry_key = str(uuid.uuid4())
                    trace['retry_request_start'] = clock_sample()
                    retried = await http.post(f'/api/v1/sessions/{sid}/retry',
                        headers=headers | {'Idempotency-Key': retry_key}, json={'retry_kind': 'asr'})
                    trace['retry_request_end'] = clock_sample()
                    assert retried.status_code == 202 and retried.json()['current_attempt_no'] == 2
                    assert retried.json()['replayed'] is False
                    assert expiry - 4 <= trace['retry_request_start']['epoch'] < expiry - 1
                    runtime = manager.runtimes[sid]
                    runtimes.append(runtime)
                    assert runtime.retry_expires_at == expiry
                    key = (user_id, sid, 2)
                    await until(lambda: key in engine.scheduler.running, 2)
                    await until(lambda: worker.process is not None and any(
                        row['pid'] == worker.process.pid and row['event'] == 'synthetic_decode_enter'
                        for row in child_events(folder, owner)), 2)
                    old_process = capture()
                    old_popen = old_process._popen
                    job = engine.scheduler.running[key]
                    assert alive(old_process, old_popen) and not job.done.is_set() and job.priority == 'high'
                    frame = job.task.get_coro().cr_frame
                    assert frame is not None and 'actual_deadline' in frame.f_locals
                    physical_deadline = frame.f_locals['actual_deadline']
                    del frame
                    bounds = decode_deadlines[key]
                    assert job.deadline == bounds['eligibility'] == runtime.audio_lease.deadline
                    assert bounds['outcome'] <= runtime.deadline
                    assert physical_deadline == min(bounds['eligibility'], bounds['outcome'] - 1)
                    assert loop.time() < physical_deadline and runtime.deadline == runtime.end_accepted_at + 30
                    trace.update(old_pid=old_process.pid, physical_deadline_loop=physical_deadline,
                        finish_deadline_loop=job.deadline, runtime_total_deadline_loop=runtime.deadline,
                        decode_deadlines=bounds,
                        child_positive_clock=clock_sample())
                    # No direct timeout/expiry/cancel invocation. Reserve stays
                    # inside original B, while audio eligibility remains D.
                    trace['probe_due_observation'] = await wait_epoch(expiry - 1.05)
                    assert alive(old_process, old_popen) and engine.scheduler.running.get(key) is job
                    assert loop.time() < physical_deadline, 'missed the live pre-timeout positive window'
                    probe_owner = ('owned-os600-permit-observer', str(uuid.uuid4()), 1)

                    async def execution_probe(slot):
                        started = clock_sample()
                        state = alive(old_process, old_popen)
                        trace['probe_execution'] = {'before': started, 'after': clock_sample(),
                            'old_alive': state, 'old_exitcode': old_popen.returncode, 'slot': slot}
                        return slot  # No provider, PCM, recorder or model-call row.

                    trace['probe_enqueued'] = clock_sample()
                    probe_deadline = loop.time() + 2
                    trace['probe_deadline_loop'] = probe_deadline
                    probe_task = asyncio.create_task(engine.scheduler.submit(probe_owner, 'high',
                        execution_probe, deadline=probe_deadline, wait_deadline=probe_deadline))
                    await asyncio.sleep(0)
                    assert probe_owner in engine.scheduler.queues['high'], 'probe missed the positive queue window'
                    assert not probe_task.done() and alive(old_process, old_popen)
                    trace['probe_queued_positive'] = clock_sample()
                    trace['observation_end'] = await wait_epoch(expiry + 2)
                    assert probe_task.done(), 'probe did not complete within unchanged two-second budget'
                    assert probe_task.result() == 0
                    trace['after'] = await read_session(app, user_id, sid)
                    trace['after_permanent'] = await permanent4000(app, user_id, sid)
                    trace['facts'] = await business_facts(app, item)
                    trace['ledger'] = await read_ledger(app, sid)
                    denied = await http.post(f'/api/v1/sessions/{sid}/retry',
                        headers=headers | {'Idempotency-Key': str(uuid.uuid4())}, json={'retry_kind': 'asr'})
                    trace['new_key_after_expiry'] = safe_status(denied) | {
                        'reason': denied.json().get('error', {}).get('details', {}).get('reason')}
                    replay = await http.post(f'/api/v1/sessions/{sid}/retry',
                        headers=headers | {'Idempotency-Key': retry_key}, json={'retry_kind': 'asr'})
                    trace['replay'] = {'status': replay.status_code, 'replayed': replay.json().get('replayed'),
                        'attempt': replay.json().get('current_attempt_no'),
                        'facts_unchanged': await business_facts(app, item) == trace['facts'],
                        'permanent_unchanged': await permanent4000(app, user_id, sid) == initial_facts}
                    trace['children'] = child_events(folder, owner)
                    trace['completed'] = True
        finally:
            # Freeze all timing/oracle observations BEFORE cleanup; cleanup can
            # never turn missed natural exit into a successful timing oracle.
            trace['evidence_end_before_cleanup'] = clock_sample()
            stop_monitor = True
            errors = []

            async def clean(label, awaitable, seconds):
                try:
                    async with asyncio.timeout(seconds):
                        return await awaitable
                except BaseException as exc:
                    errors.append({'stage': label, 'type': type(exc).__name__})
                    return None

            if monitor_task is not None:
                await clean('monitor', asyncio.shield(monitor_task), 1)
                if not monitor_task.done():
                    monitor_task.cancel()
                    await clean('monitor_cancel', asyncio.gather(monitor_task, return_exceptions=True), 1)
            try:
                capture()
                trace['children_at_cleanup'] = child_events(folder, owner)
            except BaseException as exc:
                errors.append({'stage': 'capture', 'type': type(exc).__name__})
            owned_tasks = [task for task in (probe_task, getattr(wire, 'task', None),
                getattr(wire, 'drain_task', None)) if task is not None]
            for task in owned_tasks:
                if not task.done():
                    task.cancel()
            if owned_tasks:
                await clean('owned_tasks', asyncio.gather(*owned_tasks, return_exceptions=True), 5)
            await clean('engine_close', engine.close(), 6)
            try:
                capture()
            except BaseException as exc:
                errors.append({'stage': 'capture_after_close', 'type': type(exc).__name__})
            emergencies = []
            for pid, (process, popen) in processes.items():
                try:
                    if alive(process, popen):
                        assert process._parent_pid == os.getpid() and process.pid == pid
                        emergencies.append(pid)
                        process.kill()
                        await clean('emergency_join', asyncio.to_thread(process.join, 2), 3)
                        assert not alive(process, popen)
                except BaseException as exc:
                    errors.append({'stage': 'owned_process_cleanup', 'type': type(exc).__name__})
            trace['cleanup'] = {'errors': errors, 'emergency_owned_pids': emergencies,
                'all_retained_exited': all(not alive(p, popen) for p, popen in processes.values()),
                'scheduler': engine.scheduler.snapshot(),
                'pool_checkedout': app.state.db.engine.sync_engine.pool.checkedout()}
            trace['source_after'] = source_manifest()
            trace['source_unchanged'] = trace['source_before'] == trace['source_after']
            payload = json.dumps(trace, default=str, sort_keys=True, indent=2) + '\n'
            await asyncio.to_thread((directory / 'owned-os600-summary.json').write_text, payload)
            record_property('natural_owned_os600_summary', json.dumps({
                'directory': str(directory), 'completed': trace['completed'],
                'summary_sha256': hashlib.sha256(payload.encode()).hexdigest()}))

        assert trace['completed'] and trace['source_unchanged']
        assert not trace['cleanup']['errors'] and not trace['cleanup']['emergency_owned_pids']
        assert trace['cleanup']['all_retained_exited'] and trace['cleanup']['pool_checkedout'] == 0
        assert trace['cleanup']['scheduler'] == {'running': 0, 'high_waiting': 0, 'low_waiting': 0}
        assert 'premature_permit_release' not in trace and 'new_generation_before_old_exit' not in trace
        assert trace['first_old_exit_observation']['after']['epoch'] <= expiry + 2
        assert trace['first_old_exit_observation']['old_exitcode'] == -signal.SIGKILL
        assert any(row['pid'] == trace['old_pid'] and row['event'] == 'sigterm_ignored' for row in trace['children'])
        assert trace['probe_execution']['old_alive'] is False
        assert trace['probe_execution']['old_exitcode'] == -signal.SIGKILL
        assert trace['probe_execution']['after']['loop_after'] <= trace['probe_deadline_loop']
        assert trace['first_all_local_zero']['after']['epoch'] <= expiry + 1
        assert trace.get('last_second_positive_count', 0) > 0 and trace.get('positive_after_D_plus_1', 0) == 0
        assert trace['observation_end']['first_due']['monotonic_before'] - trace['first_failure_window']['after']['monotonic_after'] >= 600
        after = trace['after']
        assert after['state'] == 'failed' and after['retry_cache_status'] == 'expired'
        assert after['current_attempt_no'] == 2 and after['current_result_id'] is None
        assert after['first_asr_failed_at'] == before['first_asr_failed_at'] and after['received_samples'] == 4000
        assert trace['before_permanent'] == trace['after_permanent']
        assert trace['new_key_after_expiry'] == {'status': 409, 'code': 'RETRY_UNAVAILABLE', 'reason': 'audio_expired'}
        assert trace['replay'] == {'status': 200, 'replayed': True, 'attempt': 2,
                                   'facts_unchanged': True, 'permanent_unchanged': True}
        assert trace['facts']['counts'] == {'results': 0, 'insertions': 0, 'learning_events': 0}
        assert trace['facts']['dictionary_version'] == trace['facts']['term_count'] == 0
        assert all(row['state'] == 'failed' and not row['has_raw'] for row in trace['facts']['attempts'])
        assert trace['refine_calls'] == []
        calls = trace['ledger']['calls']
        assert len(calls) == 2 and {row['attempt_no'] for row in calls} == {1, 2}
        assert all(row['purpose'] == 'asr_finish' and row['status'] in ('failed', 'cancelled', 'unknown')
            and row['ended_at'] is not None and row['aggregated_at'] is not None
            and row['prompt_tokens'] is None and row['completion_tokens'] is None for row in calls)

    client.portal.call(run)
