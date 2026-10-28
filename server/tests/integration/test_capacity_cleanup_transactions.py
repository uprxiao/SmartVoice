"""CAP24 missing local PG controls, not migration/model/native substitutes.

Uses conftest's CREATE-owned database and actual API/WS/production transactions.
The sole synthetic state change ages an API-created terminal row for retention.
Only physical Mock boundaries and one post-User-lock cleanup gate are controlled.
No pool size, authorization/DB/model deadline or retention policy is weakened.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from datetime import timedelta
import json
import time

import asyncpg
import httpx
import pytest
from sqlalchemy import event, select, text, update

from app.models import CapacityChange, RuntimeCapacity, SessionAttempt, SessionContribution, SessionResult, UsageDaily, User, VoiceSession
from app.providers.asr.mock import MockAsrSession
from app.providers.llm.mock import MockLlmProvider
from app.services import cleanup
from tests.conftest import PG_HOST, PG_PASSWORD, PG_PORT, PG_USER, TEST_DB, auth_headers, register_and_login
from tests.integration.test_normal_shutdown_contract import WsProbe, create_session, require_owned_database
from tests.test_admin import login_admin
from tests.test_history_stats import run_session


async def control_connection():
    require_owned_database()
    conn = await asyncpg.connect(host=PG_HOST, port=PG_PORT, user=PG_USER,
                               password=PG_PASSWORD, database=TEST_DB)
    assert await conn.fetchval('SELECT current_database()') == TEST_DB
    return conn


async def permanent(app, owner, sid):
    async with app.state.db.session_factory() as db:
        result = {}
        for model in (SessionContribution, UsageDaily):
            query = select(model).where(model.user_id == owner)
            if model is SessionContribution:
                query = query.where(model.session_id == sid)
            rows = (await db.scalars(query.order_by(*model.__table__.primary_key.columns))).all()
            result[model.__tablename__] = [tuple(getattr(row, col.name) for col in model.__table__.columns) for row in rows]
        return result


def test_cap24_remaining_capacity_checks_reject_and_rollback(app_client, record_property):
    """Complement archived NFR07's singleton/PK/version unique/version step probes."""
    admin = auth_headers(login_admin(app_client))
    view = app_client.get('/api/admin/capacity', headers=admin)
    assert view.status_code == 200
    before_config = view.json()['config']
    changed = app_client.patch('/api/admin/capacity', headers=admin,
        json={'version': before_config['version'], 'max_active_sessions': before_config['max_active_sessions'] - 1})
    assert changed.status_code == 200

    async def run():
        control = await control_connection()
        try:
            async def rows():
                return {table: [dict(row) for row in await control.fetch(f'SELECT * FROM {table} ORDER BY id')]
                        for table in ('runtime_capacity', 'capacity_changes')}
            baseline = await rows()
            assert len(baseline['runtime_capacity']) == len(baseline['capacity_changes']) == 1
            observed = []
            probes = [
                ('runtime_positive', 'UPDATE runtime_capacity SET max_active_sessions=0 WHERE id=1', '23514', 'ck_capacity_positive'),
                ('audit_old_positive', 'UPDATE capacity_changes SET old_max_active=0', '23514', 'ck_capacity_change_old_positive'),
                ('audit_new_positive', 'UPDATE capacity_changes SET new_max_active=0', '23514', 'ck_capacity_change_new_positive'),
                ('audit_actual_change', 'UPDATE capacity_changes SET new_max_active=old_max_active', '23514', 'ck_capacity_change_actual'),
                ('audit_actor_fk', "UPDATE capacity_changes SET actor_user_id='00000000-0000-0000-0000-000000000000'", '23503', 'fk_capacity_changes_actor'),
            ]
            for label, sql, code, constraint in probes:
                tx = control.transaction()
                await tx.start()
                try:
                    with pytest.raises(asyncpg.PostgresError) as caught:
                        await control.execute(sql)
                    assert caught.value.sqlstate == code
                    assert caught.value.constraint_name == constraint
                    observed.append({'probe': label, 'sqlstate': code, 'constraint': constraint})
                finally:
                    await tx.rollback()
                assert await rows() == baseline
            record_property('cap24_remaining_constraints', json.dumps(observed, sort_keys=True))
        finally:
            await control.close()
    app_client.portal.call(run)


