"""S2 engine isolation contract, on a database created only by this test.

No app_client/shared test database, roles, server defaults, services, models, or
user rows are changed. The new empty database is preserved with its original
per-database setting restored, including on the expected baseline failure.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import uuid

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import load_config
from app.core.db import Database
from tests.conftest import PG_HOST, PG_PASSWORD, PG_PORT, PG_USER, SERVER_ROOT


@pytest.mark.asyncio
async def test_database_engine_pins_read_committed_when_owned_database_defaults_to_repeatable_read(
    monkeypatch, record_property,
):
    name = f"smartvoiceinput_test_iso_{os.getpid()}_{uuid.uuid4().hex[:10]}"
    assert re.fullmatch(r"smartvoiceinput_test_iso_[0-9]+_[a-f0-9]{10}", name)
    assert len(name.encode("ascii")) < 64
    identifier = f'"{name}"'  # Generated identifier only; never supplied by env/user.
    credentials = dict(host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD)
    url = URL.create("postgresql+asyncpg", username=PG_USER, password=PG_PASSWORD,
                     host=PG_HOST, port=PG_PORT, database=name)
    evidence = {"database": name, "created_by_this_test": False,
                "default_restored": False, "application_observations": [],
                "cleanup_errors": [], "cleanup_cancel_requests": 0,
                "cleanup_completed": False}
    admin = control = application = None
    database_oid = owner_oid = None
    original_setting = original_effective = None
    restore_needed = False

    async def verify_owned_database(connection):
        # Recheck identity before every ALTER; collision or replacement is never
        # permission to modify an existing database with the same text name.
        assert evidence["created_by_this_test"] and database_oid is not None
        row = await connection.fetchrow(
            "SELECT oid, datdba, datdba=(SELECT oid FROM pg_roles WHERE rolname=current_user) AS owned "
            "FROM pg_database WHERE datname=$1", name, timeout=3)
        assert row is not None and row["oid"] == database_oid
        assert row["datdba"] == owner_oid and row["owned"] is True

    async def database_setting(connection):
        return await connection.fetchval(
            "SELECT substr(setting, length('default_transaction_isolation=')+1) "
            "FROM pg_db_role_setting CROSS JOIN LATERAL unnest(setconfig) AS item(setting) "
            "WHERE setdatabase=$1 AND setrole=0 "
            "AND setting LIKE 'default_transaction_isolation=%'", database_oid, timeout=3)

    async def raw_isolation():
        raw = await asyncpg.connect(**credentials, database=name, timeout=3)
        try:
            return (await raw.fetchval("SHOW default_transaction_isolation", timeout=3),
                    await raw.fetchval("SHOW transaction_isolation", timeout=3))
        finally:
            await raw.close(timeout=3)

    async def observe_application(connection, phase):
        value = await connection.scalar(text("SHOW transaction_isolation"))
        pid = await connection.scalar(text("SELECT pg_backend_pid()"))
        evidence["application_observations"].append({"phase": phase, "isolation": value, "pid": pid})
        assert value == "read committed", "application inherited deployment default instead of S2 READ COMMITTED"
        return pid

    async def cleanup():
        async def step(label, budget, operation):
            try:
                # Fixture cleanup budgets only; never passed to production
                # transactions, authorization, model calls or admission.
                async with asyncio.timeout(budget):
                    await operation()
            except Exception as error:
                evidence["cleanup_errors"].append({"stage": label, "type": type(error).__name__})

        async def close_setup_connection():
            if admin is not None and not admin.is_closed():
                await admin.close(timeout=3)
            if admin is not None:
                assert admin.is_closed()

        async def restore_owned_default():
            if not restore_needed:
                return
            # A cancelled ALTER has an uncertain client-side outcome. Close its
            # owned setup connection first, then use a fresh connection to avoid
            # relying on a broken/cancelled command channel for restoration.
            assert admin is not None and admin.is_closed(), "setup ALTER channel has not closed"
            repair = await asyncpg.connect(**credentials, database="postgres", timeout=3)
            try:
                await verify_owned_database(repair)
                if original_setting is None:
                    await repair.execute(f"ALTER DATABASE {identifier} RESET default_transaction_isolation", timeout=3)
                else:
                    assert original_setting in (
                        "read uncommitted", "read committed", "repeatable read", "serializable")
                    await repair.execute(
                        f"ALTER DATABASE {identifier} SET default_transaction_isolation TO '{original_setting}'", timeout=3)
                assert await database_setting(repair) == original_setting
                assert await raw_isolation() == original_effective
                evidence["default_restored"] = True
            finally:
                await repair.close(timeout=3)

        # Restore before potentially slow application/control pool disposal.
        # Ordinary driver closes affect only connections owned by this test;
        # there is no DROP, terminate(), pg_terminate_backend or role/system SET.
        await step("close_setup_connection", 4, close_setup_connection)
        await step("restore_owned_database_default", 12, restore_owned_default)
        if application is not None:
            await step("dispose_application", 4, application.close)
        if control is not None:
            await step("dispose_control", 4, control.dispose)
        evidence["cleanup_completed"] = True
        if evidence["cleanup_errors"]:
            stages = ", ".join(item["stage"] + ":" + item["type"] for item in evidence["cleanup_errors"])
            raise RuntimeError("owned isolation fixture cleanup failed: " + stages) from None

    async def complete_cleanup():
        task = asyncio.create_task(cleanup(), name="owned-isolation-fixture-cleanup")
        cancelled = None
        try:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError as error:
                    if task.cancelled():
                        raise RuntimeError("owned isolation fixture cleanup task itself was cancelled") from None
                    # A second cancellation must not interrupt the restore.
                    # Preserve caller cancellation until the bounded cleanup
                    # task has completed; never report cancellation as a pass.
                    evidence["cleanup_cancel_requests"] += 1
                    cancelled = error
            task.result()  # Propagate cleanup failure; do not swallow it.
            if cancelled is not None:
                raise cancelled
        finally:
            # Even failed setup-close/restore/dispose leaves a clear property.
            record_property("owned_database_isolation", json.dumps(evidence, sort_keys=True))

    try:
        admin = await asyncpg.connect(**credentials, database="postgres")
        # CREATE itself is the ownership claim. A collision fails, with no
        # DROP/RESET/ALTER attempted against the colliding database.
        await admin.execute(f"CREATE DATABASE {identifier}")
        evidence["created_by_this_test"] = True
        identity = await admin.fetchrow("SELECT oid, datdba FROM pg_database WHERE datname=$1", name)
        assert identity is not None
        database_oid, owner_oid = identity["oid"], identity["datdba"]
        await verify_owned_database(admin)
        original_setting = await database_setting(admin)
        original_effective = await raw_isolation()
        evidence["original_database_setting"] = original_setting
        evidence["original_raw_isolation"] = original_effective

        # Mark BEFORE dispatch: cancellation/transport failure can lose an
        # ALTER acknowledgement even when PostgreSQL already committed it.
        restore_needed = True
        await admin.execute(f"ALTER DATABASE {identifier} SET default_transaction_isolation TO 'repeatable read'")
        assert await database_setting(admin) == "repeatable read"
        raw_default, raw_transaction = await raw_isolation()
        evidence["altered_raw_isolation"] = [raw_default, raw_transaction]
        assert (raw_default, raw_transaction) == ("repeatable read", "repeatable read"), (
            "invalid comparison fixture: fresh raw connection does not inherit this database's RR default; "
            "a role/session override must not be hidden by changing global or role settings"
        )

        # Independent negative control establishes that an unconfigured
        # SQLAlchemy/asyncpg engine really inherits RR in this environment.
        control = create_async_engine(url, pool_size=1, max_overflow=0)
        async with control.connect() as connection:
            inherited = await connection.scalar(text("SHOW transaction_isolation"))
            evidence["unconfigured_engine_isolation"] = inherited
            assert inherited == "repeatable read"

        monkeypatch.setenv("SVI_DATABASE_URL", url.render_as_string(hide_password=False))
        monkeypatch.setenv("SVI_ENVIRONMENT", "development")
        config = load_config(str(SERVER_ROOT / "config" / "app.yaml"))
        # Use production config and Database, without app lifespan or migrations.
        assert make_url(config.database_url) == url, "application must target only this test-created database"
        application = Database(config)
        async with application.engine.connect() as first:
            first_pid = await observe_application(first, "first_checkout")
            async with application.engine.connect() as second:
                second_pid = await observe_application(second, "second_physical_connection")
                assert second_pid != first_pid
        async with application.session_factory() as session:
            await observe_application(session, "session_factory_after_checkin")
            await session.commit()
            await observe_application(session, "second_session_transaction")
        # Force fresh physical connections without changing production settings;
        # the engine's contract must survive pool disposal/recreation as well.
        await application.engine.dispose()
        async with application.session_factory() as session:
            await observe_application(session, "new_connection_after_dispose")
    finally:
        await complete_cleanup()
