"""Additional real-PG shutdown parameters; physical inference is a declared Mock.

The budget case waits the actual 30 seconds and does not edit any runtime clock,
configuration budget, recovery row or result. The loss case terminates only this
process-owned test DB's randomly tagged advisory owner via the existing exact
PID/backend generation/key helper. Neither case is TCP/SIGTERM/model acceptance.
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid

import asyncpg
import httpx
import pytest
from sqlalchemy import event

from app import main
from app.providers.asr.mock import MockAsrSession
from app.providers.llm.mock import MockLlmProvider
from app.services.inference_context import current_scope
from tests.conftest import PG_HOST, PG_PASSWORD, PG_PORT, PG_USER, SERVER_ROOT, TEST_DB, TEST_URL, wait_for_readiness_async
from tests.integration.test_instance_recovery_loss import terminate_exact_test_owner
from tests.integration.test_normal_shutdown_contract import (
    WsProbe, create_session, register, require_owned_database, safe_status, snapshot,
)


async def wait_until(predicate, seconds=5):
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(.005)


async def ledger_snapshot(control, session_id):
    calls = await control.fetch("""SELECT id,user_id,purpose,status,error_code,
        ended_at,aggregated_at,prompt_tokens,completion_tokens,elapsed_ms
        FROM model_calls WHERE session_id=$1 ORDER BY started_at,id""", session_id)
    # Sum the real date buckets so a run spanning Shanghai midnight is valid.
    daily = await control.fetch("""SELECT purpose,sum(calls)::bigint AS calls,
        sum(successes)::bigint AS successes,sum(failures)::bigint AS failures,
        sum(unknown_calls)::bigint AS unknown_calls,
        sum(prompt_tokens_known)::bigint AS prompt_tokens_known,
        sum(completion_tokens_known)::bigint AS completion_tokens_known,
        sum(prompt_tokens_unknown_calls)::bigint AS prompt_tokens_unknown_calls,
        sum(completion_tokens_unknown_calls)::bigint AS completion_tokens_unknown_calls
        FROM model_usage_daily WHERE user_id=(SELECT user_id FROM sessions WHERE id=$1)
        GROUP BY purpose ORDER BY purpose""", session_id)
    return [dict(row) for row in calls], [dict(row) for row in daily]


def safe_calls(rows):
    return [{name: row[name] for name in (
        "id", "purpose", "status", "error_code", "prompt_tokens", "completion_tokens", "elapsed_ms")}
        | {"ended": row["ended_at"] is not None, "aggregated": row["aggregated_at"] is not None}
        for row in rows]


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["original_budget_fallback", "normal_drain_then_lock_loss"])
async def test_shutdown_preserves_raw_fallback_or_upgrades_on_real_lock_loss(
    clean_database, monkeypatch, record_property, scenario,
):
    require_owned_database()
    for name, value in {
        "SVI_DATABASE_URL": TEST_URL, "SVI_CONFIG": str(SERVER_ROOT / "config/app.yaml"),
        "SVI_INSTANCE_LOCK": "1", "SVI_ENVIRONMENT": "development", "SVI_ASR_PROVIDER": "mock",
        "SVI_LLM_PROVIDER": "mock", "WEB_CONCURRENCY": "1", "UVICORN_WORKERS": "1",
    }.items():
        monkeypatch.setenv(name, value)
    loop = asyncio.get_running_loop()
    origin = loop.time()
    target = {"session_id": None}
    trace, physical_calls = [], []
    observed = {"scenario": scenario, "gate_cancel_count": 0, "gate_returned": False,
                "db_disposed": False, "pool_checkedout_before_dispose": None}
    started, stop, shutdown_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    physical_gate_entered, physical_gate_release = asyncio.Event(), asyncio.Event()
    physical_gate_cancelled, physical_gate_ended = asyncio.Event(), asyncio.Event()
    raw_text = "嗯那个，帮我加一个按钮，不要修改数据库。"

    def note(name, **fields):
        trace.append({"event": name, "elapsed_ms": round((loop.time()-origin)*1000, 3), **fields})

    # Same exact helper identity grammar as the already executed instance tests.
    nonce = uuid.uuid4().hex[:10]
    app_tag = f"svi_recovery_{os.getpid()}_{nonce}_A"
    control_tag = f"svi_recovery_{os.getpid()}_{nonce}_C"
    actual_database = main.Database

    def tagged_database(config):
        database = actual_database(config)

        def identify_connection(adapted, _record):
            adapted.run_async(lambda driver: driver.execute(
                "SELECT set_config('application_name', $1, false)", app_tag))

        event.listen(database.engine.sync_engine, "connect", identify_connection)
        return database

    monkeypatch.setattr(main, "Database", tagged_database)
    actual_finish, actual_generate = MockAsrSession.finish, MockLlmProvider.generate

    async def original_budget_work(label, deadline):
        # Real elapsed work, less than the unmodified per-call 10s limit. Three
        # such stages put audit at approximately end+25.5s, leaving <10s of the
        # original pipeline budget. An overloaded harness fails, never rebases.
        note(label + "_entered", remaining_ms=round((deadline-loop.time())*1000, 3))
        await asyncio.sleep(8.5)
        assert loop.time() < deadline, "fixture work exceeded the original physical deadline"
        note(label + "_returned")

    async def gated_finish(stream, total_samples, deadline):
        if stream.options.session_id == target["session_id"]:
            assert total_samples == stream.samples == 4000 and stream.options.attempt_no == 1
            assert stream.text == raw_text
            if scenario == "original_budget_fallback":
                await original_budget_work("asr_finish", deadline)
        return await actual_finish(stream, total_samples, deadline)

    async def gated_generate(provider, request, deadline):
        scope = current_scope.get()
        if scope is None or scope.session_id != target["session_id"]:
            return await actual_generate(provider, request, deadline)
        assert request.call_id and scope.attempt_no == 1
        call = {"id": request.call_id, "purpose": request.purpose, "deadline": deadline,
                "entered_at": loop.time()}
        physical_calls.append(call)
        note("physical_llm_entered", purpose=request.purpose,
             remaining_ms=round((deadline-loop.time())*1000, 3))
        if scenario == "original_budget_fallback" and request.purpose == "refine":
            refine_no = sum(row["purpose"] == "refine" for row in physical_calls)
            assert refine_no in (1, 2), "the single structural retry allowance was exceeded"
            await original_budget_work(f"refine_{refine_no}", deadline)
            response = await actual_generate(provider, request, deadline)
            if refine_no == 1:
                # Explicit physical fixture fault. Formal validate_response and
                # RetryAllowance must reject/retry it; no business function is
                # monkeypatched and no synthetic DB ledger row is inserted.
                response.parsed = {"candidate_text": raw_text}  # Missing required edits.
                note("physical_schema_fault_returned")
                return response
        expected_purpose = "refine"
        assert request.purpose == expected_purpose
        observed["physical_deadline"] = deadline
        physical_gate_entered.set()
        try:
            await physical_gate_release.wait()
            observed["gate_returned"] = True
            return await actual_generate(provider, request, deadline)
        except asyncio.CancelledError:
            observed["gate_cancel_count"] += 1
            observed["physical_task_cancelling"] = asyncio.current_task().cancelling()
            observed["physical_cancel_at"] = loop.time()
            note("physical_llm_cancelled")
            physical_gate_cancelled.set()
            raise
        finally:
            physical_gate_ended.set()

    monkeypatch.setattr(MockAsrSession, "finish", gated_finish)
    monkeypatch.setattr(MockLlmProvider, "generate", gated_generate)
    actual_close = main._close_instance_resources
    shutdown_refs = {}

    async def observe_shutdown(app, lock_conn, lock_task):
        note("normal_shutdown_entered", fenced=app.state.instance_fenced)
        shutdown_refs["lock_task"] = lock_task
        shutdown_entered.set()
        return await actual_close(app, lock_conn, lock_task)

    monkeypatch.setattr(main, "_close_instance_resources", observe_shutdown)
    app = main.create_app(str(SERVER_ROOT / "config/app.yaml"))

    async def lifespan_owner():
        async with app.router.lifespan_context(app):
            started.set()
            await stop.wait()  # Normal exit first in both parameters; not parent cancellation.

    parent = asyncio.create_task(lifespan_owner())
    control = probe = None
    runtime = None
    saved_runtime_deadline = None
    try:
        async with asyncio.timeout(8):
            await started.wait()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://shutdown-boundary") as health_client:
                await wait_for_readiness_async(health_client)
        assert app.state.ready and not app.state.instance_fenced
        assert app.state.config.pipeline.stop_to_result_budget_ms == 30000
        assert app.state.config.asr.finish_timeout_ms == app.state.config.llm.call_timeout_ms == 10000
        assert app.state.config.llm.automatic_retries_per_attempt == 1
        actual_db_close = app.state.db.close
        actual_llm_close = app.state.llm.close
        scheduler = app.state.llm.scheduler

        async def observe_db_close():
            observed["pool_checkedout_before_dispose"] = app.state.db.engine.pool.checkedout()
            await actual_db_close()
            observed["db_disposed"] = True

        async def observe_llm_close():
            # Check the physical service permits before provider.close can hide
            # a still-running consumer by cancelling it as final cleanup.
            observed["scheduler_before_close"] = scheduler.snapshot()
            observed["physical_gate_ended_before_provider_close"] = physical_gate_ended.is_set()
            await actual_llm_close()
            observed["scheduler_after_close"] = scheduler.snapshot()
            observed["scheduler_workers_after_close"] = len(scheduler.workers)

        monkeypatch.setattr(app.state.db, "close", observe_db_close)
        monkeypatch.setattr(app.state.llm, "close", observe_llm_close)
        control = await asyncpg.connect(host=PG_HOST, port=PG_PORT, user=PG_USER,
            password=PG_PASSWORD, database=TEST_DB, server_settings={"application_name": control_tag})
        assert await control.fetchval("SELECT current_database()") == TEST_DB
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://shutdown-boundary") as http:
            headers = await register(http, nonce)
            response = await create_session(http, headers)
            assert response.status_code == 201, safe_status(response)
            session_id = target["session_id"] = response.json()["session_id"]
            probe = WsProbe(app, headers, session_id)
            await probe.start()
            await probe.end()
            runtime = app.state.sessions.runtimes[session_id]
            saved_runtime_deadline = runtime.deadline
            saved_accepted_at = runtime.end_accepted_at
            assert runtime.ended and runtime.processing_accepted
            assert saved_runtime_deadline - saved_accepted_at == pytest.approx(30)
            probe.drain_task = asyncio.create_task(probe.drain())
            async with asyncio.timeout(29):
                await physical_gate_entered.wait()
            # The raw was persisted by the genuine ASR->refining transaction.
            before = await snapshot(control, session_id)
            persisted_raw = await control.fetchval(
                "SELECT raw_text FROM session_attempts WHERE session_id=$1 AND attempt_no=1", session_id)
            assert before["session"]["state"] == "refining" and before["session"]["audio_complete"]
            assert persisted_raw == raw_text and not before["results"]
            assert runtime.raw_text == persisted_raw
            observed["remaining_at_normal_stop_ms"] = round((saved_runtime_deadline-loop.time())*1000, 3)
            if scenario == "original_budget_fallback":
                assert [row["purpose"] for row in physical_calls] == ["refine", "refine"]
                assert 0 < saved_runtime_deadline-loop.time() < 6
                # A provider may reserve existing outcome-persistence time
                # inside the original total budget. It need not consume all 30s.
                assert loop.time() < observed["physical_deadline"] <= saved_runtime_deadline
                assert observed["physical_deadline"] - physical_calls[-1]["entered_at"] <= 10
                observed["physical_to_total_gap_ms"] = round(
                    (saved_runtime_deadline-observed["physical_deadline"])*1000, 3)
            stop.set()
            async with asyncio.timeout(3):
                await shutdown_entered.wait()
                await wait_until(lambda: not app.state.ready and app.state.sessions._close_task is not None)
            close_owner = app.state.sessions._close_task
            shutdown_owner = app.state.shutdown_task
            assert not close_owner.done() and not app.state.instance_fenced
            assert shutdown_refs["lock_task"] is not None and not shutdown_refs["lock_task"].done()
            assert runtime.deadline == saved_runtime_deadline and runtime.end_accepted_at == saved_accepted_at
            ready = await http.get("/health/ready")
            assert ready.status_code == 503
            if scenario == "normal_drain_then_lock_loss":
                # Helper rechecks actual DB, nonce tag, PID, backend_start and
                # precise advisory key in the terminating statement itself.
                pid = await terminate_exact_test_owner(control, app_tag, app.state.config.server.instance_lock_key)
                note("exact_test_advisory_backend_terminated", pid=pid)
                await wait_until(lambda: app.state.instance_fenced, seconds=5)
                note("real_watcher_fenced")
                assert not app.state.ready and app.state.unavailable_reason == "INSTANCE_LOCK_LOST"
                assert app.state.sessions._close_task is close_owner
                assert app.state.shutdown_task is shutdown_owner
                async with asyncio.timeout(3):
                    await physical_gate_cancelled.wait()
                assert observed["physical_cancel_at"] < observed["physical_deadline"]
                assert loop.time() < observed["physical_deadline"], "harness reached the permit check too late"
                observed["scheduler_immediately_after_fence_cancel"] = scheduler.snapshot()
                assert observed["scheduler_immediately_after_fence_cancel"]["running"] == 1
            # Do not release the held model. Only the original budget or actual
            # lost-lock fence may cancel it on the passing path.
            async with asyncio.timeout(36):
                await asyncio.gather(probe.drain_task, parent)
            after = await snapshot(control, session_id)
            calls, daily = await ledger_snapshot(control, session_id)
            rows = await control.fetch("""SELECT raw_text,refined_text,review_required,review_reason
                FROM results WHERE session_id=$1 ORDER BY attempt_no,id""", session_id)
            result_texts = [dict(row) for row in rows]
            observed["after"] = {"state": after["session"]["state"], "error": after["session"]["error_code"],
                "attempt_state": after["attempt"]["state"], "results": len(rows),
                "review_reasons": [row["review_reason"] for row in rows],
                "ws_types": [item["type"] for item in probe.events], "ws_close": probe.close_code,
                "model_calls": safe_calls(calls), "model_daily": daily}
            observed["elapsed_since_end_ms"] = round((loop.time()-saved_accepted_at)*1000, 3)
            observed["instance_fenced"] = app.state.instance_fenced
            record_property("shutdown_boundary_observation", json.dumps(observed, sort_keys=True))
            record_property("shutdown_boundary_trace", json.dumps(trace, sort_keys=True))

            assert runtime.deadline == saved_runtime_deadline and runtime.end_accepted_at == saved_accepted_at
            assert not observed["gate_returned"] and observed["gate_cancel_count"] == 1
            assert physical_gate_ended.is_set()
            assert app.state.sessions._close_task is close_owner and close_owner.done() and not close_owner.cancelled()
            assert app.state.shutdown_task is shutdown_owner and shutdown_owner.done() and not shutdown_owner.cancelled()
            assert parent.done() and not parent.cancelled() and probe.task.done() and not probe.task.cancelled()
            if scenario == "original_budget_fallback":
                # Fail loudly if normal close wins this race by discarding raw.
                # 'Any terminal state' is expressly not a permitted oracle.
                assert not app.state.instance_fenced
                assert observed["physical_cancel_at"] >= observed["physical_deadline"]
                assert after["session"]["state"] == after["attempt"]["state"] == "ready"
                assert after["session"]["error_code"] is None
                assert len(result_texts) == 1
                assert result_texts[0]["raw_text"] == result_texts[0]["refined_text"] == raw_text
                assert result_texts[0]["review_required"] is True
                assert result_texts[0]["review_reason"] in ("budget_exceeded", "llm_timeout")
                assert after["results"][0]["stop_to_result_ms"] <= 30000
                assert sum(row["type"] == "result.ready" for row in probe.events) == 1
                assert not any(row["type"] == "error" for row in probe.events) and probe.close_code == 1000
                assert after["contribution"] == {"outcome": "ready", "has_result": True,
                    "successful_audio_samples": 4000, "received_samples": 4000}
                assert [(row["purpose"], row["status"]) for row in calls] == [
                    ("refine", "failed"), ("refine", "unknown")]
                assert calls[0]["error_code"] == "invalid_output"
                # No explicit cancellation preceded this physical deadline;
                # expiration must not impersonate an interrupted client.
                assert calls[1]["error_code"] == "timeout"
                assert [(row["purpose"], row["calls"], row["successes"], row["failures"], row["unknown_calls"])
                    for row in daily] == [("refine", 2, 0, 2, 1)]
            else:
                assert app.state.instance_fenced and app.state.unavailable_reason == "INSTANCE_LOCK_LOST"
                assert observed["physical_task_cancelling"] == 1
                assert after["session"]["state"] == after["attempt"]["state"] == "failed"
                assert after["session"]["error_code"] == after["attempt"]["error_code"] == "INTERNAL_ERROR"
                assert not result_texts and not any(row["type"] == "result.ready" for row in probe.events)
                assert probe.close_code == 1011
                assert any(row["type"] == "error" and row["error"]["code"] == "INTERNAL_ERROR" for row in probe.events)
                assert after["contribution"] == {"outcome": "failed", "has_result": False,
                    "successful_audio_samples": 0, "received_samples": 4000}
                assert [(row["purpose"], row["status"], row["error_code"]) for row in calls] == [
                    ("refine", "unknown", "CLIENT_INTERRUPTED")]
                assert [(row["purpose"], row["calls"], row["successes"], row["failures"], row["unknown_calls"])
                    for row in daily] == [("refine", 1, 0, 1, 1)]
            assert {row["id"] for row in calls} == {row["id"] for row in physical_calls}
            assert all(row["ended_at"] is not None and row["aggregated_at"] is not None for row in calls)
            assert all(row["prompt_tokens"] is None and row["completion_tokens"] is None for row in calls)
            assert all(row["prompt_tokens_known"] == row["completion_tokens_known"] == 0
                and row["prompt_tokens_unknown_calls"] == row["completion_tokens_unknown_calls"] == row["calls"]
                for row in daily)
            assert observed["scheduler_before_close"] == {"running": 0, "high_waiting": 0, "low_waiting": 0}
            assert observed["physical_gate_ended_before_provider_close"]
            assert observed["scheduler_after_close"] == observed["scheduler_before_close"]
            assert observed["scheduler_workers_after_close"] == 0
            assert not runtime.audio and runtime.asr is None
            assert not app.state.sessions.runtimes and not app.state.sessions.background_tasks
            assert not app.state.active_requests and not app.state.correction_requests and not app.state.background_tasks
            assert observed["pool_checkedout_before_dispose"] == 0 and observed["db_disposed"]
            # All actual owners have ended. Allow a later loop turn and verify
            # the permanent result/call identities cannot acquire a late row.
            await asyncio.sleep(.05)
            assert await snapshot(control, session_id) == after
            assert await ledger_snapshot(control, session_id) == (calls, daily)
            key = app.state.config.server.instance_lock_key % (1 << 64)
            assert await control.fetchval("""SELECT count(*) FROM pg_locks WHERE locktype='advisory'
                AND database=(SELECT oid FROM pg_database WHERE datname=current_database())
                AND classid::bigint=$1 AND objid::bigint=$2 AND objsubid=1""", key >> 32, key & 0xffffffff) == 0
    finally:
        # Failure cleanup only; releasing this gate is never a passed scenario.
        physical_gate_release.set()
        stop.set()
        owned = [parent] + ([task for task in (probe.task, probe.drain_task) if task] if probe else [])
        pending = [task for task in owned if not task.done()]
        if pending:
            _, still_pending = await asyncio.wait(pending, timeout=36)
            for task in still_pending:
                if not task.cancelling():
                    task.cancel()
            if still_pending:
                _, remaining = await asyncio.wait(still_pending, timeout=4)
                record_property("shutdown_boundary_cleanup_pending", len(remaining))
                assert not remaining, "owned cleanup did not finish; a test failure, never a pass"
        for task in owned:
            if task.done() and not task.cancelled():
                task.exception()
        if control is not None:
            await control.close()
        record_property("shutdown_boundary_final", json.dumps({
            "owned_tasks_done": all(task.done() for task in owned),
            "parent_cancelled": parent.cancelled(), "db_disposed": observed["db_disposed"],
            "pool_checkedout_before_dispose": observed["pool_checkedout_before_dispose"],
        }, sort_keys=True))
        record_property("shutdown_boundary_final_trace", json.dumps(trace, sort_keys=True))
