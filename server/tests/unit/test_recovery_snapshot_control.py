"""Control-boundary tests. Fake SQL rows are not PostgreSQL evidence."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app import main
from app.services.cleanup import mark_restarted_pending
from app.services.model_call_recorder import DatabaseCallRecorder
from app.services.recovery_snapshot import (
    RecoveryOwnershipLost, capture_recovery_snapshot,
)


class Result:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows


class Connection:
    closed = invalidated = False

    def __init__(self, *, owned=True, pid=73, commit_error=None):
        self.owned, self.pid, self.commit_error = owned, pid, commit_error
        self.executed = []
        self.transaction_exited = self.committed = False

    def begin(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, error_type, error, traceback):
        self.transaction_exited = True
        if error_type is None:
            if self.commit_error is not None:
                raise self.commit_error
            self.committed = True
        return False

    async def execute(self, statement, parameters):
        self.executed.append((statement, parameters))
        return Result([{
            "kind": "ownership", "user_id": None, "resource_id": None,
            "attempt_no": None, "state": None, "retry_cache_status": None,
            "actual_pid": self.pid, "owned": self.owned,
        }])


def test_snapshot_publishes_empty_tuple_only_after_transaction_exit():
    connection = Connection()
    answer = asyncio.run(capture_recovery_snapshot(connection, backend_pid=73, key=42))
    assert connection.committed and connection.transaction_exited
    assert answer.ownership_checked is True
    assert answer.learning == answer.sessions == answer.calls == ()
    assert len(connection.executed) == 1
    statement, parameters = connection.executed[0]
    # This only inspects the intended query. Real PG must establish its syntax,
    # snapshot consistency, PID/lock predicates and commit-failure behavior.
    query = str(statement)
    assert all(table in query for table in ("pg_locks", "learning_events", "sessions", "model_calls"))
    assert "pg_try_advisory_lock" not in query
    assert parameters["require_lock"] is True and parameters["pid"] == 73


@pytest.mark.parametrize("connection", [Connection(owned=False), Connection(pid=74)])
def test_snapshot_rejects_lost_lock_or_changed_backend(connection):
    with pytest.raises(RecoveryOwnershipLost):
        asyncio.run(capture_recovery_snapshot(connection, backend_pid=73, key=42))
    assert connection.transaction_exited and not connection.committed


def test_snapshot_commit_error_does_not_publish_rows():
    connection = Connection(commit_error=RuntimeError("controlled commit fault"))
    with pytest.raises(RecoveryOwnershipLost) as caught:
        asyncio.run(capture_recovery_snapshot(connection, backend_pid=73, key=42))
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert connection.transaction_exited and not connection.committed
    assert len(connection.executed) == 1


def test_snapshot_external_cancellation_propagates_without_publication():
    connection = Connection(commit_error=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(capture_recovery_snapshot(connection, backend_pid=73, key=42))
    assert connection.transaction_exited and not connection.committed


def test_snapshot_invalidated_connection_never_reconnects():
    connection = Connection()
    connection.invalidated = True
    with pytest.raises(RecoveryOwnershipLost):
        asyncio.run(capture_recovery_snapshot(connection, backend_pid=73, key=42))
    assert connection.executed == [] and not connection.transaction_exited


def test_explicit_skip_mode_remains_a_frozen_cohort_without_ownership_claim():
    connection = Connection()
    answer = asyncio.run(capture_recovery_snapshot(connection, require_lock=False))
    assert connection.committed and answer.ownership_checked is False
    assert answer.learning == answer.sessions == answer.calls == ()
    assert connection.executed[0][1]["require_lock"] is False


def test_explicit_empty_identities_never_fall_back_to_global_scan():
    class ForbiddenDatabase:
        def session_factory(self):
            raise AssertionError("empty frozen cohort must not query any later identity")

    async def check():
        database = ForbiddenDatabase()
        await mark_restarted_pending(SimpleNamespace(state=SimpleNamespace(db=database)), identities=())
        await main._recover_interrupted_sessions(database, identities=())
        assert await DatabaseCallRecorder(database).recover_incomplete(identities=()) == 0

    asyncio.run(check())


def test_heartbeat_mutex_wait_is_inside_existing_two_second_budget():
    async def check():
        mutex = asyncio.Lock()
        await mutex.acquire()
        connection = SimpleNamespace(closed=False, invalidated=False)
        # No scalar/commit exists: it must time out waiting, before any SQL.
        started = asyncio.get_running_loop().time()
        try:
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(4):  # Harness bound; production must still fail by 2s.
                    await main._instance_lock_alive(connection, 42, 73, connection_mutex=mutex)
            elapsed = asyncio.get_running_loop().time() - started
            assert 1.9 <= elapsed < 3.0
        finally:
            mutex.release()

    asyncio.run(check())
