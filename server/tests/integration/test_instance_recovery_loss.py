"""Lost advisory owner must not recover the successor's call.

Use only the process-owned DB from tests/conftest.py. Both applications use real
lifespans, PG locks, recorder and ScheduledLlmProvider; physical inference is a
declared gated stub. No TCP, original service, model load or deployment change.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
import json
import os
from pathlib import Path
import re
import sys
import uuid

import asyncpg
import httpx
import pytest
from sqlalchemy import event

from app import main
from app.providers.llm.base import LlmRequest, LlmResponse
from app.providers.llm.scheduled import ScheduledLlmProvider
from app.services.inference_context import CallScope, call_scope
from app.services.model_call_recorder import DatabaseCallRecorder
from tests.conftest import (
    PG_HOST, PG_PASSWORD, PG_PORT, PG_USER, SERVER_ROOT, TEST_DB, TEST_URL,
    wait_for_readiness_async,
)


def require_process_owned_database():
    # pytest may load conftest twice under distinct module names. Require the
    # actual fixture module's successful CREATE flag, not merely a safe prefix.
    expected = (SERVER_ROOT / "tests" / "conftest.py").resolve()
    owners = [module for module in list(sys.modules.values())
              if isinstance(getattr(module, "__file__", None), str)
              and Path(module.__file__).resolve() == expected
              and getattr(module, "TEST_DB", None) == TEST_DB
              and getattr(module, "_CREATED_TEST_DB", False) is True]
    assert owners, "test process did not create this database; refusing any connection termination"
    assert re.fullmatch(r"smartvoiceinput_test_[a-zA-Z0-9_]{1,38}", TEST_DB)


async def wait_until(predicate, seconds=4):
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(.01)


async def call_snapshot(control, call_id):
    row = await control.fetchrow("""SELECT id,user_id,status,error_code,started_at,
        ended_at,aggregated_at,elapsed_ms,prompt_tokens,completion_tokens
        FROM model_calls WHERE id=$1""", call_id)
    assert row is not None
    return dict(row)


async def terminate_exact_test_owner(control, tag, key):
    require_process_owned_database()
    assert re.fullmatch(r"svi_recovery_\d+_[0-9a-f]{10}_A", tag)
    assert await control.fetchval("SELECT current_database()") == TEST_DB
    unsigned = key % (1 << 64)
    args = (TEST_DB, tag, unsigned >> 32, unsigned & 0xffffffff)
    rows = await control.fetch("""SELECT a.pid,a.backend_start
        FROM pg_stat_activity a
        WHERE a.datname=$1 AND a.application_name=$2
          AND a.pid<>pg_backend_pid()
          AND EXISTS(SELECT 1 FROM pg_locks l WHERE l.pid=a.pid
            AND l.locktype='advisory' AND l.granted AND l.mode='ExclusiveLock'
            AND l.database=(SELECT oid FROM pg_database WHERE datname=$1)
            AND l.classid::bigint=$3 AND l.objid::bigint=$4 AND l.objsubid=1)""", *args)
    assert len(rows) == 1, "expected exactly one tagged advisory backend; terminating none"
    pid, backend_start = rows[0]["pid"], rows[0]["backend_start"]
    # Recheck DB, random application tag, PID, backend generation and exact lock
    # in the statement that terminates. No prefix match, PID sweep or retry.
    killed = await control.fetch("""SELECT pg_terminate_backend(a.pid) AS terminated
        FROM pg_stat_activity a
        WHERE a.datname=$1 AND a.application_name=$2
          AND a.pid=$5 AND a.backend_start=$6 AND a.pid<>pg_backend_pid()
          AND EXISTS(SELECT 1 FROM pg_locks l WHERE l.pid=a.pid
            AND l.locktype='advisory' AND l.granted AND l.mode='ExclusiveLock'
            AND l.database=(SELECT oid FROM pg_database WHERE datname=$1)
            AND l.classid::bigint=$3 AND l.objid::bigint=$4 AND l.objsubid=1)""",
        *args, pid, backend_start)
    assert len(killed) == 1 and killed[0]["terminated"] is True
    async with asyncio.timeout(4):
        while await control.fetchval("""SELECT EXISTS(SELECT 1 FROM pg_stat_activity
                WHERE datname=$1 AND application_name=$2 AND pid=$3 AND backend_start=$4)""",
                TEST_DB, tag, pid, backend_start):
            await asyncio.sleep(.01)
    return pid


@pytest.mark.asyncio
async def test_old_fenced_startup_does_not_recover_new_instances_running_call(
    clean_database, monkeypatch, record_property,
):
    require_process_owned_database()
    for name, value in {
        "SVI_DATABASE_URL": TEST_URL, "SVI_CONFIG": str(SERVER_ROOT / "config" / "app.yaml"),
        "SVI_INSTANCE_LOCK": "1", "SVI_ENVIRONMENT": "development",
        "SVI_ASR_PROVIDER": "mock", "SVI_LLM_PROVIDER": "mock",
        "WEB_CONCURRENCY": "1", "UVICORN_WORKERS": "1",
    }.items():
        monkeypatch.setenv(name, value)

    nonce = uuid.uuid4().hex[:10]
    tags = {side: f"svi_recovery_{os.getpid()}_{nonce}_{side}" for side in ("A", "B", "C")}
    actor = ContextVar("recovery_test_actor", default=None)
    database_factory = main.Database

    def tagged_database(config):
        side = actor.get()
        assert side in ("A", "B")
        database = database_factory(config)
        database._recovery_test_actor = side

        def identify_connection(adapted, _record):
            # Observability only: name every connection from this private app.
            adapted.run_async(lambda driver: driver.execute(
                "SELECT set_config('application_name', $1, false)", tags[side]))

        event.listen(database.engine.sync_engine, "connect", identify_connection)
        return database

    monkeypatch.setattr(main, "Database", tagged_database)
    a_at_recovery, release_a, a_recovery_ended = asyncio.Event(), asyncio.Event(), asyncio.Event()
    actual_recovery = DatabaseCallRecorder.recover_incomplete
    recovery_returned = {}

    async def gated_recovery(recorder, *, identities=None):
        assert identities is not None, "startup must pass its frozen identities, including an empty tuple"
        side = recorder.db._recovery_test_actor
        if side != "A":
            return await actual_recovery(recorder, identities=identities)
        a_at_recovery.set()
        try:
            await release_a.wait()  # Only a scheduling gate; original recovery follows unchanged.
            recovery_returned["A"] = await actual_recovery(recorder, identities=identities)
            return recovery_returned["A"]
        finally:
            a_recovery_ended.set()

    monkeypatch.setattr(DatabaseCallRecorder, "recover_incomplete", gated_recovery)
    apps = {side: main.create_app(str(SERVER_ROOT / "config" / "app.yaml")) for side in ("A", "B")}
    entered = {side: asyncio.Event() for side in apps}
    stop = {side: asyncio.Event() for side in apps}
    tasks = {}
    release_model, model_entered = asyncio.Event(), asyncio.Event()
    call_task = None
    control = None

    async def run_app(side):
        token = actor.set(side)
        try:
            async with apps[side].router.lifespan_context(apps[side]):
                entered[side].set()
                await stop[side].wait()
        finally:
            actor.reset(token)

    try:
        control = await asyncpg.connect(host=PG_HOST, port=PG_PORT, user=PG_USER,
            password=PG_PASSWORD, database=TEST_DB,
            server_settings={"application_name": tags["C"]})
        assert await control.fetchval("SELECT current_database()") == TEST_DB
        tasks["A"] = asyncio.create_task(run_app("A"))
        await asyncio.wait_for(a_at_recovery.wait(), 8)
        assert not entered["A"].is_set() and not apps["A"].state.instance_fenced
        key = apps["A"].state.config.server.instance_lock_key
        terminated_pid = await terminate_exact_test_owner(control, tags["A"], key)
        # Wait for the REAL watchdog, not a test-written fence flag.
        await wait_until(lambda: apps["A"].state.instance_fenced)
        assert not apps["A"].state.ready

        tasks["B"] = asyncio.create_task(run_app("B"))
        async with asyncio.timeout(8):
            await entered["B"].wait()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=apps["B"]),
                                         base_url="http://recovery-fixture") as health_client:
                await wait_for_readiness_async(health_client)
        assert apps["B"].state.ready and not apps["B"].state.instance_fenced
        assert apps["B"].state.config.server.instance_lock_key == key
        # Registration gives a real tenant parent without needing a token or
        # creating a voice session whose watchdog could obscure this ledger race.
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=apps["B"]),
                                    base_url="http://recovery-fixture") as client:
            response = await client.post("/api/auth/register", json={
                "email": f"recovery-{nonce}@example.invalid", "password": "FixtureOnly123!"})
            assert response.status_code == 201
            user_id = response.json()["id"]
        provider = apps["B"].state.llm
        assert isinstance(provider, ScheduledLlmProvider)
        request = LlmRequest("refine", "synthetic ownership test", {}, {
            "type": "object", "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"], "additionalProperties": False})

        async def controlled_physical_call(request, deadline):
            assert request.call_id  # Production started() committed before dispatch.
            model_entered.set()
            await release_model.wait()
            return LlmResponse(parsed={"ok": True}, provider=provider.provider_id,
                model_id=provider.model_id, prompt_tokens=3, completion_tokens=2)

        monkeypatch.setattr(provider.inner, "generate", controlled_physical_call)
        # Optional session/attempt IDs are legal null in this focused call ledger
        # fixture. This is NOT a completed voice-session or actual-model claim.
        with call_scope(CallScope(user_id=user_id)):
            call_task = asyncio.create_task(provider.generate(request, asyncio.get_running_loop().time() + 10))
        await asyncio.wait_for(model_entered.wait(), 2)
        before = await call_snapshot(control, request.call_id)
        assert before["status"] == "running" and before["ended_at"] is None
        assert before["aggregated_at"] is None
        assert not call_task.done()

        release_a.set()
        await asyncio.wait_for(a_recovery_ended.wait(), 3)
        await wait_until(lambda: entered["A"].is_set() or tasks["A"].done(), seconds=2)
        if tasks["A"].done() and not tasks["A"].cancelled():
            error = tasks["A"].exception()
            assert error is None or (isinstance(error, RuntimeError) and str(error) == "INSTANCE_LOCK_LOST")
        after_old_startup = await call_snapshot(control, request.call_id)
        assert not apps["A"].state.ready and apps["A"].state.instance_fenced
        assert apps["A"].state.unavailable_reason == "INSTANCE_LOCK_LOST"
        assert apps["A"].state.probe_task is None and apps["A"].state.cleanup_task is None
        assert apps["B"].state.ready and not apps["B"].state.instance_fenced
        assert not call_task.done()
        release_model.set()
        answer = await asyncio.wait_for(call_task, 2)
        assert answer.error is None and answer.parsed == {"ok": True}
        after_real_completion = await call_snapshot(control, request.call_id)
        record_property("instance_recovery_observation", json.dumps({
            "only_test_backend_terminated": terminated_pid,
            "a_watchdog_fenced": apps["A"].state.instance_fenced,
            "a_recovery_returned": recovery_returned.get("A"),
            "status_before": before["status"],
            "status_after_old_startup": after_old_startup["status"],
            "error_after_old_startup": after_old_startup["error_code"],
            "status_after_controlled_completion": after_real_completion["status"],
            "tokens_after_controlled_completion": [after_real_completion["prompt_tokens"],
                                                    after_real_completion["completion_tokens"]],
            "physical_provider": "controlled_stub_not_real_model",
        }, ensure_ascii=False))
        # Safety oracle: baseline is expected to FAIL here, but not pre-labelled
        # as reproduced. A safe candidate must preserve the successor's live row.
        assert after_old_startup == before, "fenced old startup modified successor's live call"
        assert after_real_completion["status"] == "succeeded"
        assert after_real_completion["error_code"] is None
        assert (after_real_completion["prompt_tokens"], after_real_completion["completion_tokens"]) == (3, 2)
    finally:
        already_failing = sys.exc_info()[0] is not None
        release_a.set()
        release_model.set()
        for signal in stop.values():
            signal.set()
        if call_task is not None and not call_task.done():
            call_task.cancel()
        names = list(tasks) + (["model"] if call_task is not None else [])
        children = list(tasks.values()) + ([call_task] if call_task is not None else [])
        try:
            async with asyncio.timeout(12):
                outcomes = await asyncio.gather(*children, return_exceptions=True)
        finally:
            if control is not None:
                await control.close()
        if not already_failing:
            for name, outcome in zip(names, outcomes):
                if not isinstance(outcome, BaseException):
                    continue
                expected_owner_abort = name == "A" and (
                    isinstance(outcome, asyncio.CancelledError)
                    or (isinstance(outcome, RuntimeError) and str(outcome) == "INSTANCE_LOCK_LOST"))
                assert expected_owner_abort, f"unexpected private {name} shutdown failure: {type(outcome).__name__}"
            for app in apps.values():
                assert app.state.db.engine.pool.checkedout() == 0
                for field in ("startup_task", "probe_task", "cleanup_task", "llm_prepare_task", "shutdown_task"):
                    task = getattr(app.state, field, None)
                    assert task is None or task.done(), field
            record_property("instance_recovery_cleanup", json.dumps({
                "a_pool_checkedout": apps["A"].state.db.engine.pool.checkedout(),
                "b_pool_checkedout": apps["B"].state.db.engine.pool.checkedout(),
                "owned_tasks_done": True,
            }))
