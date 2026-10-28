"""Real PostgreSQL transaction faults; no fake Python database exceptions.

Requires test_recovery_snapshot_pg.py alongside this module. These test direct
capture transaction boundaries, NOT the full startup watcher/mutex lifecycle.
No DB/backend is terminated, no existing table/database is dropped or truncated.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import event, text

from app.services.recovery_snapshot import RecoveryOwnershipLost, capture_recovery_snapshot
from tests.integration.test_recovery_snapshot_pg import (
    fingerprints, mock_prepare_only, owned_connection, owned_locks, register_actor, seed_call,
)


def sqlstate(error):
    for _ in range(6):
        if error is None:
            return None
        value = getattr(error, "sqlstate", None) or getattr(error, "pgcode", None)
        if value is not None:
            return value
        error = getattr(error, "orig", None) or getattr(error, "__cause__", None)
    return None


def test_pg_deferred_unique_failure_at_snapshot_commit_never_publishes(app_client):
    actor = register_actor(app_client, "commit-failure")

    async def run():
        database = app_client.app.state.db
        await seed_call(app_client.app.state.recorder, actor)
        before = await fingerprints(database, actor)
        async with owned_connection(database) as (connection, pid, key):
            # Unique temp table exists only on this test's physical connection;
            # it is removed when the fixture engine closes that connection.
            name = "svi_capture_commit_" + uuid.uuid4().hex
            await connection.exec_driver_sql(f'CREATE TEMPORARY TABLE "{name}" '
                '(value integer, UNIQUE(value) DEFERRABLE INITIALLY DEFERRED)')
            await connection.commit()
            before_locks = await owned_locks(connection)
            observed = {"snapshot_sql_finished": False, "duplicate_inserted": False, "fired": 0}

            def after_cursor(conn, cursor, statement, parameters, context, executemany):
                if observed["fired"] or "WITH owner AS MATERIALIZED" not in statement:
                    return
                # Do not replace the snapshot query/results. Its real SELECT
                # has executed; this extra test-only INSERT succeeds now, while
                # its real deferred UNIQUE constraint must fail at COMMIT.
                observed["fired"] += 1
                observed["snapshot_sql_finished"] = True
                conn.exec_driver_sql(f'INSERT INTO "{name}" (value) VALUES (1), (1)')
                observed["duplicate_inserted"] = True

            event.listen(connection.sync_connection, "after_cursor_execute", after_cursor)
            published = []
            try:
                with pytest.raises(RecoveryOwnershipLost) as caught:
                    published.append(await capture_recovery_snapshot(connection, backend_pid=pid, key=key))
                assert sqlstate(caught.value) == "23505"
            finally:
                event.remove(connection.sync_connection, "after_cursor_execute", after_cursor)
            assert observed == {"snapshot_sql_finished": True, "duplicate_inserted": True, "fired": 1}
            assert published == [] and not connection.in_transaction()
            assert not connection.closed and not connection.invalidated
            assert await connection.scalar(text(f'SELECT count(*) FROM "{name}"')) == 0
            await connection.commit()
            assert await owned_locks(connection) == before_locks
            assert await fingerprints(database, actor) == before
            # Same physical owner remains usable; no reconnect/reacquire needed.
            snapshot = await capture_recovery_snapshot(connection, backend_pid=pid, key=key)
            assert len(snapshot.calls) == 1 and snapshot.ownership_checked is True

    app_client.portal.call(run)


def test_pg_cancel_snapshot_waiting_for_real_table_lock_rolls_back_without_publication(app_client):
    actor = register_actor(app_client, "capture-cancel")

    async def run():
        database = app_client.app.state.db
        await seed_call(app_client.app.state.recorder, actor)
        before = await fingerprints(database, actor)
        async with owned_connection(database, allow_connection_loss=True) as (connection, pid, key):
            before_locks = await owned_locks(connection)
            async with database.engine.connect() as blocker:
                await blocker.execute(text("LOCK TABLE model_calls IN ACCESS EXCLUSIVE MODE"))
                published = []

                async def capture():
                    answer = await capture_recovery_snapshot(connection, backend_pid=pid, key=key)
                    published.append(answer)

                task = asyncio.create_task(capture())
                try:
                    # The original capture SELECT really waits for a PG table
                    # lock; no wrapper adds a gate or replaces the SQL/result.
                    async with asyncio.timeout(4):
                        while not await blocker.scalar(text("""SELECT EXISTS(
                            SELECT 1 FROM pg_locks WHERE pid=:pid AND NOT granted
                              AND locktype='relation' AND relation='model_calls'::regclass)"""), {"pid": pid}):
                            if task.done():
                                await task
                                raise AssertionError("capture finished without the required real PG lock wait")
                            await asyncio.sleep(.01)
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        async with asyncio.timeout(4):
                            await task
                    assert task.cancelled() and task.cancelling() == 1 and published == []
                    assert not connection.in_transaction()
                    assert not await blocker.scalar(text("""SELECT EXISTS(
                        SELECT 1 FROM pg_locks WHERE pid=:pid AND NOT granted
                          AND locktype='relation' AND relation='model_calls'::regclass)"""), {"pid": pid})
                finally:
                    if not task.done() and not task.cancelling():
                        task.cancel()
                    # Always release this fixture's table lock before draining
                    # a failed observation, so diagnostics cannot strand PG.
                    await blocker.rollback()
                    async with asyncio.timeout(4):
                        await asyncio.gather(task, return_exceptions=True)
                if connection.invalidated or connection.closed:
                    # SQLAlchemy may invalidate the physical connection on
                    # CancelledError. That is valid failclosed cleanup, not a
                    # reason to reconnect it and pretend its old lock survived.
                    async with asyncio.timeout(4):
                        while await blocker.scalar(text(
                            "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=:pid)"), {"pid": pid}):
                            await asyncio.sleep(.01)
                    with pytest.raises(RecoveryOwnershipLost):
                        await capture_recovery_snapshot(connection, backend_pid=pid, key=key)
                else:
                    assert await owned_locks(connection) == before_locks
                    assert (await capture_recovery_snapshot(connection, backend_pid=pid, key=key)).ownership_checked is True
                await blocker.rollback()
            assert await fingerprints(database, actor) == before
        # A fresh explicit owner can capture again after the cancelled read.
        async with owned_connection(database) as (connection, pid, key):
            snapshot = await capture_recovery_snapshot(connection, backend_pid=pid, key=key)
            assert len(snapshot.calls) == 1 and snapshot.ownership_checked is True

    app_client.portal.call(run)
