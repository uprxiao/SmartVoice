"""Ordinary initial ASR has no audio TTL: raw SQL can wait past finish10.

Real PG/API/ASGI WS/MlxAsrSession/AsrEngine/scheduler/ledger. Reuses the
synthetic physical worker and Mock LLM; no clock/budget/session-state/ledger changes.
This is a nonregression control, not real model/audio/native or TTL600 proof.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import httpx
from sqlalchemy import text

from tests.conftest import auth_headers, register_and_login
from tests.integration.test_asr_finish_queue_deadline import RAW, finish_client, ledger
from tests.integration.test_normal_shutdown_contract import WsProbe, create_session, require_owned_database, safe_status


def test_initial_asr_durable_raw_may_follow_original_finish_deadline(finish_client, monkeypatch, record_property):
    client, control = finish_client
    require_owned_database()
    actor = register_and_login(client, email="initial-raw-wait@example.invalid")
    headers, user_id = auth_headers(actor), actor["user"]["id"]
    runtime_source = Path(__file__).resolve().parents[2] / "app/services/session_runtime.py"
    trace = {"scope": "PG/API/ASGI; production adapter/scheduler/ledger; synthetic physical ASR + Mock LLM",
             "source_before": hashlib.sha256(runtime_source.read_bytes()).hexdigest(), "completed": False}

    async def scenario():
        app, manager, loop = client.app, client.app.state.sessions, asyncio.get_running_loop()
        released, resume, locked, unlock, lock_entered = [asyncio.Event() for _ in range(5)]
        original_release, original_lock = manager._release_audio, manager._locked_session
        wire = runtime = lock_task = None
        sid = None

        async def release_audio(current):
            await original_release(current)
            if current is runtime and not released.is_set():
                assert current.state == "finalizing" and current.audio_lease is None
                assert current.retry_expires_at is None and not current.audio
                trace["actual_release_return_loop"] = loop.time()
                released.set()
                await resume.wait()  # Already finished ASR; raw transaction has not started.

        async def locked_session(db, owner, session_id):
            if session_id == sid and resume.is_set() and not lock_entered.is_set():
                trace["raw_pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
                trace["raw_lock_enter_loop"] = loop.time()
                lock_entered.set()
            return await original_lock(db, owner, session_id)

        async def hold_user():
            async with app.state.db.session_factory() as db:
                trace["holder_pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
                assert await db.scalar(text("SELECT id FROM users WHERE id=:u FOR UPDATE"), {"u": user_id}) == user_id
                trace["user_locked_loop"] = loop.time()
                locked.set()
                try:
                    await unlock.wait()
                finally:
                    await db.rollback()
            trace["user_unlocked_loop"] = loop.time()

        async def prove_wait():
            async with asyncio.timeout(2):
                await lock_entered.wait()
                async with app.state.db.session_factory() as db:
                    while not await db.scalar(text("WITH RECURSIVE w(pid,depth) AS (SELECT CAST(:p AS integer),0 "
                        "UNION ALL SELECT b,depth+1 FROM w CROSS JOIN LATERAL unnest(pg_blocking_pids(pid)) AS blockers(b) WHERE depth<4) "
                        "SELECT EXISTS(SELECT 1 FROM w WHERE pid=CAST(:h AS integer) AND depth>0)"),
                        {"p": trace["raw_pid"], "h": trace["holder_pid"]}):
                        await asyncio.sleep(.01)
            trace["real_lock_wait_loop"] = loop.time()

        async def read_attempt():
            async with app.state.db.session_factory() as db:
                row = (await db.execute(text("SELECT s.state,s.current_attempt_no,s.first_asr_failed_at,a.raw_text "
                    "FROM sessions s JOIN session_attempts a ON a.user_id=s.user_id AND a.session_id=s.id "
                    "AND a.attempt_no=s.current_attempt_no WHERE s.user_id=:u AND s.id=:s"),
                    {"u": user_id, "s": sid})).one()
                return {"state": row.state, "attempt": row.current_attempt_no,
                        "first_failure_absent": row.first_asr_failed_at is None,
                        "has_raw": row.raw_text is not None, "raw_matches": row.raw_text == RAW}

        monkeypatch.setattr(manager, "_release_audio", release_audio)
        monkeypatch.setattr(manager, "_locked_session", locked_session)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as http:
            try:
                async with asyncio.timeout(35):  # Anti-hang only, original finish10 and total30 unchanged.
                    created = await create_session(http, headers)
                    assert created.status_code == 201, safe_status(created)
                    sid = created.json()["session_id"]
                    runtime = manager.runtimes[sid]
                    assert runtime.attempt_no == 1 and runtime.audio_lease is runtime.retry_expires_at is None
                    control.modes[sid] = "success"  # Existing declared physical fixture, no application state write.
                    wire = WsProbe(app, headers, sid)
                    await wire.start()
                    stream = control.streams[sid]
                    audio, provider_buffer = runtime.audio, stream.buffer
                    await wire.end()
                    wire.drain_task = asyncio.create_task(wire.drain())
                    end, finish, total = runtime.end_accepted_at, runtime.end_accepted_at + 10, runtime.deadline
                    trace.update(end_loop=end, finish10_loop=finish, total30_loop=total)
                    assert total == end + 30
                    async with asyncio.timeout(2):
                        await released.wait()
                        calls_before = await ledger(app, [sid])
                        trace["before"] = await read_attempt()
                        assert len(calls_before) == 1 and calls_before[0]["purpose"] == "asr_finish"
                        assert calls_before[0]["status"] == "succeeded" and calls_before[0]["ended_at"] is not None
                        assert trace["before"] == {"state": "finalizing", "attempt": 1,
                            "first_failure_absent": True, "has_raw": False, "raw_matches": False}
                        assert len(control.calls) == 1 and control.calls[0]["returned_at"] < finish
                        assert trace["actual_release_return_loop"] < finish and audio == provider_buffer == b""
                        lock_task = asyncio.create_task(hold_user())
                        await locked.wait()
                    resume.set()
                    await prove_wait()
                    assert trace["real_lock_wait_loop"] < finish, "CONTROL: actual wait did not begin before finish10"
                    while loop.time() < finish + .1:
                        await asyncio.sleep(min(.05, max(0, finish + .1 - loop.time())))
                    trace["held_after_finish10"] = await read_attempt()
                    trace["unlock_requested_loop"] = loop.time()
                    assert trace["held_after_finish10"] == trace["before"]
                    assert finish < trace["unlock_requested_loop"] < total
                    assert runtime.audio_lease is runtime.retry_expires_at is None
                    unlock.set()
                    await lock_task
                    await wire.drain_task
                    response = await http.get(f"/api/v1/sessions/{sid}", headers=headers)
                    assert response.status_code == 200, safe_status(response)
                    current, calls_after = response.json(), await ledger(app, [sid])
                    trace["after"] = await read_attempt()
                    trace["terminal"] = {key: current[key] for key in ("state", "error_code", "current_attempt_no", "received_samples")}
                    trace["calls"] = [{key: row[key] for key in ("purpose", "status", "attempt_no", "processed_audio_samples")} for row in calls_after]
                    trace["close_code"], trace["ready_loop"] = wire.close_code, loop.time()
                    assert current["state"] == "ready" and current["current_attempt_no"] == 1 and current["received_samples"] == 4000
                    assert current["result"]["raw_text"] == current["result"]["refined_text"] == RAW
                    assert current["result"]["review_required"] is False
                    assert trace["after"] == {"state": "ready", "attempt": 1,
                        "first_failure_absent": True, "has_raw": True, "raw_matches": True}
                    assert wire.close_code == 1000 and [e["type"] for e in wire.events] == [
                        "ready", "audio.ack", "audio.end_ack", "asr.final", "result.ready"]
                    assert sorted(row["purpose"] for row in calls_after) == ["asr_finish", "refine"]
                    assert all(row["status"] == "succeeded" and row["attempt_no"] == 1 for row in calls_after)
                    assert control.calls[0]["returned_at"] < finish < trace["ready_loop"] < total
                    assert runtime.task.done() and audio == provider_buffer == b"" and stream.cancelled
                    assert control.worker.active_audio is None
                    assert app.state.asr.scheduler.snapshot() == {"running": 0, "high_waiting": 0, "low_waiting": 0}
                    trace["completed"] = True
            finally:
                resume.set(); unlock.set()
                try:
                    if lock_task is not None:
                        async with asyncio.timeout(5):
                            await asyncio.gather(lock_task, return_exceptions=True)
                    if runtime is not None and runtime.state in ("created", "streaming", "finalizing", "refining"):
                        async with asyncio.timeout(5):
                            await http.post(f"/api/v1/sessions/{sid}/cancel", headers=headers)
                    if wire is not None:
                        tasks = [task for task in (wire.drain_task, wire.task) if task is not None]
                        async with asyncio.timeout(35):
                            await asyncio.gather(*tasks, return_exceptions=True)
                finally:
                    trace["source_after"] = hashlib.sha256(runtime_source.read_bytes()).hexdigest()
                    record_property("ordinary_initial_raw_after_finish10", json.dumps(trace, sort_keys=True))
    client.portal.call(scenario)
    assert trace["completed"] and trace["source_before"] == trace["source_after"]
