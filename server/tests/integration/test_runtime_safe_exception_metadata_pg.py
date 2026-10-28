"""Prepared-only diagnostic test template; not a historical CAP13 reproduction.

Original API/ASGI WS/real owned PostgreSQL + declared physical Mock models.
One exact result-transaction CAST fault, no state/result/ledger UPDATE or fake reply.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid

import httpx
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from tests.conftest import auth_headers, register_and_login
from tests.integration.test_capacity_admission_atomicity import atomic_client  # noqa: F401
from tests.integration.test_instance_recovery_loss import require_process_owned_database
from tests.integration.test_normal_shutdown_contract import WsProbe
from tests.integration.test_voice_cancel_boundaries import create, facts, settle, until_fact


LOGGER = "app.services.safe_exception_metadata"


def assert_scalar_tree_without_needles(value, needles):
    # Inspect LogRecord msg/args/extras without formatting arbitrary objects.
    if type(value) is str:
        assert not any(needle in value for needle in needles), "PRIVATE_CANARY_IN_LOG_SCALAR"
    elif value is None or type(value) in (int, float, bool):
        return
    elif type(value) in (tuple, list):
        for item in value:
            assert_scalar_tree_without_needles(item, needles)
    elif type(value) is dict:
        for key, item in value.items():
            assert type(key) is str, "NON_SCALAR_LOG_MAPPING_KEY"
            assert_scalar_tree_without_needles(key, needles)
            assert_scalar_tree_without_needles(item, needles)
    else:
        assert False, "NON_SCALAR_OR_EXCEPTION_FRAME_IN_LOGRECORD"


def test_owned_pg_result_error_logs_only_safe_metadata_and_preserves_behavior(
    atomic_client, monkeypatch, caplog, record_property,
):
    require_process_owned_database()
    caplog.set_level(logging.WARNING)
    passwords = ["SqlPrivacyPass_" + uuid.uuid4().hex + "!" for _ in range(2)]
    pairs = [register_and_login(atomic_client,
        email="safe-sql-" + uuid.uuid4().hex + "@example.invalid", password=password)
        for password in passwords]
    headers = [auth_headers(pair) for pair in pairs]
    users = [pair["user"]["id"] for pair in pairs]
    sql_marker = "SVI_SQL_BOUND_" + uuid.uuid4().hex
    raw_by_user = {user: "嗯，SVI_Raw_" + uuid.uuid4().hex + "。" for user in users}
    needles = [sql_marker, *passwords, *raw_by_user.values(),
        *(pair[name] for pair in pairs for name in ("access_token", "refresh_token"))]

    async def scenario():
        app, manager = atomic_client.app, atomic_client.app.state.sessions
        target, observed, wires = {}, {"fault_count": 0, "dispatches": [], "asr_opens": []}, []
        entered, release = asyncio.Event(), asyncio.Event()
        original_locked = manager._locked_session
        original_open = manager.asr_provider.open
        original_generate = app.state.llm.inner.generate
        original_close = app.state.db.close

        async def close_observed():
            # Original lifespan remains responsible for disposal/fixture teardown.
            checked_out = app.state.db.engine.pool.checkedout()
            await original_close()
            record_property("safe_metadata_owned_db_close",
                json.dumps({"checkedout_before_dispose": checked_out}))
            assert checked_out == 0

        async def opened(options):
            stream = await original_open(options)
            # Declared original Mock data only; no product runtime.raw assignment.
            stream.text = raw_by_user[options.user_id]
            observed["asr_opens"].append((options.session_id, options.attempt_no))
            return stream

        async def generated(request, deadline):
            from app.services.inference_context import current_scope
            scope = current_scope.get()
            assert scope is not None
            observed["dispatches"].append((scope.session_id, scope.attempt_no,
                request.purpose, request.call_id))
            return await original_generate(request, deadline)

        async def locked(db, user_id, session_id):
            runtime = target.get("runtime")
            selected = (runtime is not None and user_id == users[0]
                and session_id == target["sid"] and runtime.attempt_no == 1
                and asyncio.current_task() is runtime.task
                and runtime.state == "refining" and observed["fault_count"] == 0)
            if selected:
                observed["fault_count"] += 1
                entered.set()
                # Test-controlled result-boundary gate only. Original runtime
                # deadline remains active; original session has no result yet.
                await release.wait()
                assert asyncio.get_running_loop().time() < runtime.deadline
                try:
                    # Real bound-parameter PostgreSQL error, not a constructed
                    # SQLAlchemyError or mock DB return. Original rollback follows.
                    await db.execute(text("SELECT CAST(CAST(:marker AS text) AS integer)"), {"marker": sql_marker})
                except SQLAlchemyError as error:
                    # Test-only negative-control evidence: do not log/retain the
                    # error, SQL, params or driver message. No str/repr/args used.
                    params = error.params
                    observed["bound_marker_present"] = (
                        sql_marker in params.values() if type(params) is dict
                        else sql_marker in params if type(params) in (tuple, list) else False)
                    driver = getattr(error.orig, "__cause__", None)
                    message = getattr(driver, "message", None)
                    observed["real_driver_message_contains_marker"] = (
                        type(message) is str and sql_marker in message)
                    observed["real_driver_sqlstate"] = getattr(driver, "sqlstate", None)
                    observed["exception_type"] = type(error).__name__
                    del params, driver, message
                    raise  # The SAME exception must reach original _process catch.
                assert False, "DECLARED_REAL_PG_FAULT_DID_NOT_FAIL"
            return await original_locked(db, user_id, session_id)

        monkeypatch.setattr(app.state.db, "close", close_observed)
        monkeypatch.setattr(manager.asr_provider, "open", opened)
        monkeypatch.setattr(app.state.llm.inner, "generate", generated)
        monkeypatch.setattr(manager, "_locked_session", locked)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                       base_url="http://safe-metadata-owned-asgi") as http:
                sid = target["sid"] = await create(http, headers[0], "safe-sql-affected")
                other = await create(http, headers[1], "safe-sql-unaffected")
                runtime = target["runtime"] = manager.runtimes[sid]
                original_pcm, original_stream = runtime.audio, runtime.asr
                a, b = WsProbe(app, headers[0], sid), WsProbe(app, headers[1], other)
                wires.extend((a, b))
                await a.start()
                await b.start()
                await a.end()
                a.drain_task = asyncio.create_task(a.drain())
                async with asyncio.timeout(3):
                    await entered.wait()
                    while not any(event["type"] == "asr.final" for event in a.events):
                        await asyncio.sleep(.01)
                before = await facts(app, users[0], sid)
                assert before["session"]["state"] == "refining" and before["attempt"]["has_raw"]
                assert before["session"]["audio_complete"] and before["counts"]["results"] == 0
                assert before["counts"]["model_calls"] == 1
                assert runtime.audio is original_pcm and len(original_pcm) == 0
                assert runtime.asr is None and original_stream.cancelled
                assert app.state.config.asr.finish_timeout_ms == app.state.config.llm.call_timeout_ms == 10000
                assert app.state.config.pipeline.stop_to_result_budget_ms == 30000
                assert runtime.deadline == runtime.end_accepted_at + 30
                original_deadline = runtime.deadline
                release.set()
                await a.drain_task
                failed = await until_fact(app, users[0], sid,
                    lambda value: value["session"]["state"] == "failed")
                assert failed["attempt"]["state"] == "failed"
                assert failed["session"]["error_code"] == failed["attempt"]["error_code"] == "INTERNAL_ERROR"
                assert failed["counts"]["results"] == failed["counts"]["insertions"] == failed["counts"]["learning_events"] == 0
                assert failed["counts"]["model_calls"] == 1
                assert observed["fault_count"] == 1 and observed["bound_marker_present"]
                assert observed["real_driver_message_contains_marker"] and observed["real_driver_sqlstate"] == "22P02"
                assert a.close_code == 1011 and runtime.task.done()
                assert any(event["type"] == "error" and event["error"]["code"] == "INTERNAL_ERROR" for event in a.events)
                assert not any(event["type"] == "result.ready" for event in a.events)
                assert runtime.deadline == original_deadline and runtime.audio is original_pcm
                assert len(original_pcm) == 0 and runtime.asr is None
                detail = await http.get(f"/api/v1/sessions/{sid}", headers=headers[0])
                assert detail.status_code == 200 and detail.json()["state"] == "failed" and detail.json()["result"] is None

                # Existing healthy account and same-account new round remain usable.
                await b.end()
                b.drain_task = asyncio.create_task(b.drain())
                await b.drain_task
                healthy = await facts(app, users[1], other)
                assert healthy["session"]["state"] == healthy["attempt"]["state"] == "ready"
                assert healthy["counts"]["results"] == 1 and b.close_code == 1000
                recovered = await create(http, headers[0], "safe-sql-recovered")
                c = WsProbe(app, headers[0], recovered)
                wires.append(c)
                await c.start()
                await c.end()
                c.drain_task = asyncio.create_task(c.drain())
                await c.drain_task
                resumed = await facts(app, users[0], recovered)
                assert resumed["session"]["state"] == resumed["attempt"]["state"] == "ready"
                assert resumed["counts"]["results"] == 1 and c.close_code == 1000
                assert healthy["counts"]["model_calls"] == resumed["counts"]["model_calls"] == 1
                for current in (healthy, resumed):
                    assert current["counts"]["insertions"] == current["counts"]["learning_events"] == 0
                assert (await http.get("/health/ready")).status_code == 200
                later = await facts(app, users[0], sid)
                assert all(later[key] == failed[key] for key in ("session", "attempt", "counts"))
                assert later["active"] == 0 and sid not in manager.runtimes
                assert not app.state.instance_fenced
                assert len(observed["asr_opens"]) == 3
                for current_sid in (sid, other, recovered):
                    calls = [item for item in observed["dispatches"] if item[0] == current_sid]
                    assert [item[2] for item in calls] == ["refine"]
                    assert len({item[3] for item in calls}) == 1

                safe_records = [record for record in caplog.records if record.name == LOGGER]
                assert len(safe_records) == 1
                record = safe_records[0]
                assert type(record.msg) is str and record.args == ()
                payload = json.loads(record.msg)
                assert set(payload) == {"event", "phase", "code", "session_id", "attempt_no", "exception_type", "frames"}
                assert payload["event"] == "RUNTIME_SAFE_EXCEPTION"
                assert payload["phase"] == "process_sqlalchemy" and payload["code"] == "INTERNAL_ERROR"
                assert payload["session_id"] == sid and payload["attempt_no"] == 1
                assert payload["exception_type"] == observed["exception_type"]
                assert 1 <= len(payload["frames"]) <= 8
                for frame in payload["frames"]:
                    assert set(frame) == {"basename", "line", "function"}
                    assert type(frame["basename"]) is str and "/" not in frame["basename"] and "\\" not in frame["basename"]
                    assert type(frame["line"]) is int and frame["line"] > 0
                    assert type(frame["function"]) is str
                # Includes every captured msg/args/extra/trace field, not just final text.
                for logged in caplog.records:
                    assert logged.exc_info is logged.exc_text is logged.stack_info is None
                    assert_scalar_tree_without_needles(logged.__dict__, needles)
                    assert_scalar_tree_without_needles(logged.getMessage(), needles)
                assert_scalar_tree_without_needles(caplog.text, needles)
                return {"session_id": sid, "exception_type": observed["exception_type"],
                    "actual_bound_pg_cast_failed": True, "driver_sqlstate": "22P02",
                    "safe_event_count": 1, "trace_nodes": len(payload["frames"]),
                    "original_failed_internal_error_and_ws1011": True,
                    "normal_other_and_new_round_ready": True, "model_calls_per_session": 1,
                    "original_single_llm_ms": 10000, "original_total_ms": 30000,
                    "raw_credentials_or_exception_body_exported": False,
                    "historical_CAP13_root_cause_or_acceptance_pass_claimed": False}
        finally:
            release.set()
            for wire in wires:
                await settle(wire)

    safe = atomic_client.portal.call(scenario)
    record_property("safe_runtime_exception_metadata", json.dumps(safe, sort_keys=True))
