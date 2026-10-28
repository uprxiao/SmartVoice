"""Real PostgreSQL cancellation/release draft; no model or throughput claim."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
import json
import uuid

from sqlalchemy import event, select, text

from app.models import LoginSession
from app.services.runtime_auth_batch import RuntimeAuthBatch, RuntimeAuthKey, RuntimeAuthUnavailable
from tests.conftest import register_and_login


def test_auth_query_original_deadline_cancels_pg_and_returns_pool_lease(app_client, record_property):
    tokens = register_and_login(app_client, email="auth-batch-cancel@example.invalid")
    owner = tokens["user"]["id"]
    marker = "svi_auth_cancel_" + uuid.uuid4().hex
    scope = ContextVar(marker, default=None)
    evidence = {"deadline_seconds": 1.0, "active_sleep_observed": False,
                "error_types": [], "sqlstates": []}

    async def run():
        db_owner = app_client.app.state.db
        engine = db_owner.engine.sync_engine
        async with db_owner.session_factory() as db:
            sid = await db.scalar(select(LoginSession.id).where(LoginSession.user_id == owner))
        assert sid is not None
        identity = RuntimeAuthKey(owner, sid, str(uuid.uuid4()))
        reader = RuntimeAuthBatch(db_owner)
        loans, by_dbapi, captured = {}, {}, []
        injected = False
        target = None
        auth_task = None
        deadline_handle = None

        def checkout(dbapi_connection, connection_record, connection_proxy):
            phase = scope.get()
            if phase not in ("auth", "recovery"):
                return  # Readiness/cleanup and the observer are not this lease.
            record_id = id(connection_record)
            assert record_id not in loans, "a tracked pool lease was checked out twice"
            loan = {"phase": phase, "record_id": record_id,
                    "dbapi_id": id(dbapi_connection), "checkins": 0}
            loans[record_id] = loan
            by_dbapi[id(dbapi_connection)] = loan
            captured.append(loan)

        def checkin(dbapi_connection, connection_record):
            # On invalidation SQLAlchemy may report None for dbapi_connection;
            # the stable connection_record still identifies the released lease.
            loan = loans.pop(id(connection_record), None)
            if loan is not None:
                loan["checkins"] += 1
                loan["invalidated_at_checkin"] = dbapi_connection is None
                by_dbapi.pop(loan["dbapi_id"], None)

        def before(connection, cursor, statement, parameters, context, many):
            nonlocal injected, target
            if scope.get() == "auth" and not injected and "runtime_auth_checks" in statement:
                target = by_dbapi.get(id(connection.connection.dbapi_connection))
                assert target is not None and target["phase"] == "auth"
                assert not many
                injected = True
                evidence["injected_at_loop_time"] = asyncio.get_running_loop().time()
                # Unique comment contains no account, token, query text or DSN.
                return f"SELECT pg_sleep(10) /* {marker} */", ()
            return statement, parameters

        def failed(context):
            if scope.get() != "auth":
                return
            original = context.original_exception
            evidence["error_types"].append(type(original).__name__)
            candidates = (original, getattr(original, "orig", None), getattr(original, "__cause__", None))
            sqlstate = next((getattr(error, "sqlstate", None) or getattr(error, "pgcode", None)
                             for error in candidates if error is not None
                             and (getattr(error, "sqlstate", None) or getattr(error, "pgcode", None))), None)
            evidence["sqlstates"].append(sqlstate)  # Observe; do not presume a cancellation code.

        async def active_sleeps(observer, only_marker=False):
            statement = text("SELECT count(*) FROM pg_stat_activity "
                "WHERE datname=current_database() AND pid<>pg_backend_pid() "
                "AND state='active' AND query ILIKE :pattern")
            count = int(await observer.scalar(statement,
                {"pattern": f"%{marker}%" if only_marker else "%pg_sleep%"}) or 0)
            # Repeated pg_stat_activity reads must not reuse a transaction's
            # cached statistics snapshot. No application row is changed.
            await observer.commit()
            return count

        listeners = ((engine.pool, "checkout", checkout), (engine.pool, "checkin", checkin),
                     (engine, "before_cursor_execute", before), (engine, "handle_error", failed))
        # Hold only this test's observer connection, acquired before listeners;
        # the existing pool remains at its original 5+5 configuration.
        async with db_owner.engine.connect() as observer:
            for source, name, listener in listeners:
                event.listen(source, name, listener, **({"retval": True} if name == "before_cursor_execute" else {}))
            try:
                loop = asyncio.get_running_loop()
                started = loop.time()
                deadline = started + 1.0  # Exactly the original authorization budget.
                evidence["started_at_loop_time"] = started
                evidence["deadline_loop_time"] = deadline
                deadline_reached = asyncio.Event()
                deadline_handle = loop.call_at(deadline, deadline_reached.set)

                async def authorize():
                    token = scope.set("auth")
                    try:
                        return await reader.read(identity, deadline)
                    finally:
                        evidence["reader_exit_loop_time"] = loop.time()
                        evidence["reader_elapsed_ms"] = (loop.time() - started) * 1000
                        scope.reset(token)

                auth_task = asyncio.create_task(authorize())
                while not auth_task.done():
                    if await active_sleeps(observer, only_marker=True):
                        evidence["active_sleep_observed"] = True
                        break
                    await asyncio.sleep(.01)
                assert injected and evidence["active_sleep_observed"], "the real pg_sleep was not observed running"

                # Observe the unchanged deadline; do not externally cancel the
                # authorization call to manufacture its timeout. Two scheduler
                # turns allow the timeout callback and consumer to run; wall
                # elapsed time is recorded rather than given a longer budget.
                await deadline_reached.wait()
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                assert auth_task.done(), "reader outlived its original deadline callbacks"
                outcome = (await asyncio.gather(auth_task, return_exceptions=True))[0]
                evidence["reader_outcome_type"] = type(outcome).__name__
                assert isinstance(outcome, (TimeoutError, RuntimeAuthUnavailable))
                assert evidence["reader_exit_loop_time"] >= deadline

                await reader.close()
                assert target is not None and target["checkins"] == 1
                assert len([loan for loan in captured if loan["phase"] == "auth"]) == 1
                assert not loans, "authorization lease still checked out after reader.close"
                assert reader._pending == [] and reader._current == ()
                assert reader._runner is None or reader._runner.done()

                token = scope.set("recovery")
                try:
                    # New checkout proves usable pool capacity; reusing a
                    # different physical connection after invalidation is valid.
                    async with db_owner.engine.connect() as recovered:
                        assert await recovered.scalar(text("SELECT 1")) == 1
                finally:
                    scope.reset(token)
                recovery = [loan for loan in captured if loan["phase"] == "recovery"]
                assert len(recovery) == 1 and recovery[0]["checkins"] == 1
                assert not loans
                evidence["active_pg_sleep_after_close"] = await active_sleeps(observer)
                assert evidence["active_pg_sleep_after_close"] == 0
            finally:
                if deadline_handle is not None:
                    deadline_handle.cancel()
                try:
                    if auth_task is not None and not auth_task.done():
                        auth_task.cancel()  # Failure cleanup only, never the passing-path timeout.
                    if auth_task is not None:
                        await asyncio.gather(auth_task, return_exceptions=True)
                    await reader.close()
                finally:
                    evidence["pool_leases"] = [{"phase": loan["phase"], "checkins": loan["checkins"],
                        "invalidated_at_checkin": loan.get("invalidated_at_checkin")}
                        for loan in captured]
                    evidence["unreturned_tracked_leases"] = len(loans)
                    for source, name, listener in reversed(listeners):
                        event.remove(source, name, listener)

    async def bounded():
        # Test anti-hang only; this value is never supplied to production read().
        async with asyncio.timeout(5):
            await run()

    try:
        app_client.portal.call(bounded)
    finally:
        record_property("runtime_auth_pg_cancellation", json.dumps(evidence, sort_keys=True))