def test_cap24_cleanup_create_and_admin_capacity_lock_order(app_client, monkeypatch, record_property):
    tokens = register_and_login(app_client, email='cap24-cleanup@example.invalid')
    headers = auth_headers(tokens)
    admin = auth_headers(login_admin(app_client))
    old_session, _ = run_session(app_client, headers, 'cap24-expired-parent')
    sid = old_session['session_id']
    app = app_client.app

    async def run():
        require_owned_database()
        assert app.state.config.asr.provider == app.state.config.llm.provider == 'mock'
        owner = None
        async with app.state.db.session_factory() as db:
            session = await db.get(VoiceSession, sid)
            assert session.state == 'ready' and session.first_terminal_at is not None
            owner = session.user_id
        before = await permanent(app, owner, sid)
        entered, release, capacity_held, admin_entered = (asyncio.Event() for _ in range(4))
        pids, cleared_counts = {}, []
        original_cleanup = cleanup._cleanup_user
        original_cleanup_user = cleanup.cleanup_user
        original_lock = app.state.capacity.lock
        claimed = False
        create_task = admin_task = clean_task = None
        control = await control_connection()
        new_sid = None
        cleanup_context = ContextVar('cap24_same_owner_cleanup', default=None)
        cleanup_waiters = {}
        verified_graph = {}

        async def observed_cleanup_user(app_arg, user_id):
            task = asyncio.current_task()
            token = cleanup_context.set((user_id, task))
            try:
                return await original_cleanup_user(app_arg, user_id)
            finally:
                for pid, info in list(cleanup_waiters.items()):
                    if info['task'] is task:
                        del cleanup_waiters[pid]
                cleanup_context.reset(token)

        def observe_user_lock(conn, cursor, statement, parameters, context, executemany, *, waiting):
            tracked = cleanup_context.get()
            if tracked is None or tracked[0] != owner:
                return
            sql_shape = ' '.join(statement.split()).upper()
            if not sql_shape.startswith('SELECT USERS.') or ' FOR UPDATE' not in sql_shape:
                return
            # Existing checked-out asyncpg connection; getter sends no SQL.
            pid = conn.connection.driver_connection.get_server_pid()
            cleanup_waiters[pid] = {'task': tracked[1], 'waiting': waiting}

        def before_user_lock(*args):
            observe_user_lock(*args, waiting=True)

        def after_user_lock(*args):
            observe_user_lock(*args, waiting=False)

        async def known_cleanup_chain(pid, path, graph):
            if pid == pids['cleanup']:
                return True  # Same-owner User holder verified by gated_cleanup.
            info = cleanup_waiters.get(pid)
            if (pid in path or len(path) >= 8 or len(graph) >= 8 or info is None
                    or not info['waiting'] or info['task'].done()):
                return False
            blockers = await control.fetchval('SELECT pg_blocking_pids($1)', pid)
            graph[pid] = list(blockers)
            if not blockers:
                return False
            # Every blocker must be the gate or another live, same-owner cleanup
            # waiting in its actual User FOR UPDATE. Arbitrary PIDs never pass.
            for blocker in blockers:
                if not await known_cleanup_chain(blocker, (*path, pid), graph):
                    return False
            return True

        async def gated_cleanup(db, user, now):
            nonlocal claimed
            if user.id == owner and not claimed:
                claimed = True
                # Actual cleanup_user already acquired the production User row lock.
                pids['cleanup'] = await db.scalar(text('SELECT pg_backend_pid()'))
                entered.set()
                await release.wait()
            count = await original_cleanup(db, user, now)
            if user.id == owner:
                cleared_counts.append(count)
            return count

        async def observed_lock(db):
            task = asyncio.current_task()
            if task is admin_task:
                pids['admin'] = await db.scalar(text('SELECT pg_backend_pid()'))
                admin_entered.set()
            row = await original_lock(db)
            if task is create_task:
                pids['create'] = await db.scalar(text('SELECT pg_backend_pid()'))
                capacity_held.set()
            return row

        monkeypatch.setattr(cleanup, '_cleanup_user', gated_cleanup)
        monkeypatch.setattr(cleanup, 'cleanup_user', observed_cleanup_user)
        monkeypatch.setattr(app.state.capacity, 'lock', observed_lock)
        engine = app.state.db.engine.sync_engine
        event.listen(engine, 'before_cursor_execute', before_user_lock)
        event.listen(engine, 'after_cursor_execute', after_user_lock)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://testserver') as http:
                view = await http.get('/api/admin/capacity', headers=admin)
                assert view.status_code == 200
                config = view.json()['config']
                target = config['max_active_sessions'] - 1
                assert target >= 1
                # Prepare only expiry metadata on a real completed API/WS result.
                async with app.state.db.session_factory() as db:
                    user = await db.scalar(select(User).where(User.id == owner).with_for_update())
                    user.history_retention_days = 7
                    row = await db.scalar(select(VoiceSession).where(VoiceSession.id == sid,
                        VoiceSession.user_id == owner).with_for_update())
                    row.first_terminal_at -= timedelta(days=8)
                    assert row.content_cleared_at is None
                    await db.commit()
                clean_task = asyncio.create_task(cleanup.cleanup_user(app, owner))
                async with asyncio.timeout(3):
                    await entered.wait()
                    create_task = asyncio.create_task(create_session(http, headers))
                    await capacity_held.wait()
                    admin_task = asyncio.create_task(http.patch('/api/admin/capacity', headers=admin,
                        json={'version': config['version'], 'max_active_sessions': target}))
                    await admin_entered.wait()
                    # Observe the real PG wait graph, never return fabricated lock results.
                    while True:
                        create_blockers = await control.fetchval('SELECT pg_blocking_pids($1)', pids['create'])
                        admin_blockers = await control.fetchval('SELECT pg_blocking_pids($1)', pids['admin'])
                        graph = {pids['create']: list(create_blockers)}
                        if create_blockers and pids['create'] in admin_blockers:
                            valid = True
                            for blocker in create_blockers:
                                if not await known_cleanup_chain(blocker, (pids['create'],), graph):
                                    valid = False
                                    break
                            if valid:
                                verified_graph = graph
                                break
                        await asyncio.sleep(.005)
                assert not create_task.done() and not admin_task.done()
                release.set()
                async with asyncio.timeout(5):
                    created, changed, count = await asyncio.gather(create_task, admin_task, clean_task)
                assert created.status_code == 201 and changed.status_code == 200
                new_sid = created.json()['session_id']
                assert count in (0, 1)  # Periodic cleanup may have won the same genuine User lock.
                assert sum(cleared_counts) == 1
                assert changed.json()['config']['version'] == config['version'] + 1
                assert changed.json()['config']['max_active_sessions'] == target
                async with app.state.db.session_factory() as db:
                    old = await db.get(VoiceSession, sid)
                    result = await db.get(SessionResult, old.current_result_id)
                    attempt = await db.scalar(select(SessionAttempt).where(SessionAttempt.session_id == sid,
                        SessionAttempt.user_id == owner, SessionAttempt.attempt_no == old.current_attempt_no))
                    assert old.content_cleared_at and old.asr_hints is None and old.llm_hints is None
                    assert result.content_cleared_at and result.raw_text is None and result.refined_text is None
                    assert attempt.raw_text is None
                    capacity = (await db.scalars(select(RuntimeCapacity))).all()
                    audits = (await db.scalars(select(CapacityChange))).all()
                    assert len(capacity) == len(audits) == 1
                    assert (audits[0].from_version, audits[0].to_version) == (config['version'], config['version'] + 1)
                    assert (audits[0].old_max_active, audits[0].new_max_active) == (config['max_active_sessions'], target)
                assert await permanent(app, owner, sid) == before
                assert await cleanup.cleanup_user(app, owner) == 0
                assert await permanent(app, owner, sid) == before
                record_property('cap24_cleanup_lock_order', json.dumps({
                    'actual_pg_edges': ['create reaches gated cleanup(User) via verified same-owner cleanup waiters', 'admin waits create(capacity)'],
                    'actual_create_wait_graph': verified_graph,
                    'old_bodies_cleared_once': True, 'permanent_contributions_unchanged': True,
                    'new_create_status': 201, 'capacity_patch_status': 200,
                    'capacity_rows': 1, 'audit_rows': 1, 'synthetic_age_only': True,
                }, sort_keys=True))
        finally:
            release.set()
            event.remove(engine, 'before_cursor_execute', before_user_lock)
            event.remove(engine, 'after_cursor_execute', after_user_lock)
            for task in (create_task, admin_task, clean_task):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(t for t in (create_task, admin_task, clean_task) if t is not None), return_exceptions=True)
            if new_sid is not None:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://testserver') as http:
                    response = await http.post(f'/api/v1/sessions/{new_sid}/cancel', headers=headers)
                    assert response.status_code == 200
            await control.close()
    app_client.portal.call(run)


