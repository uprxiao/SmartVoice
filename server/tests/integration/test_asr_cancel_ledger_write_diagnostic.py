"""Diagnostic only: real PG blocking + real owned OS stop; no model quality claim.

The cancel transaction pauses BEFORE its real commit, with the production User
lock held. No ledger/result/status is fabricated. This creates a controlled
transaction-overlap ordering, not a claim about its natural frequency.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
import json
import logging
import os
import signal

import httpx
from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import inference_context
from tests.conftest import TEST_DB, auth_headers, register_and_login
from tests.integration.test_asr_real_stop_second_cancel import (
    alive, child_events, read_ledger, real_process_client, until,
)
from tests.integration.test_normal_shutdown_contract import (
    WsProbe, create_session, require_owned_database, safe_status,
)


_CANCEL_REQUEST = ContextVar('diagnostic_owned_cancel', default=False)
_FINISHED_CALL = ContextVar('diagnostic_owned_finished', default=False)


def safe_ledger(snapshot):
    """No tokens/headers/body/user identity: only ledger dimensions and counters."""
    keep = ('attempt_no', 'purpose', 'status', 'error_code', 'processed_audio_samples',
            'prompt_tokens', 'completion_tokens', 'ended_at', 'aggregated_at')
    return {'calls': [{name: row[name] for name in keep} for row in snapshot['calls']],
            'daily': snapshot['daily']}


def durable_failure(snapshot):
    rows, daily = snapshot['calls'], snapshot['daily']
    return (len(rows) == 1 and rows[0]['status'] in ('failed', 'cancelled', 'unknown')
        and rows[0]['ended_at'] is not None and rows[0]['aggregated_at'] is not None
        and len(daily) == 1
        and (daily[0]['calls'], daily[0]['successes'], daily[0]['failures']) == (1, 0, 1))


class CancelLedgerProbe:
    """Observe actual methods; only the exact cancel transaction commit is gated."""
    def __init__(self, monkeypatch, app, evidence):
        self.app, self.evidence = app, evidence
        self.sid = self.user_id = self.call_id = self.runtime_task = None
        self.cancel_pid = self.writer_pid = None
        self._writer_query_db = self._writer_query_task = None
        self._writer_engine = app.state.db.engine.sync_engine
        assert make_url(app.state.config.database_url).database == TEST_DB
        assert self._writer_engine.url.database == TEST_DB
        self.commit_entered = asyncio.Event()
        self.release_commit = asyncio.Event()
        self.writer_query_entered = asyncio.Event()
        self.events = evidence['writer_events'] = []
        manager, recorder = app.state.sessions, app.state.recorder
        original_locked = manager._locked_session
        original_stop_processing = manager._stop_processing
        original_scalar = AsyncSession.scalar
        original_finished = recorder.finished
        original_persist = inference_context.persist_outcome

        def note(kind, **fields):
            task = asyncio.current_task()
            self.events.append({'kind': kind, 'loop': asyncio.get_running_loop().time(),
                'task_is_runtime': task is self.runtime_task,
                'task_cancelling': task.cancelling() if task else None, **fields})
        self.note = note

        async def locked(db, user_id, session_id):
            value = await original_locked(db, user_id, session_id)
            if (_CANCEL_REQUEST.get() and user_id == self.user_id and session_id == self.sid):
                assert not self.commit_entered.is_set(), 'only one owned cancel transaction is permitted'
                assert value[0] is not None and value[1] is not None
                original_commit = db.commit

                async def commit():
                    # The real API already owns User/Session here. Flush exactly
                    # the writes commit would flush; the transaction stays open.
                    await db.flush()
                    self.cancel_pid = await original_scalar(db, text('SELECT pg_backend_pid()'))
                    assert await original_scalar(db, text('SELECT current_database()')) == TEST_DB
                    note('cancel_commit_gate_enter', backend_pid=self.cancel_pid)
                    self.commit_entered.set()
                    try:
                        await self.release_commit.wait()
                        note('cancel_real_commit_enter')
                        await original_commit()
                        note('cancel_real_commit_return')
                    except BaseException as exc:
                        note('cancel_commit_exit_error', error_type=type(exc).__name__)
                        raise
                monkeypatch.setattr(db, 'commit', commit)
            return value

        async def scalar(db, statement, *args, **kwargs):
            if not _FINISHED_CALL.get():
                return await original_scalar(db, statement, *args, **kwargs)
            sql = ' '.join(str(statement).lower().split())
            owned_user_query = ('from users' in sql and 'for update' in sql)
            if not owned_user_query:
                return await original_scalar(db, statement, *args, **kwargs)
            original_commit = db.commit

            async def commit():
                note('finished_real_commit_enter')
                try:
                    await original_commit()
                except BaseException as exc:
                    note('finished_real_commit_exit_error', error_type=type(exc).__name__)
                    raise
                note('finished_real_commit_return')
            monkeypatch.setattr(db, 'commit', commit)
            self._writer_query_db, self._writer_query_task = db, asyncio.current_task()
            try:
                try:
                    result = await original_scalar(db, statement, *args, **kwargs)
                except BaseException as exc:
                    note('finished_user_query_exit_error', error_type=type(exc).__name__)
                    raise
                note('finished_user_query_return')
                return result
            finally:
                self._writer_query_db = self._writer_query_task = None

        def writer_dispatch(connection, cursor, statement, parameters, context, executemany):
            # Only this exact original writer Task/AsyncSession's User-lock SQL.
            # No SQL/parameters/connection object is stored in the evidence.
            if (not _FINISHED_CALL.get()
                    or asyncio.current_task() is not self._writer_query_task
                    or self._writer_query_db is None):
                return
            normalized = ' '.join(statement.lower().split())
            if 'from users' not in normalized or 'for update' not in normalized:
                return
            assert connection.engine is self._writer_engine
            assert self._writer_query_db.bind is app.state.db.engine
            assert connection.engine.url.database == TEST_DB
            assert connection.in_transaction() and self._writer_query_db.in_transaction()
            # At before_cursor_execute the original query already owns this
            # physical lease. Read the existing proxy directly: never invoke a
            # connection getter that could revalidate or borrow another lease.
            pooled = connection._dbapi_connection
            assert pooled is not None
            driver = pooled.driver_connection
            assert driver is not None and not driver.is_closed()
            pid = driver.get_server_pid()  # asyncpg cached protocol scalar; no I/O.
            assert type(pid) is int and pid > 0
            self.writer_pid = pid
            note('finished_user_query_enter', backend_pid=pid,
                 pid_source='actual_before_cursor_driver_cache',
                 owned_database_config_match=True)
            self.writer_query_entered.set()

        async def finished(*args, **kwargs):
            call_id = kwargs.get('call_id', args[0] if args else None)
            if call_id != self.call_id or self.call_id is None:
                return await original_finished(*args, **kwargs)
            token = _FINISHED_CALL.set(True)
            note('finished_enter', outcome_status=kwargs.get('status'),
                 outcome_error=kwargs.get('error_code'))
            try:
                result = await original_finished(*args, **kwargs)
            except BaseException as exc:
                note('finished_exit_error', error_type=type(exc).__name__)
                raise
            else:
                note('finished_return')
                return result
            finally:
                _FINISHED_CALL.reset(token)

        async def persist(recorder_arg, outcome, deadline):
            if outcome.get('call_id') != self.call_id or self.call_id is None:
                return await original_persist(recorder_arg, outcome, deadline)
            note('persist_enter', deadline=deadline, outcome_status=outcome.get('status'),
                 outcome_error=outcome.get('error_code'), recorder_is_original=recorder_arg is recorder)
            try:
                result = await original_persist(recorder_arg, outcome, deadline)
            except BaseException as exc:
                note('persist_exit_error', error_type=type(exc).__name__)
                raise
            note('persist_return', recorded=result)
            return result

        async def stop_processing(runtime):
            if runtime.session_id != self.sid:
                return await original_stop_processing(runtime)
            note('stop_processing_enter', caller_is_cancel_request=_CANCEL_REQUEST.get(),
                 runtime_task_cancelling=runtime.task.cancelling() if runtime.task else None)
            try:
                return await original_stop_processing(runtime)
            finally:
                note('stop_processing_exit', caller_is_cancel_request=_CANCEL_REQUEST.get(),
                     runtime_task_done=runtime.task.done() if runtime.task else None)

        monkeypatch.setattr(manager, '_locked_session', locked)
        monkeypatch.setattr(manager, '_stop_processing', stop_processing)
        monkeypatch.setattr(AsyncSession, 'scalar', scalar)
        monkeypatch.setattr(recorder, 'finished', finished)
        monkeypatch.setattr(inference_context, 'persist_outcome', persist)
        self.logger = logging.getLogger(inference_context.__name__)
        probe = self

        class OutcomeWarningCount(logging.Handler):
            def emit(self, record):
                if str(record.msg).startswith('MODEL_CALL_OUTCOME_UNCONFIRMED'):
                    # Deliberately do not retain formatted messages/call IDs.
                    probe.evidence['unconfirmed_warning_count'] += 1
        self.handler = OutcomeWarningCount()
        evidence['unconfirmed_warning_count'] = 0
        self.logger.addHandler(self.handler)
        self._writer_dispatch = writer_dispatch
        event.listen(self._writer_engine, 'before_cursor_execute', self._writer_dispatch)

    async def cancel(self, http, headers):
        token = _CANCEL_REQUEST.set(True)
        try:
            return await http.post(f'/api/v1/sessions/{self.sid}/cancel', headers=headers)
        finally:
            _CANCEL_REQUEST.reset(token)

    async def establish_real_block(self, deadline):
        """Observe actual PG lock dependency; do not replace a query/result."""
        async with asyncio.timeout_at(deadline):
            await self.commit_entered.wait()
            await self.writer_query_entered.wait()
            assert self.writer_pid != self.cancel_pid
            while True:
                async with self.app.state.db.session_factory() as db:
                    assert await db.scalar(text('SELECT current_database()')) == TEST_DB
                    blockers = await db.scalar(text('SELECT pg_blocking_pids(:pid)'), {'pid': self.writer_pid})
                if self.cancel_pid in blockers:
                    self.note('pg_blocker_confirmed', waiter_pid=self.writer_pid, blocker_pid=self.cancel_pid)
                    return
                await asyncio.sleep(.005)

    def release(self):
        self.release_commit.set()

    def detach_log(self):
        event.remove(self._writer_engine, 'before_cursor_execute', self._writer_dispatch)
        self.logger.removeHandler(self.handler)


async def read_until_original_deadline(app, sid, deadline, evidence):
    """Successful fresh PG read must finish before deadline; later read is diagnostic."""
    loop = asyncio.get_running_loop()
    polls = evidence['deadline_polls'] = []
    terminal = None
    while loop.time() < deadline:
        try:
            async with asyncio.timeout_at(deadline):
                value = await read_ledger(app, sid)
        except TimeoutError:
            evidence['deadline_poll_timeout'] = True
            break
        observed = loop.time()
        polls.append({'read_complete_loop': observed, 'snapshot': safe_ledger(value)})
        if observed <= deadline and durable_failure(value):
            terminal = value
            evidence['first_durable_read_loop'] = observed
            break
        remaining = deadline - loop.time()
        if remaining > 0:
            await asyncio.sleep(min(.01, remaining))
    # Not a completion extension: this read cannot satisfy the deadline oracle.
    if terminal is None:
        async with asyncio.timeout(1):
            evidence['after_deadline'] = safe_ledger(await read_ledger(app, sid))
            evidence['after_deadline_read_loop'] = loop.time()
    return terminal

def test_cancel_commit_after_physical_stop_preserves_ledger_within_original_finish(
    real_process_client, monkeypatch, record_property,
):
    client, folder, owner = real_process_client
    require_owned_database()
    actor = register_and_login(client, email='owned-ledger-stop@example.invalid')
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
        monitor_task = cancel_task = deadline_read_task = wire = job = None
        stop_monitor = False
        evidence = {'scope': 'controlled-cancel-commit-overlap; real-owned-OS+PG+ASGI; synthetic-transcribe-only',
            'parent_pid': owner['parent_pid'], 'nonce': owner['nonce'], 'old_pid': old_pid,
            'config': {'partial_ms': 3000, 'finish_ms': 10000, 'stop_ms': 30000},
            'states': [], 'issues': [], 'cleanup': {}}
        probe = CancelLedgerProbe(monkeypatch, app, evidence)

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
                probe.sid, probe.user_id = sid, actor['user']['id']
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
                    before=safe_ledger(await read_ledger(app, sid)))
                before = await read_ledger(app, sid)
                assert len(before['calls']) == 1 and before['calls'][0]['status'] == 'running'
                probe.call_id = before['calls'][0]['id']
                probe.runtime_task = runtime.task
                monitor_task = asyncio.create_task(monitor())
                # Start the real request just before the physical timeout, but
                # hold only its real commit. Actual runtime cancellation remains
                # AFTER stop and the proven blocked writer, not a direct cancel.
                await asyncio.sleep(max(0, physical - .5 - loop.time()))
                cancel_task = asyncio.create_task(probe.cancel(http, headers))
                async with asyncio.timeout_at(physical):
                    await probe.commit_entered.wait()
                evidence['cancel_user_lock_held_before_physical'] = loop.time() < physical
                assert evidence['cancel_user_lock_held_before_physical']
                await until(lambda: any(row['pid'] == old_pid and row['event'] == 'sigterm_ignored'
                    for row in child_events(folder, owner)), 12)
                evidence['first_sigterm_observed_loop'] = loop.time()
                assert loop.time() >= physical
                assert alive(old_process, old_popen) and engine.scheduler.running.get(key) is job
                assert not job.done.is_set() and job.task.cancelling() == 0
                assert loop.time() < finish - .2, 'harness missed the second-cancel window'
                # Query PG to prove recorder.finished is waiting on THIS API's
                # production User lock, with actual stop already complete.
                # Missing this bounded diagnostic window fails the harness; no
                # budget/config/physical deadline is moved to obtain a result.
                await probe.establish_real_block(finish - .15)
                assert not alive(old_process, old_popen), 'writer gate must follow real process exit'
                assert job.done.is_set(), 'no permit is held during outcome persistence'
                assert loop.time() < finish - .15
                evidence['commit_gate_release_loop'] = loop.time()
                probe.release()
                # The deadline oracle must not wait behind the optional
                # cancel-return diagnostic read or the HTTP response tail.
                deadline_read_task = asyncio.create_task(
                    read_until_original_deadline(app, sid, finish, evidence))
                async with asyncio.timeout_at(finish + 1):
                    cancelled = await asyncio.shield(cancel_task)
                evidence['cancel_return_loop'] = loop.time()
                evidence['old_alive_at_cancel_return'] = alive(old_process, old_popen)
                assert cancelled.status_code == 200, safe_status(cancelled)
                assert cancelled.json()['state'] == 'cancelled' and cancelled.json()['result'] is None
                # Immediate visibility is diagnostic, not substituted for the
                # separate unchanged original-finish deadline oracle below.
                evidence['at_cancel_return'] = safe_ledger(await read_ledger(app, sid))
                evidence['ledger_read_loop'] = loop.time()
                terminal = await deadline_read_task
                async with asyncio.timeout(5):
                    await wire.drain_task
                assert wire.events[-1]['type'] == 'cancelled'
                await until(lambda: not alive(old_process, old_popen), 3)
                evidence['old_exit_observed_loop'] = loop.time()
                evidence['old_exitcode'] = old_popen.returncode
                evidence['after_exit'] = safe_ledger(await read_ledger(app, sid))
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
                if terminal is None:
                    issues.append('actual dispatched ledger+daily not durably readable within original finish10')
                else:
                    row = terminal['calls'][0]
                    assert row['prompt_tokens'] is row['completion_tokens'] is None
                    assert row['processed_audio_samples'] == 4000 and row['attempt_no'] == 1
                    assert (row['status'], row['error_code']) == ('failed', 'ASR_TIMEOUT')
                    assert evidence['first_durable_read_loop'] <= finish
                    assert terminal['daily'][0]['processed_audio_samples'] == 4000
                    assert terminal['daily'][0]['prompt_tokens_known'] == terminal['daily'][0]['completion_tokens_known'] == 0
                    assert terminal['daily'][0]['prompt_tokens_unknown_calls'] == terminal['daily'][0]['completion_tokens_unknown_calls'] == 1
                assert any(event['kind'] == 'pg_blocker_confirmed' for event in probe.events)
                assert any(event['kind'] == 'persist_enter' and event['deadline'] == finish
                    and event['recorder_is_original'] for event in probe.events)
        finally:
            # Cleanup failures stay failures, but one failed join must not skip
            # cleanup of other Process objects or erase the safe evidence.
            cleanup_errors = []
            probe.release()  # A failed diagnostic must never strand its API transaction.

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
            owned_tasks = [task for task in (deadline_read_task, cancel_task, getattr(wire, 'drain_task', None),
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
            probe.detach_log()
            record_property('asr_cancel_ledger_write_diagnostic', json.dumps(evidence, default=str))
        assert not evidence['cleanup']['errors'], evidence['cleanup']['errors']
        assert evidence['cleanup']['all_retained_exited']
        assert evidence['cleanup']['pool_checkedout'] == 0
        assert not evidence['issues'], evidence['issues']

    client.portal.call(run)
