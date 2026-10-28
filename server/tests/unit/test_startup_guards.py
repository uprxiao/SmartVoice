"""Guard control-flow tests; do not substitute for migration/lock integration."""
import asyncio
from types import SimpleNamespace
import pytest

from app import main


class Connection:
    closed = invalidated = False
    def __init__(self, *, alive=True):
        self.alive, self.queries, self.commits = alive, [], 0
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return False
    async def run_sync(self, callback):
        return callback(self)
    async def scalar(self, statement, params):
        self.queries.append((str(statement), params))
        return self.alive
    async def commit(self):
        self.commits += 1


def test_schema_requires_exact_migration_head(monkeypatch):
    connection = Connection()
    db = SimpleNamespace(engine=SimpleNamespace(connect=lambda: connection))
    monkeypatch.setattr(main.ScriptDirectory, 'from_config', lambda _: SimpleNamespace(get_heads=lambda: ['new']))
    monkeypatch.setattr(main.MigrationContext, 'configure', lambda _: SimpleNamespace(get_current_heads=lambda: ['old']))
    with pytest.raises(RuntimeError, match='SCHEMA_REVISION_MISMATCH'):
        asyncio.run(main._verify_schema(db))
    monkeypatch.setattr(main.MigrationContext, 'configure', lambda _: SimpleNamespace(get_current_heads=lambda: ['new']))
    asyncio.run(main._verify_schema(db))


def test_instance_guard_checks_existing_pid_and_lock_without_reacquiring():
    connection = Connection(alive=False)
    assert asyncio.run(main._instance_lock_alive(connection, 420017863, 123)) is False
    query, params = connection.queries[0]
    assert 'pg_backend_pid()' in query and 'pg_locks' in query
    assert 'pg_try_advisory_lock' not in query and params['pid'] == 123
    assert connection.commits == 1


def test_invalidated_owner_connection_is_not_reconnected():
    connection = Connection(); connection.invalidated = True
    assert asyncio.run(main._instance_lock_alive(connection, 1, 1)) is False
    assert not connection.queries