@pytest.mark.parametrize('phase', ['streaming', 'asr_finish', 'llm_generate'])
def test_cap24_waiting_ws_or_model_does_not_hold_pool_lease(app_client, monkeypatch, record_property, phase):
    tokens = register_and_login(app_client, email=f'cap24-{phase}@example.invalid')
    headers, app = auth_headers(tokens), app_client.app

    async def run():
        require_owned_database()
        assert app.state.config.asr.provider == app.state.config.llm.provider == 'mock'
        entered, release = asyncio.Event(), asyncio.Event()
        original_finish, original_generate = MockAsrSession.finish, MockLlmProvider.generate
        control = await control_connection()
        ws = None
        sid = None
        gate_hits = 0
        samples = []

        async def finish(self, total_samples, deadline):
            nonlocal gate_hits
            if phase == 'asr_finish' and self.options.session_id == sid:
                gate_hits += 1; entered.set(); await release.wait()
            return await original_finish(self, total_samples, deadline)

        async def generate(self, request, deadline):
            nonlocal gate_hits
            if phase == 'llm_generate' and request.purpose == 'refine':
                gate_hits += 1; entered.set(); await release.wait()
            return await original_generate(self, request, deadline)

        monkeypatch.setattr(MockAsrSession, 'finish', finish)
        monkeypatch.setattr(MockLlmProvider, 'generate', generate)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://testserver') as http:
                response = await create_session(http, headers)
                assert response.status_code == 201
                sid = response.json()['session_id']
                ws = await WsProbe(app, headers, sid).start()
                if phase != 'streaming':
                    await ws.end()
                    ws.drain_task = asyncio.create_task(ws.drain())
                    async with asyncio.timeout(3):
                        await entered.wait()
                began = time.monotonic()
                # Observe across a complete normal watchdog tick. This does not
                # change the actual 1s auth / 10s physical / 30s attempt budgets.
                while time.monotonic() - began < 1.1:
                    checkedout = app.state.db.engine.pool.checkedout()
                    idle_tx = await control.fetchval("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid() AND state='idle in transaction'")
                    samples.append({'elapsed_ms': round((time.monotonic() - began) * 1000, 3),
                                    'checkedout': checkedout, 'idle_in_transaction': idle_tx})
                    assert ws.task is not None and not ws.task.done()
                    if phase != 'streaming':
                        assert not release.is_set() and gate_hits == 1
                    await asyncio.sleep(.02)
                zero_points = [s for s in samples if s['checkedout'] == s['idle_in_transaction'] == 0]
                assert len(zero_points) >= 2
                assert zero_points[-1]['elapsed_ms'] - zero_points[0]['elapsed_ms'] >= 500
                current = await http.get(f'/api/v1/sessions/{sid}', headers=headers)
                assert current.status_code == 200
                assert current.json()['state'] == {'streaming': 'streaming', 'asr_finish': 'finalizing', 'llm_generate': 'refining'}[phase]
                release.set()
                if phase == 'streaming':
                    await ws.end()
                    ws.drain_task = asyncio.create_task(ws.drain())
                async with asyncio.timeout(5):
                    await ws.drain_task
                assert ws.close_code == 1000 and any(e['type'] == 'result.ready' for e in ws.events)
                record_property('cap24_wait_pool', json.dumps({'phase': phase, 'gate_hits': gate_hits,
                    'observations': samples, 'result_ready': True, 'synthetic_pcm_and_mock_physical_provider': True}, sort_keys=True))
        finally:
            release.set()
            if ws is not None and ws.task is not None and not ws.task.done():
                if sid is not None:
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://testserver') as http:
                        await http.post(f'/api/v1/sessions/{sid}/cancel', headers=headers)
                if not ws.task.done():
                    await ws.send({'type': 'websocket.disconnect', 'code': 1000})
            for task in (getattr(ws, 'task', None), getattr(ws, 'drain_task', None)):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(t for t in (getattr(ws, 'task', None), getattr(ws, 'drain_task', None)) if t is not None), return_exceptions=True)
            await control.close()
    app_client.portal.call(run)
