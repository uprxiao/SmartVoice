"""CAP22: one real, exactly owned PG business backend dies before result SQL.

Formal ASGI HTTP/WS + real PG; configured physical ASR/LLM are declared Mocks.
No database outage, model throughput, TCP or complete CAP22 claim is made.
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid

import asyncpg
import httpx
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from tests.conftest import PG_HOST, PG_PASSWORD, PG_PORT, PG_USER, TEST_DB, auth_headers, register_and_login
from tests.integration.test_capacity_admission_atomicity import atomic_client  # noqa: F401
from tests.integration.test_instance_recovery_loss import require_process_owned_database
from tests.integration.test_normal_shutdown_contract import WsProbe
from tests.integration.test_voice_cancel_boundaries import create, facts, settle, until_fact


def test_cap22_exact_business_backend_disconnect_isolated_and_new_round_recovers(
    atomic_client, monkeypatch, record_property,
):
    require_process_owned_database()
    assert PG_HOST in {"127.0.0.1", "localhost", "::1"}
    pairs = [register_and_login(atomic_client, email=f"cap22-db-{i}@example.invalid") for i in range(2)]
    headers = [auth_headers(pair) for pair in pairs]
    users = [pair["user"]["id"] for pair in pairs]

    async def scenario():
        app, manager = atomic_client.app, atomic_client.app.state.sessions
        loop = asyncio.get_running_loop()
        entered, release = asyncio.Event(), asyncio.Event()
        target, observed, wires = {}, {"gate_count": 0, "sql_errors": []}, []
        tag = f"svi_business_{os.getpid()}_{uuid.uuid4().hex[:10]}"
        actual_locked = manager._locked_session
        actual_close = app.state.db.close

        async def close_observed():
            # Executed by the ordinary app_client lifespan teardown, not here.
            before = app.state.db.engine.pool.checkedout()
            await actual_close()
            record_property("cap22_business_db_close", json.dumps({"checkedout_before_dispose": before}))
            assert before == 0

        async def locked(db, user_id, session_id):
            runtime = target.get("runtime")
            selected = (runtime is not None and user_id == users[0] and session_id == target["sid"]
                and asyncio.current_task() is runtime.task and runtime.state == "refining"
                and observed["gate_count"] == 0)
            if selected:
                # This is the original _store_result AsyncSession. It has not
                # read/locked User or Session, inserted Result, or committed.
                observed["gate_count"] += 1
                await db.execute(text("SELECT set_config('application_name', :tag, true)"), {"tag": tag})
                row = (await db.execute(text("""SELECT pid,datid,backend_start,xact_start,
                    current_database() AS db,current_setting('application_name') AS tag
                    FROM pg_stat_activity WHERE pid=pg_backend_pid()"""))).mappings().one()
                assert row["db"] == TEST_DB and row["tag"] == tag and row["xact_start"] is not None
                target["backend"] = dict(row)
                entered.set()
                await release.wait()  # Controlled delivery gate; no replacement SQL/exception.
                assert loop.time() < runtime.deadline
            try:
                answer = await actual_locked(db, user_id, session_id)
            except SQLAlchemyError as error:
                if selected:
                    observed["sql_errors"].append({"type": type(error).__name__,
                        "invalidated": bool(getattr(error, "connection_invalidated", False))})
                raise
            if session_id == target.get("recovered_sid"):
                observed["recovered_backend_pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
            return answer

        monkeypatch.setattr(manager, "_locked_session", locked)
        monkeypatch.setattr(app.state.db, "close", close_observed)
        control = await asyncpg.connect(host=PG_HOST, port=PG_PORT, user=PG_USER,
            password=PG_PASSWORD, database=TEST_DB,
            server_settings={"application_name": tag + "_control"})
        try:
            assert await control.fetchval("SELECT current_database()") == TEST_DB
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://cap22-test") as http:
                sid = target["sid"] = await create(http, headers[0], "cap22-db-affected")
                other = await create(http, headers[1], "cap22-db-unaffected")
                runtime = target["runtime"] = manager.runtimes[sid]
                original_audio = runtime.audio
                a, b = WsProbe(app, headers[0], sid), WsProbe(app, headers[1], other)
                wires.extend([a, b])
                await a.start()
                await b.start()
                assert (await facts(app, users[1], other))["session"]["state"] == "streaming"
                await a.end()
                a.drain_task = asyncio.create_task(a.drain())
                async with asyncio.timeout(3):
                    await entered.wait()
                before = await facts(app, users[0], sid)
                assert before["session"]["state"] == "refining" and before["session"]["audio_complete"]
                assert before["attempt"]["has_raw"] and before["counts"]["results"] == 0
                assert before["counts"]["model_calls"] == 1
                assert runtime.audio == original_audio == b"" and runtime.asr is None
                assert app.state.config.asr.finish_timeout_ms == app.state.config.llm.call_timeout_ms == 10000
                assert runtime.deadline == runtime.end_accepted_at + 30
                original_deadline = runtime.deadline
                victim = target["backend"]
                require_process_owned_database()
                # Exact current DB, backend generation, transaction, nonce tag,
                # client-backend type; additionally refuse any advisory owner.
                # A missing/mismatched row terminates nobody and fails the test.
                killed = await control.fetch("""SELECT pg_terminate_backend(a.pid) AS terminated
                    FROM pg_stat_activity a WHERE current_database()=$1 AND a.datname=$1
                      AND a.datid=$2 AND a.pid=$3 AND a.backend_start=$4 AND a.xact_start=$5
                      AND a.application_name=$6 AND a.backend_type='client backend'
                      AND a.pid<>pg_backend_pid() AND a.state='idle in transaction'
                      AND NOT EXISTS(SELECT 1 FROM pg_locks l WHERE l.pid=a.pid AND l.locktype='advisory')
                    """, TEST_DB, victim["datid"], victim["pid"], victim["backend_start"], victim["xact_start"], tag)
                assert len(killed) == 1 and killed[0]["terminated"] is True
                async with asyncio.timeout(3):
                    while await control.fetchval("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE pid=$1 AND backend_start=$2)",
                        victim["pid"], victim["backend_start"]):
                        await asyncio.sleep(.01)
                observed["terminated_pid"] = victim["pid"]
                # The healthy session completes while A's original operation is
                # still held. No global 503/fence is required for one lost socket.
                await b.end()
                b.drain_task = asyncio.create_task(b.drain())
                await b.drain_task
                healthy = await facts(app, users[1], other)
                assert healthy["session"]["state"] == healthy["attempt"]["state"] == "ready"
                assert healthy["counts"]["results"] == 1 and b.close_code == 1000
                assert healthy["counts"]["insertions"] == healthy["counts"]["learning_events"] == 0
                assert sum(e["type"] == "result.ready" for e in b.events) == 1
                assert not any(e["type"] == "error" for e in b.events)
                release.set()
                await a.drain_task
                failed = await until_fact(app, users[0], sid, lambda f: f["session"]["state"] == "failed")
                assert failed["attempt"]["state"] == "failed"
                assert failed["session"]["error_code"] == failed["attempt"]["error_code"] == "INTERNAL_ERROR"
                assert failed["counts"]["results"] == failed["counts"]["insertions"] == failed["counts"]["learning_events"] == 0
                assert failed["counts"]["model_calls"] == 1 and observed["sql_errors"]
                assert not any(e["type"] == "result.ready" for e in a.events)
                assert any(e["type"] == "error" and e["error"]["code"] == "INTERNAL_ERROR" for e in a.events)
                assert a.close_code == 1011 and runtime.task.done()
                assert runtime.deadline == original_deadline and runtime.audio == original_audio == b"" and runtime.asr is None
                output = await http.get(f"/api/v1/sessions/{sid}", headers=headers[0])
                assert output.status_code == 200 and output.json()["state"] == "failed" and output.json()["result"] is None
                recovered = target["recovered_sid"] = await create(http, headers[0], "cap22-db-recovered")
                c = WsProbe(app, headers[0], recovered)
                wires.append(c)
                await c.start()
                await c.end()
                c.drain_task = asyncio.create_task(c.drain())
                await c.drain_task
                resumed = await facts(app, users[0], recovered)
                assert resumed["session"]["state"] == resumed["attempt"]["state"] == "ready"
                assert resumed["counts"]["results"] == 1 and resumed["counts"]["model_calls"] == 1
                assert resumed["counts"]["insertions"] == resumed["counts"]["learning_events"] == 0
                assert c.close_code == 1000 and sum(e["type"] == "result.ready" for e in c.events) == 1
                assert not any(e["type"] == "error" for e in c.events)
                assert observed["recovered_backend_pid"] != victim["pid"]
                later = await facts(app, users[0], sid)
                assert all(later[key] == failed[key] for key in ("session", "attempt", "counts"))
                assert later["active"] == 0 and runtime.audio == original_audio == b"" and sid not in manager.runtimes
                assert not app.state.instance_fenced
                calls = await control.fetch("SELECT status,ended_at,aggregated_at FROM model_calls WHERE user_id=ANY($1::text[])", users)
                assert len(calls) == 3 and all(r["status"] == "succeeded" and r["ended_at"] and r["aggregated_at"] for r in calls)
                observed.update(affected="failed/INTERNAL_ERROR", healthy="ready", recovered="ready",
                    affected_results=0, other_results=2, side_effects=0, original_total_budget=30,
                    original_single_call_budget=10, old_audio_bytes=0, real_model_inference_claimed=False,
                    declared_mock_ledger_calls=3, scope="one-owned-business-backend-not-cluster-outage")
        finally:
            release.set()
            for wire in wires:
                await settle(wire)
            await control.close()
            record_property("cap22_business_db_disconnect", json.dumps(observed, sort_keys=True))

    atomic_client.portal.call(scenario)
