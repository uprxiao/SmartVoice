"""Root-operated real PG recovery tests for both heartbeat delivery schedules.

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


async def terminate_exact_test_owner(control, tag, key, expected_pid):
    require_process_owned_database()
    assert re.fullmatch(rf"svi_preobserve_{os.getpid()}_[0-9a-f]{{10}}_A", tag)
    assert type(expected_pid) is int and expected_pid > 0
    assert await control.fetchval("SELECT current_database()") == TEST_DB
    unsigned = key % (1 << 64)
    args = (TEST_DB, tag, unsigned >> 32, unsigned & 0xffffffff)
    rows = await control.fetch("""SELECT a.pid,a.backend_start
        FROM pg_stat_activity a
        WHERE a.datname=$1 AND a.application_name=$2 AND a.pid=$5
          AND a.pid<>pg_backend_pid()
          AND EXISTS(SELECT 1 FROM pg_locks l WHERE l.pid=a.pid
            AND l.locktype='advisory' AND l.granted AND l.mode='ExclusiveLock'
            AND l.database=(SELECT oid FROM pg_database WHERE datname=$1)
            AND l.classid::bigint=$3 AND l.objid::bigint=$4 AND l.objsubid=1)""", *args, expected_pid)
    assert len(rows) == 1, "expected exactly one tagged advisory backend; terminating none"
    pid, backend_start = rows[0]["pid"], rows[0]["backend_start"]
    assert pid == expected_pid
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


async def usage_snapshot(control, user_id):
    return [dict(row) for row in await control.fetch("""SELECT date,provider,model_id,purpose,
        calls,successes,failures,unknown_calls,elapsed_ms,processed_audio_samples,
        prompt_tokens_known,completion_tokens_known,
        prompt_tokens_unknown_calls,completion_tokens_unknown_calls
        FROM model_usage_daily WHERE user_id=$1 ORDER BY date,provider,model_id,purpose""", user_id)]


async def advisory_owner_tags(control, key):
    unsigned = key % (1 << 64)
    return list(await control.fetch("""SELECT a.pid,a.application_name FROM pg_locks l
        JOIN pg_stat_activity a ON a.pid=l.pid
        WHERE a.datname=$1 AND l.database=(SELECT oid FROM pg_database WHERE datname=$1)
          AND l.locktype='advisory' AND l.granted AND l.mode='ExclusiveLock'
          AND l.classid::bigint=$2 AND l.objid::bigint=$3 AND l.objsubid=1
        ORDER BY a.pid""", TEST_DB, unsigned >> 32, unsigned & 0xffffffff))


@pytest.mark.asyncio
@pytest.mark.parametrize("delay_reply", [False, True], ids=["original-poll-window", "delayed-true-reply"])
async def test_old_startup_preserves_new_call_before_delayed_true_heartbeat_is_delivered(
    clean_database, monkeypatch, record_property, delay_reply,
):
    """Original poll sleep or controlled reply delay; neither implies frequency.

    Actual PG heartbeat result is retained unchanged. The test additionally
    contains A before readiness/provider creation, so that gate is NOT evidence
    that production prevents a stale owner from publishing ready.
    """
    require_process_owned_database()
    for name, value in {
        "SVI_DATABASE_URL": TEST_URL, "SVI_CONFIG": str(SERVER_ROOT / "config/app.yaml"),
        "SVI_INSTANCE_LOCK": "1", "SVI_ENVIRONMENT": "development",
        "SVI_ASR_PROVIDER": "mock", "SVI_LLM_PROVIDER": "mock",
        "WEB_CONCURRENCY": "1", "UVICORN_WORKERS": "1",
    }.items():
        monkeypatch.setenv(name, value)

    nonce = uuid.uuid4().hex[:10]
    tags = {side: f"svi_preobserve_{os.getpid()}_{nonce}_{side}" for side in ("A", "B", "C")}
    actor = ContextVar("preobserve_test_actor", default=None)
    database_factory = main.Database
    database_close_observations = {}
    trace = []
    origin = asyncio.get_running_loop().time()

    def note(event, **safe_fields):
        trace.append({"event": event, "elapsed_ms": round(
            (asyncio.get_running_loop().time() - origin) * 1000, 3), **safe_fields})

    def tagged_database(config):
        side = actor.get()
        assert side in ("A", "B")
        database = database_factory(config)
        database._preobserve_test_actor = side
        actual_close = database.close

        async def observe_close():
            database_close_observations[side] = {
                "pool_checkedout_before_dispose": database.engine.pool.checkedout(),
                "dispose_completed": False,
            }
            await actual_close()
            database_close_observations[side]["dispose_completed"] = True

        database.close = observe_close

        def identify_connection(adapted, _record):
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
        if recorder.db._preobserve_test_actor != "A":
            return await actual_recovery(recorder, identities=identities)
        a_at_recovery.set()
        note("a_recovery_gate_reached")
        try:
            await release_a.wait()
            note("a_original_recovery_entered")
            recovery_returned["A"] = await actual_recovery(recorder, identities=identities)
            note("a_original_recovery_returned", count=recovery_returned["A"])
            return recovery_returned["A"]
        finally:
            a_recovery_ended.set()

    monkeypatch.setattr(DatabaseCallRecorder, "recover_incomplete", gated_recovery)
    heartbeat_captured, deliver_heartbeat, heartbeat_delivered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    later_real_probe_failed = asyncio.Event()
    actual_alive = main._instance_lock_alive
    observed_heartbeat = {}

    async def delayed_real_heartbeat(connection, key, backend_pid, *, connection_mutex=None):
        side = actor.get()
        try:
            assert side in apps and connection_mutex is apps[side].state.instance_connection_mutex
            answer = await actual_alive(connection, key, backend_pid, connection_mutex=connection_mutex)
        except Exception as error:
            if side == "A":
                note("a_real_heartbeat_error", exception_type=type(error).__name__)
                if heartbeat_captured.is_set():
                    later_real_probe_failed.set()
            raise
        if side == "A" and not heartbeat_captured.is_set():
            assert answer is True, "first real A heartbeat was not successful; schedule not established"
            observed_heartbeat.update(pid=backend_pid, key=key, answer=answer)
            note("a_real_true_heartbeat_committed", backend_pid=backend_pid)
            heartbeat_captured.set()
            # The real function and its two-second timeout already completed.
            # The original-poll parameter adds NO await after the real result;
            # the other parameter delays delivery without replacing its bool.
            if delay_reply:
                await deliver_heartbeat.wait()
            heartbeat_delivered.set()
            note("a_original_true_heartbeat_delivered")
        elif side == "A":
            note("a_next_real_heartbeat_returned", answer=answer)
            if answer is False:
                later_real_probe_failed.set()
        return answer

    monkeypatch.setattr(main, "_instance_lock_alive", delayed_real_heartbeat)
    actual_readiness = main._refresh_readiness
    a_at_readiness, release_a_readiness = asyncio.Event(), asyncio.Event()

    async def contain_stale_readiness(app):
        if app.state.db._preobserve_test_actor == "A":
            note("a_reached_readiness_containment_gate")
            a_at_readiness.set()
            # This gate is never released during the experiment. Actual watcher
            # fencing (or finally's owned parent cancellation) must cancel it.
            await release_a_readiness.wait()
        return await actual_readiness(app)

    monkeypatch.setattr(main, "_refresh_readiness", contain_stale_readiness)
    factory_calls = {"A": [], "B": []}
    for name in ("build_asr", "build_llm"):
        factory = getattr(main, name)

        def observe_factory(config, _factory=factory, _name=name):
            side = actor.get()
            assert side in factory_calls
            factory_calls[side].append(_name)
            return _factory(config)

        monkeypatch.setattr(main, name, observe_factory)

    apps = {side: main.create_app(str(SERVER_ROOT / "config/app.yaml")) for side in ("A", "B")}
    entered = {side: asyncio.Event() for side in apps}
    stop = {side: asyncio.Event() for side in apps}
    tasks = {}
    release_model, model_entered = asyncio.Event(), asyncio.Event()
    call_task, control = None, None
    outcomes = []
    observations = {}

    async def run_app(side):
        token = actor.set(side)
        try:
            async with apps[side].router.lifespan_context(apps[side]):
                note("lifespan_entered", side=side)
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
        await asyncio.wait_for(heartbeat_captured.wait(), 4)
        assert not entered["A"].is_set() and not apps["A"].state.instance_fenced
        assert not deliver_heartbeat.is_set() and observed_heartbeat["answer"] is True
        assert heartbeat_delivered.is_set() is (not delay_reply)
        key = apps["A"].state.config.server.instance_lock_key
        assert key == observed_heartbeat["key"]
        terminated_pid = await terminate_exact_test_owner(control, tags["A"], key, observed_heartbeat["pid"])
        assert terminated_pid == observed_heartbeat["pid"]
        note("a_exact_advisory_backend_terminated", backend_pid=terminated_pid)
        assert not apps["A"].state.instance_fenced

        tasks["B"] = asyncio.create_task(run_app("B"))
        async with asyncio.timeout(8):
            await entered["B"].wait()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=apps["B"]),
                                         base_url="http://preobserve-fixture") as health_client:
                await wait_for_readiness_async(health_client)
        assert apps["B"].state.ready and not apps["B"].state.instance_fenced
        owners = await advisory_owner_tags(control, key)
        assert len(owners) == 1 and owners[0]["application_name"] == tags["B"]
        assert owners[0]["pid"] != terminated_pid
        note("b_is_only_actual_lock_owner_and_ready", backend_pid=owners[0]["pid"])
        assert not apps["A"].state.instance_fenced
        assert heartbeat_delivered.is_set() is (not delay_reply)

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=apps["B"]),
                                    base_url="http://preobserve-fixture") as client:
            response = await client.post("/api/auth/register", json={
                "email": f"preobserve-{nonce}@example.invalid", "password": "FixtureOnly123!"})
            assert response.status_code == 201
            user_id = response.json()["id"]
        provider = apps["B"].state.llm
        assert isinstance(provider, ScheduledLlmProvider)
        request = LlmRequest("refine", "synthetic pre-observation ownership test", {}, {
            "type": "object", "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"], "additionalProperties": False})

        async def controlled_physical_call(request, deadline):
            assert request.call_id
            note("b_physical_stub_entered_after_started_commit")
            model_entered.set()
            await release_model.wait()
            return LlmResponse(parsed={"ok": True}, provider=provider.provider_id,
                model_id=provider.model_id, prompt_tokens=3, completion_tokens=2)

        monkeypatch.setattr(provider.inner, "generate", controlled_physical_call)
        with call_scope(CallScope(user_id=user_id)):
            call_task = asyncio.create_task(provider.generate(request, asyncio.get_running_loop().time() + 10))
        await asyncio.wait_for(model_entered.wait(), 2)
        before = await call_snapshot(control, request.call_id)
        usage_before = await usage_snapshot(control, user_id)
        assert before["status"] == "running" and before["ended_at"] is None
        assert before["aggregated_at"] is None and usage_before == []
        assert not call_task.done()
        assert heartbeat_delivered.is_set() is (not delay_reply)
        assert not apps["A"].state.instance_fenced
        note("b_running_baseline_read_back")

        release_a.set()
        await asyncio.wait_for(a_recovery_ended.wait(), 3)
        # Current child-only candidate reaches readiness. A future safe version
        # may abort first; record that separately without demanding unsafe progress.
        await wait_until(lambda: a_at_readiness.is_set() or apps["A"].state.startup_task.done(), seconds=2)
        after_old_recovery = await call_snapshot(control, request.call_id)
        usage_after_old_recovery = await usage_snapshot(control, user_id)
        note("b_rows_read_after_a_recovery", heartbeat_delivered=heartbeat_delivered.is_set())
        assert heartbeat_delivered.is_set() is (not delay_reply)
        observations["a_fenced_at_data_observation"] = apps["A"].state.instance_fenced
        observations["a_reached_readiness_path"] = a_at_readiness.is_set()
        # These are harness containment assertions, not production-ready safety.
        assert not release_a_readiness.is_set()
        assert not apps["A"].state.ready
        if not apps["A"].state.instance_fenced:
            # The DB-complete barrier permits listening while this fixture's
            # first readiness gate still holds. Prove zero admission through
            # public routes instead of assuming lifespan has not yielded.
            counts_sql = """SELECT (SELECT count(*) FROM sessions) AS sessions,
                (SELECT count(*) FROM session_attempts) AS attempts,
                (SELECT count(*) FROM operation_requests) AS operations"""
            request_id = str(uuid.uuid4())
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=apps["A"]),
                                         base_url="http://preobserve-fixture") as a_client:
                health = await a_client.get("/health/ready")
                assert health.status_code == 503
                assert health.json()["reason_code"] == "STARTING"
                counts_before = dict(await control.fetchrow(counts_sql))
                denied = await a_client.post("/api/v1/sessions",
                    headers={"Idempotency-Key": request_id},
                    json={"client_request_id": request_id, "mode": "dictation", "learning_enabled": True})
                assert denied.status_code == 503
                error = denied.json()["error"]
                assert error["code"] == "SERVICE_UNAVAILABLE" and error["retryable"] is True
                assert error["request_id"] == denied.headers["x-request-id"]
            messages = []

            async def receive_connect():
                return {"type": "websocket.connect"}

            async def capture_denial(message):
                messages.append(message)

            # Readiness rejects before authentication/tenant lookup. A missing
            # token's 401 or a missing session's 404 is not accepted as proof.
            await apps["A"]({"type": "websocket", "asgi": {"version": "3.0"},
                "scheme": "ws", "path": f"/ws/v1/sessions/{request_id}/audio",
                "query_string": b"", "root_path": "", "headers": [],
                "client": ("127.0.0.1", 1234), "server": ("preobserve-fixture", 80),
                "subprotocols": [], "extensions": {"websocket.http.response": {}}},
                receive_connect, capture_denial)
            assert [message["type"] for message in messages] == [
                "websocket.http.response.start", "websocket.http.response.body"]
            assert messages[0]["status"] == 503
            ws_error = json.loads(messages[1]["body"])["error"]
            assert ws_error["code"] == "SERVICE_UNAVAILABLE" and ws_error["retryable"] is True
            assert dict(await control.fetchrow(counts_sql)) == counts_before
            assert await call_snapshot(control, request.call_id) == after_old_recovery
            assert await usage_snapshot(control, user_id) == usage_after_old_recovery
            # These observations consume the original physical call's 10s
            # deadline; no scheduling gate or extra call budget is introduced.
            owners = await advisory_owner_tags(control, key)
            assert len(owners) == 1 and owners[0]["application_name"] == tags["B"]
            assert owners[0]["pid"] != terminated_pid
        assert factory_calls["A"] == []
        assert apps["A"].state.asr is apps["A"].state.llm is apps["A"].state.sessions is None
        assert apps["A"].state.probe_task is apps["A"].state.cleanup_task is None
        assert apps["B"].state.ready and not apps["B"].state.instance_fenced
        assert not call_task.done()

        # Complete the real wrapper's controlled call inside its unchanged 10s
        # deadline before waiting for another ordinary heartbeat interval.
        release_model.set()
        answer = await asyncio.wait_for(call_task, 2)
        assert answer.error is None and answer.parsed == {"ok": True}
        after_completion = await call_snapshot(control, request.call_id)
        usage_after_completion = await usage_snapshot(control, user_id)
        note("b_controlled_physical_call_completed")
        deliver_heartbeat.set()
        if not observations["a_fenced_at_data_observation"]:
            await asyncio.wait_for(heartbeat_delivered.wait(), 2)
            await asyncio.wait_for(later_real_probe_failed.wait(), 5)
            await wait_until(lambda: apps["A"].state.instance_fenced, seconds=2)
            note("a_actual_watcher_observed_loss_after_later_real_probe")
        else:
            # A future safe version may independently fence/abort before the
            # delayed result is delivered. Do not require it to make progress
            # while unsafe, or falsely attribute that fence to this watcher.
            note("a_already_fenced_before_heartbeat_delivery")
        await wait_until(lambda: entered["A"].is_set() or tasks["A"].done(), seconds=3)
        assert not apps["A"].state.ready and apps["A"].state.unavailable_reason == "INSTANCE_LOCK_LOST"
        assert factory_calls["A"] == []
        assert apps["A"].state.asr is apps["A"].state.llm is apps["A"].state.sessions is None
        assert apps["A"].state.probe_task is apps["A"].state.cleanup_task is None
        assert apps["B"].state.ready and not apps["B"].state.instance_fenced

        def safe_usage(rows):
            # Exact projection fields, with no user ID/configuration/credentials.
            return [{**row, "date": row["date"].isoformat()} for row in rows]

        observations.update({
            "only_test_backend_terminated": terminated_pid,
            "actual_successful_heartbeat_result": observed_heartbeat["answer"],
            "monitor_gate": ("delay_delivery_of_actual_true_result_after_commit" if delay_reply
                             else "no_await_added_after_actual_true_result_original_poll_sleep"),
            "a_recovery_returned": recovery_returned.get("A"),
            "a_readiness_contained_by_test_not_product_proof": True,
            "a_factory_calls": factory_calls["A"],
            "status_before": before["status"],
            "status_after_old_recovery": after_old_recovery["status"],
            "error_after_old_recovery": after_old_recovery["error_code"],
            "status_after_controlled_completion": after_completion["status"],
            "tokens_after_controlled_completion": [after_completion["prompt_tokens"], after_completion["completion_tokens"]],
            "usage_before": safe_usage(usage_before),
            "usage_after_old_recovery": safe_usage(usage_after_old_recovery),
            "usage_after_completion": safe_usage(usage_after_completion),
            "physical_provider": "controlled_stub_not_real_model",
            "original_true_heartbeat_delivered": heartbeat_delivered.is_set(),
            "later_real_probe_failed": later_real_probe_failed.is_set(),
            "a_eventually_fenced": apps["A"].state.instance_fenced,
        })
        record_property("preobserve_recovery_observation", json.dumps(observations, ensure_ascii=False))
        record_property("preobserve_schedule", json.dumps(trace, ensure_ascii=False))

        failures = []
        if after_old_recovery != before:
            failures.append("old startup modified successor running call before watcher observed loss")
        if usage_after_old_recovery != usage_before:
            failures.append("old startup prematurely aggregated successor physical call")
        if (after_completion["status"], after_completion["error_code"],
                after_completion["prompt_tokens"], after_completion["completion_tokens"]) != ("succeeded", None, 3, 2):
            failures.append("successor normal physical completion did not persist its true outcome/use")
        expected_counts = {"calls": 1, "successes": 1, "failures": 0, "unknown_calls": 0,
            "elapsed_ms": after_completion["elapsed_ms"], "processed_audio_samples": 0,
            "prompt_tokens_known": 3, "completion_tokens_known": 2,
            "prompt_tokens_unknown_calls": 0, "completion_tokens_unknown_calls": 0}
        if (len(usage_after_completion) != 1 or any(
                usage_after_completion[0][name] != value for name, value in expected_counts.items())):
            failures.append("successor daily projection does not match exactly one successful known-use call")
        assert not failures, "; ".join(failures)
    finally:
        already_failing = sys.exc_info()[0] is not None
        release_a.set()
        release_model.set()
        deliver_heartbeat.set()
        # Never release A's readiness containment gate, even on a failed oracle.
        # Cancel only this fixture's pre-yield parents when they cannot see stop.
        for side, signal in stop.items():
            signal.set()
            parent = tasks.get(side)
            if parent is not None and not entered[side].is_set() and not parent.done() and not parent.cancelling():
                parent.cancel()
        if call_task is not None and not call_task.done() and not call_task.cancelling():
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
        cleanup = {"test_readiness_gate_released": release_a_readiness.is_set(),
                   "database_close": database_close_observations}
        for side in tasks:
            app = apps[side]
            database = getattr(app.state, "db", None)
            cleanup[f"{side.lower()}_pool_checkedout"] = (
                database.engine.pool.checkedout() if database is not None else None)
            pending = [field for field in ("startup_task", "probe_task", "cleanup_task", "llm_prepare_task", "shutdown_task")
                if getattr(app.state, field, None) is not None and not getattr(app.state, field).done()]
            cleanup[f"{side.lower()}_pending_owned_tasks"] = pending
            if not already_failing:
                assert cleanup[f"{side.lower()}_pool_checkedout"] == 0
                assert pending == []
                assert database_close_observations.get(side) == {
                    "pool_checkedout_before_dispose": 0, "dispose_completed": True,
                }
        record_property("preobserve_recovery_cleanup", json.dumps(cleanup, ensure_ascii=False))


