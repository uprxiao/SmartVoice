"""Tracked startup cancellation and preservation of the observed fence reason.

Real process-owned PostgreSQL, app lifespan, SQLAlchemy rollback and API handler.
Scheduling gates pause a real uncommitted transaction; they never fabricate its
rollback outcome. No backend termination, model call, listener or production DB.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
import uuid

import asyncpg
import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app import main
from tests.conftest import PG_HOST, PG_PASSWORD, PG_PORT, PG_USER, SERVER_ROOT, TEST_DB, TEST_URL
from tests.integration.test_instance_recovery_loss import require_process_owned_database


async def signal(event, seconds=5):
    # Harness anti-hang only; no timeout is passed into production work.
    async with asyncio.timeout(seconds):
        await event.wait()


async def lock_present(control, pid, key):
    unsigned = key % (1 << 64)
    return bool(await control.fetchval("""SELECT EXISTS(
        SELECT 1 FROM pg_locks WHERE locktype='advisory' AND granted
        AND pid=$1 AND database=(SELECT oid FROM pg_database WHERE datname=$2)
        AND classid::bigint=$3 AND objid::bigint=$4 AND objsubid=1)""",
        pid, TEST_DB, unsigned >> 32, unsigned & 0xffffffff))


@pytest.mark.asyncio
@pytest.mark.parametrize("repeat_parent_cancel", [False, True], ids=["external-cancel", "repeated-parent-cancel"])
async def test_startup_external_cancel_waits_for_real_transaction_rollback(
    clean_database, monkeypatch, record_property, repeat_parent_cancel,
):
    require_process_owned_database()
    for name, value in {
        "SVI_DATABASE_URL": TEST_URL, "SVI_CONFIG": str(SERVER_ROOT / "config/app.yaml"),
        "SVI_INSTANCE_LOCK": "0", "SVI_ENVIRONMENT": "development",
        "SVI_ASR_PROVIDER": "mock", "SVI_LLM_PROVIDER": "mock",
        "WEB_CONCURRENCY": "1", "UVICORN_WORKERS": "1",
    }.items():
        monkeypatch.setenv(name, value)
    # Existing loss test independently proves the real instance watcher. Here
    # skip only that unrelated watcher; the held advisory lock is a genuine
    # transaction-scoped lock acquired by the startup-maintenance transaction.
    entered, rollback_pending, allow_rollback = asyncio.Event(), asyncio.Event(), asyncio.Event()
    cleanup_ended = asyncio.Event()
    observed = SimpleNamespace(worker=None, pid=None, before_version=None,
        first_cancel_count=None, second_cancel_reached_child=False,
        db_close_calls=0, db_close_completed=0, checkout_before_dispose=None)
    xact_key = uuid.uuid4().int & ((1 << 63) - 1)
    actual_database = main.Database

    def database_factory(config):
        database = actual_database(config)
        close = database.close

        async def observe_close():
            observed.db_close_calls += 1
            observed.checkout_before_dispose = database.engine.pool.checkedout()
            await close()  # Delegate real disposal, no fabricated cleanup flag.
            observed.db_close_completed += 1

        database.close = observe_close
        return database

    async def gated_startup_transaction(database, *, identities=None):
        assert identities == (), "this fresh fixture must pass an explicitly empty session snapshot"
        observed.worker = asyncio.current_task()
        try:
            async with database.session_factory() as db:
                async with db.begin():
                    observed.pid = await db.scalar(text("SELECT pg_backend_pid()"))
                    observed.before_version = await db.scalar(text(
                        "SELECT version FROM runtime_capacity WHERE id=1"))
                    assert observed.before_version is not None
                    # Valid, private, uncommitted sentinel. The original value
                    # must be read back after cancellation, not guessed from it.
                    await db.execute(text("UPDATE runtime_capacity SET version=version+1 WHERE id=1"))
                    assert await db.scalar(text("SELECT version FROM runtime_capacity WHERE id=1")) == observed.before_version + 1
                    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": xact_key})
                    entered.set()
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        observed.first_cancel_count = observed.worker.cancelling()
                        rollback_pending.set()
                        # A scheduling gate immediately before leaving the real
                        # transaction context. No rollback method is replaced.
                        try:
                            await allow_rollback.wait()
                        except asyncio.CancelledError:
                            observed.second_cancel_reached_child = True
                            raise
                        raise
        finally:
            cleanup_ended.set()

    monkeypatch.setattr(main, "Database", database_factory)
    monkeypatch.setattr(main, "_recover_interrupted_sessions", gated_startup_transaction)
    app = main.create_app(str(SERVER_ROOT / "config/app.yaml"))
    reached_serving = asyncio.Event()

    async def run_lifespan():
        async with app.router.lifespan_context(app):
            reached_serving.set()
            await asyncio.Event().wait()

    parent = None
    control = await asyncpg.connect(host=PG_HOST, port=PG_PORT, user=PG_USER,
                                   password=PG_PASSWORD, database=TEST_DB)
    try:
        assert await control.fetchval("SELECT current_database()") == TEST_DB
        parent = asyncio.create_task(run_lifespan())
        await signal(entered, 8)
        assert not reached_serving.is_set()
        assert await lock_present(control, observed.pid, xact_key)
        assert await control.fetchval("SELECT version FROM runtime_capacity WHERE id=1") == observed.before_version
        parent.cancel()
        await signal(rollback_pending)
        if repeat_parent_cancel:
            # Yield one turn so the first external cancellation has created the
            # shutdown owner. No sleeps/extra business deadline are introduced.
            await asyncio.sleep(0)
            parent.cancel()
            await asyncio.sleep(0)
        assert not parent.done(), "lifespan returned before real transaction cleanup"
        assert not cleanup_ended.is_set()
        assert not observed.second_cancel_reached_child
        assert observed.worker.cancelling() == observed.first_cancel_count == 1
        assert await lock_present(control, observed.pid, xact_key)
        assert observed.db_close_completed == 0
        allow_rollback.set()
        async with asyncio.timeout(8):
            with pytest.raises(asyncio.CancelledError):
                await asyncio.shield(parent)
        assert parent.cancelled() and cleanup_ended.is_set()
        assert not reached_serving.is_set()
        assert observed.worker is app.state.startup_task and observed.worker is not parent
        assert observed.worker.cancelled() and observed.worker.cancelling() == 1
        assert not await lock_present(control, observed.pid, xact_key)
        assert await control.fetchval("SELECT version FROM runtime_capacity WHERE id=1") == observed.before_version
        assert observed.db_close_calls == observed.db_close_completed == 1
        assert observed.checkout_before_dispose == 0
        assert app.state.db.engine.pool.checkedout() == 0
        assert app.state.shutdown_task.done() and not app.state.shutdown_task.cancelled()
        for name in ("startup_task", "probe_task", "cleanup_task", "llm_prepare_task"):
            task = getattr(app.state, name, None)
            assert task is None or task.done(), name
        record_property("transaction_cancel_count", observed.first_cancel_count)
        record_property("repeated_parent_cancel", repeat_parent_cancel)
        record_property("rollback_checked_by_real_lock_and_version", True)
        record_property("pool_checkedout_before_dispose", observed.checkout_before_dispose)
    finally:
        allow_rollback.set()
        try:
            if parent is not None and not parent.done() and not parent.cancelling():
                parent.cancel()
            if parent is not None:
                async with asyncio.timeout(8):
                    await asyncio.gather(parent, return_exceptions=True)
        finally:
            try:
                # Baseline may lack the startup cleanup owner. Close only this
                # test's engine after releasing its gate; assertions above are
                # evaluated before this defensive resource release.
                if getattr(app.state, "db", None) is not None and observed.db_close_completed == 0:
                    async with asyncio.timeout(5):
                        await app.state.db.close()
            finally:
                await control.close(timeout=5)


def test_fenced_reason_survives_inflight_readiness_and_real_api_database_errors(
    app_client, monkeypatch, record_property,
):
    require_process_owned_database()

    async def run():
        app = app_client.app
        assert app.state.ready and not app.state.instance_fenced
        previous_manager = app.state.sessions
        release = asyncio.Event()
        entered = {kind: asyncio.Event() for kind in ("readiness", "request")}
        tasks, phases, states, fired = {}, {}, {}, set()
        scalar = AsyncSession.scalar

        async def fault_scalar(session, statement, *args, **kwargs):
            phase = phases.get(asyncio.current_task())
            if phase is None or phase in fired:
                return await scalar(session, statement, *args, **kwargs)
            fired.add(phase)
            entered[phase].set()
            await release.wait()
            try:
                # Genuine PG failure on that same actual session. Only the two
                # selected in-flight tasks are affected; no fake DB exception.
                return await scalar(session, text("SELECT 1 / 0"))
            except DBAPIError as exc:
                states[phase] = getattr(exc.orig, "sqlstate", None)
                raise

        with monkeypatch.context() as patch:
            patch.setattr(AsyncSession, "scalar", fault_scalar)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://instance-fence-fixture") as http:
                request_id = str(uuid.uuid4())
                tasks["readiness"] = asyncio.create_task(main._refresh_readiness(app))
                tasks["request"] = asyncio.create_task(http.post("/api/auth/register",
                    headers={"X-Request-Id": request_id},
                    json={"email": f"fence-{uuid.uuid4().hex}@example.invalid", "password": "FixtureOnly123!"}))
                phases.update({task: phase for phase, task in tasks.items()})
                try:
                    await signal(entered["readiness"])
                    await signal(entered["request"])
                    # Real fence operation, no direct assignment of state flags.
                    # These already in-flight callers are deliberately distinct
                    # from the tracked periodic probe that fence cancels.
                    async with asyncio.timeout(8):
                        await main._fence_instance(app)
                    assert app.state.instance_fenced and not app.state.ready
                    assert app.state.unavailable_reason == "INSTANCE_LOCK_LOST"
                    release.set()
                    async with asyncio.timeout(5):
                        readiness, response = await asyncio.gather(tasks["readiness"], tasks["request"])
                    assert readiness is None and states == {"readiness": "22012", "request": "22012"}
                    assert response.status_code == 503
                    assert response.headers["x-request-id"] == request_id
                    error = response.json()["error"]
                    assert error["code"] == "SERVICE_UNAVAILABLE" and error["retryable"] is True
                    assert error["request_id"] == request_id
                    assert all(value not in response.text for value in ("SELECT", "22012", "FixtureOnly123!", "postgresql"))
                    assert app.state.instance_fenced and not app.state.ready and app.state.degraded
                    assert app.state.unavailable_reason == "INSTANCE_LOCK_LOST"
                    assert app.state.sessions is previous_manager
                    await main._refresh_readiness(app)
                    health = await http.get("/health/ready")
                    assert health.status_code == 503 and health.json()["reason_code"] == "INSTANCE_LOCK_LOST"
                    assert app.state.unavailable_reason == "INSTANCE_LOCK_LOST"
                    for name in ("probe_task", "cleanup_task", "llm_prepare_task"):
                        task = getattr(app.state, name, None)
                        assert task is None or task.done(), name
                    return states
                finally:
                    release.set()
                    async with asyncio.timeout(8):
                        await asyncio.gather(*tasks.values(), return_exceptions=True)

    observed = app_client.portal.call(run)
    record_property("real_pg_sqlstates", sorted(observed.values()))
    record_property("fenced_reason_after_late_failures", "INSTANCE_LOCK_LOST")
