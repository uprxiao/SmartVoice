"""VOICE12: real process death + new lifespan + PG, physical providers are Mocks.

No listener or existing service is touched. Only retained multiprocessing spawn
Process objects owned by this test are killed. API/WS transitions, the advisory
lock, startup snapshot/recovery, and persisted rows are production behavior.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing
import os
import re
import signal
import traceback
import uuid

import asyncpg
import pytest
from sqlalchemy.engine import make_url

from tests.conftest import PG_HOST, PG_PASSWORD, PG_PORT, PG_USER, SERVER_ROOT, TEST_DB, TEST_URL, wait_for_readiness_async


PASSWORD = "VoiceRestartFixture123!"
TABLES = ("sessions", "session_attempts", "results", "insertions", "learning_events",
          "session_contributions", "model_calls", "operation_requests")


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str,
        ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


async def connect_control():
    connection = await asyncpg.connect(host=PG_HOST, port=PG_PORT, user=PG_USER,
        password=PG_PASSWORD, database=TEST_DB)
    assert await connection.fetchval("SELECT current_database()") == TEST_DB
    return connection


async def bundle(connection, owner, sid):
    # A single committed snapshot: no synthetic state or terminal writes.
    async with connection.transaction(isolation="repeatable_read", readonly=True):
        answer = {}
        for table in TABLES:
            column = "id" if table == "sessions" else "target_id" if table == "operation_requests" else "session_id"
            rows = await connection.fetch(f"SELECT to_jsonb(t) AS row FROM {table} t "
                f"WHERE user_id=$1 AND {column}=$2 ORDER BY to_jsonb(t)::text", owner, sid)
            answer[table] = [json.loads(row["row"]) for row in rows]
    return answer


async def advisory_owners(connection, key):
    unsigned = key % (1 << 64)
    rows = await connection.fetch("""SELECT l.pid FROM pg_locks l
        WHERE l.locktype='advisory' AND l.granted AND l.mode='ExclusiveLock'
          AND l.database=(SELECT oid FROM pg_database WHERE datname=$1)
          AND l.classid::bigint=$2 AND l.objid::bigint=$3 AND l.objsubid=1""",
        TEST_DB, unsigned >> 32, unsigned & 0xffffffff)
    return sorted(row["pid"] for row in rows)


async def login(http, nonce):
    response = await http.post("/api/auth/login", json={
        "email": f"voice12-{nonce}@example.invalid", "password": PASSWORD, "client_type": "desktop",
        "device": {"installation_id": str(uuid.uuid5(uuid.NAMESPACE_URL, "voice12:" + nonce)),
            "name": "Restart fixture", "platform": "macos", "client_version": "0.0.1"}})
    assert response.status_code == 200, {"status": response.status_code}
    value = response.json()
    return {"Authorization": "Bearer " + value["access_token"]}, value["user"]["id"]


async def new_session(http, headers):
    key = str(uuid.uuid4())
    response = await http.post("/api/v1/sessions", headers=headers | {"Idempotency-Key": key},
        json={"client_request_id": key, "learning_enabled": True})
    assert response.status_code == 201, {"status": response.status_code}
    return response.json()["session_id"]


async def complete_session(app, http, headers):
    from tests.integration.test_normal_shutdown_contract import WsProbe
    sid = await new_session(http, headers)
    probe = await WsProbe(app, headers, sid).start()
    await probe.end()
    await probe.drain()
    assert probe.close_code == 1000
    terminals = [event for event in probe.events
        if event["type"] in ("result.ready", "error", "session.empty", "cancelled")]
    assert len(terminals) == 1 and terminals[0]["type"] == "result.ready"
    assert sid not in app.state.sessions.runtimes
    return sid


async def child_run(pipe, expected_parent, nonce, stage, role, prior):
    # Environment is frozen by the parent before spawn; assert the exact DSN DB
    # before constructing the real application or touching any DB connection.
    assert os.getppid() == expected_parent and multiprocessing.parent_process().pid == expected_parent
    assert re.fullmatch(r"smartvoiceinput_test_[a-zA-Z0-9_]{1,38}", TEST_DB)
    assert make_url(TEST_URL).database == os.environ["SVI_TEST_DB_NAME"] == TEST_DB
    assert os.environ["SVI_DATABASE_URL"] == TEST_URL
    from app import main
    from app.providers.asr.mock import MockAsrSession
    from app.providers.llm.mock import MockLlmProvider
    from app.services.inference_context import current_scope
    from tests.integration.test_normal_shutdown_contract import WsProbe
    import httpx

    pipe.send({"event": "owned_child", "pid": os.getpid(), "ppid": os.getppid(),
        "nonce": nonce, "database": TEST_DB, "role": role,
        "loop_class": type(asyncio.get_running_loop()).__module__ + "." + type(asyncio.get_running_loop()).__name__})
    assert type(asyncio.get_running_loop()).__module__ == "uvloop"
    target, entered, release = {}, asyncio.Event(), asyncio.Event()
    actual_finish, actual_generate = MockAsrSession.finish, MockLlmProvider.generate
    gate = {"returned": False, "cancelled": False}
    calls = []

    async def hold(deadline):
        gate["deadline"] = deadline
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            gate["cancelled"] = True
            raise
        assert asyncio.get_running_loop().time() < deadline
        gate["returned"] = True

    async def finish(stream, samples, deadline):
        if role == "A" and stage == "finalizing" and stream.options.session_id == target.get("sid"):
            assert samples == stream.samples == 4000
            await hold(deadline)
        return await actual_finish(stream, samples, deadline)

    async def generate(provider, request, deadline):
        scope = current_scope.get()
        calls.append((scope.session_id if scope else None, request.purpose))
        if role == "A" and stage == "refining" and request.purpose == "refine" and scope and scope.session_id == target.get("sid"):
            assert request.call_id and scope.attempt_no == 1
            await hold(deadline)
        return await actual_generate(provider, request, deadline)

    # Process-local physical Mock gates; no production state/clock/recovery patch.
    MockAsrSession.finish, MockLlmProvider.generate = finish, generate
    app = main.create_app(str(SERVER_ROOT / "config/app.yaml"))
    close_seen = {}
    control = None
    try:
        async with app.router.lifespan_context(app):
            # Still bounded by the parent's original 20s receive window; this
            # does not reset the checkpoint or any physical-call deadline.
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                         base_url="http://testserver") as health_client:
                await wait_for_readiness_async(health_client)
            assert app.state.ready and not app.state.instance_fenced
            assert app.state.config.asr.provider == app.state.config.llm.provider == "mock"
            assert app.state.config.pipeline.stop_to_result_budget_ms == 30000
            assert app.state.config.asr.finish_timeout_ms == app.state.config.llm.call_timeout_ms == 10000
            original_close = app.state.db.close

            async def observe_close():
                close_seen["checkedout_before_dispose"] = app.state.db.engine.pool.checkedout()
                await original_close()
                close_seen["completed"] = True

            app.state.db.close = observe_close
            control = await connect_control()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://testserver") as http:
                if role == "A":
                    response = await http.post("/api/auth/register", json={
                        "email": f"voice12-{nonce}@example.invalid", "password": PASSWORD})
                    assert response.status_code == 201, {"status": response.status_code}
                headers, owner = await login(http, nonce)
                if role == "A":
                    history_sid = await complete_session(app, http, headers)
                    history_before = await bundle(control, owner, history_sid)
                    target["sid"] = sid = await new_session(http, headers)
                    runtime = app.state.sessions.runtimes[sid]
                    pcm = runtime.audio
                    probe = await WsProbe(app, headers, sid).start()
                    await probe.end()
                    # Drain concurrently so a watchdog/protocol error cannot hide
                    # behind a test-created blocked send queue.
                    probe.drain_task = asyncio.create_task(probe.drain())
                    async with asyncio.timeout(3):
                        await entered.wait()
                    before = await bundle(control, owner, sid)
                    session, attempt = before["sessions"][0], before["session_attempts"][0]
                    assert session["state"] == stage and session["audio_complete"] is True
                    assert session["received_samples"] == 4000 and session["end_accepted_at"] is not None
                    assert attempt["state"] == "running" and attempt["ended_at"] is None
                    assert before["results"] == before["insertions"] == before["learning_events"] == []
                    # Compare the original absolute deadline. Subtracting two
                    # floating clocks can round even when the 30s sum is exact.
                    assert runtime.deadline == runtime.end_accepted_at + 30
                    assert gate["returned"] is gate["cancelled"] is False
                    if stage == "finalizing":
                        assert attempt["raw_text"] is None and len(pcm) == 8000 and runtime.asr is not None
                        assert before["model_calls"] == []  # MockASR has no fabricated physical ledger.
                    else:
                        assert attempt["raw_text"] and len(pcm) == 0 and runtime.asr is None
                        assert len(before["model_calls"]) == 1
                        assert (before["model_calls"][0]["purpose"], before["model_calls"][0]["status"]) == ("refine", "running")
                    lock_key = app.state.config.server.instance_lock_key
                    lock_owners = await advisory_owners(control, lock_key)
                    assert len(lock_owners) == 1
                    pipe.send({"event": "end_committed_and_gated", "pid": os.getpid(), "owner": owner,
                        "sid": sid, "history_sid": history_sid, "bundle": before,
                        "history_hash": fingerprint(history_before), "pcm_bytes": len(pcm),
                        "lock_key": lock_key, "lock_backend_pid": lock_owners[0],
                        "remaining_physical_ms": int((gate["deadline"]-asyncio.get_running_loop().time())*1000)})
                    # Intentionally never release: the parent confirms this exact
                    # process's SIGKILL exit, then checks persisted state before B.
                    await asyncio.Event().wait()
                else:
                    assert prior and prior["owner"] == owner
                    sid, history_sid = prior["sid"], prior["history_sid"]
                    restored = await bundle(control, owner, sid)
                    session, attempt = restored["sessions"][0], restored["session_attempts"][0]
                    assert session["audio_complete"] is True and session["received_samples"] == 4000
                    assert session["current_attempt_no"] == 1 and session["first_terminal_at"] is not None
                    assert attempt["error_code"] == "SERVER_RESTARTED" and attempt["ended_at"] is not None
                    assert restored["insertions"] == restored["learning_events"] == []
                    assert sid not in app.state.sessions.runtimes
                    assert await control.fetchval("SELECT count(*) FROM sessions WHERE state IN ('created','streaming','finalizing','refining')") == 0
                    assert calls == [], "startup recovery must not re-dispatch old inference"
                    assert fingerprint(await bundle(control, owner, history_sid)) == prior["history_hash"]
                    detail = await http.get(f"/api/v1/sessions/{sid}", headers=headers)
                    assert detail.status_code == 200
                    body = detail.json()
                    if stage == "finalizing":
                        assert (session["state"], session["error_code"], session["retry_cache_status"]) == ("failed", "SERVER_RESTARTED", "server_restarted")
                        assert attempt["state"] == "failed" and attempt["raw_text"] is None
                        assert restored["results"] == restored["model_calls"] == []
                        assert (body["retry_kind"], body["retry_unavailable_reason"], body["result"]) == ("none", "server_restarted", None)
                    else:
                        raw = prior["bundle"]["session_attempts"][0]["raw_text"]
                        assert session["state"] == attempt["state"] == "ready" and session["error_code"] is None
                        assert len(restored["results"]) == 1
                        result = restored["results"][0]
                        assert result["raw_text"] == result["refined_text"] == attempt["raw_text"] == raw
                        assert result["review_required"] is True and result["review_reason"] == "server_restarted"
                        assert result["text_hash"] == hashlib.sha256(raw.encode()).hexdigest()
                        assert session["current_result_id"] == result["id"]
                        assert body["retry_kind"] == "refine" and body["result"]["review_required"] is True
                        assert len(restored["model_calls"]) == 1
                        recovered_call = restored["model_calls"][0]
                        assert (recovered_call["status"], recovered_call["error_code"]) == ("unknown", "SERVER_RESTARTED")
                        assert recovered_call["prompt_tokens"] is recovered_call["completion_tokens"] is None
                        assert recovered_call["ended_at"] and recovered_call["aggregated_at"]
                    page = await http.get("/api/v1/history", headers=headers)
                    assert page.status_code == 200
                    by_id = {row["session_id"]: row for row in page.json()["items"]}
                    assert {sid, history_sid} <= set(by_id) and by_id[history_sid]["state"] == "ready"
                    assert by_id[sid]["state"] == body["state"]
                    baseline_hash = fingerprint(restored)
                    rejected = await http.post(f"/api/v1/sessions/{sid}/retry", headers=headers | {
                        "Idempotency-Key": str(uuid.uuid4())}, json={"retry_kind": "asr"})
                    assert rejected.status_code == 409 and rejected.json()["error"]["code"] == "RETRY_UNAVAILABLE"
                    rejection_reason = rejected.json()["error"]["details"]["reason"]
                    # Do the other oracles first; report the precise reason only
                    # after subsequent normal processing has been proved.
                    assert fingerprint(await bundle(control, owner, sid)) == baseline_hash
                    new_sid = await complete_session(app, http, headers)
                    assert new_sid not in (sid, history_sid)
                    assert fingerprint(await bundle(control, owner, sid)) == baseline_hash
                    assert fingerprint(await bundle(control, owner, history_sid)) == prior["history_hash"]
                    assert all(call_sid == new_sid for call_sid, _ in calls)
                    assert [purpose for _, purpose in calls] == ["refine"]
                    assert not app.state.sessions.runtimes and await control.fetchval("SELECT count(*) FROM sessions WHERE state IN ('created','streaming','finalizing','refining')") == 0
                    expected_reason = "server_restarted" if stage == "finalizing" else "not_needed"
                    assert rejection_reason == expected_reason, {"actual_reason": rejection_reason, "expected_reason": expected_reason}
                    evidence = {"stage": stage, "recovered_state": session["state"],
                        "results": len(restored["results"]), "model_calls": len(restored["model_calls"]),
                        "asr_retry_status": rejected.status_code, "asr_retry_reason": rejection_reason,
                        "history_preserved": True, "new_session_ready": True,
                        "old_bundle_unchanged_after_new_result": True, "runtime_count": 0}
            await control.close()
            control = None
        if role == "B":
            assert close_seen == {"checkedout_before_dispose": 0, "completed": True}
            pipe.send({"event": "completed", "pid": os.getpid(), "evidence": evidence,
                "close": close_seen})
    finally:
        if control is not None:
            await control.close()
        MockAsrSession.finish, MockLlmProvider.generate = actual_finish, actual_generate


def child_entry(pipe, expected_parent, nonce, stage, role, prior):
    try:
        import uvloop
        uvloop.run(child_run(pipe, expected_parent, nonce, stage, role, prior))
    except BaseException as error:
        # No traceback/frame locals/request headers/DSN in pipe or evidence.
        reason_detail = {}
        if isinstance(error, AssertionError) and error.args and isinstance(error.args[0], dict):
            for key in ("actual_reason", "expected_reason"):
                value = error.args[0].get(key)
                if isinstance(value, str) and re.fullmatch(r"[a-z_]{1,40}", value):
                    reason_detail[key] = value
        pipe.send({"event": "failed", "pid": os.getpid(), "exception": type(error).__name__,
            "reason_detail": reason_detail,
            "location": {"file": os.path.basename(traceback.extract_tb(error.__traceback__)[-1].filename),
                         "line": traceback.extract_tb(error.__traceback__)[-1].lineno}})
        raise SystemExit(2) from None
    finally:
        pipe.close()


def receive(pipe, process, expected_event, seconds=20):
    assert pipe.poll(seconds), {"waiting_for": expected_event, "alive": process.is_alive(), "exitcode": process.exitcode}
    value = pipe.recv()
    assert value.get("event") == expected_event, {"event": value.get("event"), "exception": value.get("exception"), "location": value.get("location"), "reason_detail": value.get("reason_detail")}
    assert value["pid"] == process.pid
    return value


def start_owned(context, processes, nonce, stage, role, prior=None):
    parent_pipe, child_pipe = context.Pipe(duplex=False)
    process = context.Process(target=child_entry,
        args=(child_pipe, os.getpid(), nonce, stage, role, prior),
        name=f"svi_voice12_{os.getpid()}_{nonce}_{role}")
    process.start()
    child_pipe.close()
    processes.append((process, parent_pipe))
    greeting = receive(parent_pipe, process, "owned_child")
    assert greeting["ppid"] == os.getpid() and greeting["nonce"] == nonce
    assert greeting["database"] == TEST_DB and greeting["role"] == role
    assert greeting["loop_class"] == "uvloop.Loop"
    return process, parent_pipe


@pytest.mark.parametrize("stage", ["finalizing", "refining"])
def test_tc_voice_12_complete_end_survives_owned_process_crash(
    clean_database, monkeypatch, record_property, stage,
):
    from tests.integration.test_normal_shutdown_contract import require_owned_database
    require_owned_database()
    assert os.name == "posix", "this crash harness requires the current macOS/Linux SIGKILL scope"
    nonce = uuid.uuid4().hex
    for name, value in {
        "SVI_DATABASE_URL": TEST_URL, "SVI_CONFIG": str(SERVER_ROOT / "config/app.yaml"),
        "SVI_INSTANCE_LOCK": "1", "SVI_ENVIRONMENT": "development", "SVI_ASR_PROVIDER": "mock",
        "SVI_LLM_PROVIDER": "mock", "WEB_CONCURRENCY": "1", "UVICORN_WORKERS": "1",
        "SVI_JWT_SECRET": "voice12-jwt-" + nonce,
        "SVI_IDEMPOTENCY_SECRET": "voice12-idempotency-" + nonce,
    }.items():
        monkeypatch.setenv(name, value)
    context, processes = multiprocessing.get_context("spawn"), []
    evidence = {"stage": stage, "scope": "real-owned-process-SIGKILL+new-lifespan+PG+ASGI; Mock-models"}
    try:
        a, pipe_a = start_owned(context, processes, nonce, stage, "A")
        checkpoint = receive(pipe_a, a, "end_committed_and_gated")
        assert checkpoint["remaining_physical_ms"] > 0 and a.is_alive()
        # Use the retained Process object, never PID search/pg_terminate_backend.
        a.kill()
        a.join(5)
        assert not a.is_alive() and a.exitcode == -signal.SIGKILL
        evidence["a_exitcode"] = a.exitcode

        async def after_exit():
            connection = await connect_control()
            try:
                async with asyncio.timeout(4):
                    while checkpoint["lock_backend_pid"] in await advisory_owners(connection, checkpoint["lock_key"]):
                        await asyncio.sleep(.01)
                assert await advisory_owners(connection, checkpoint["lock_key"]) == []
                assert fingerprint(await bundle(connection, checkpoint["owner"], checkpoint["sid"])) == fingerprint(checkpoint["bundle"])
                assert fingerprint(await bundle(connection, checkpoint["owner"], checkpoint["history_sid"])) == checkpoint["history_hash"]
            finally:
                await connection.close()

        asyncio.run(after_exit())  # Exact active rows survived; recovery has not yet run.
        b, pipe_b = start_owned(context, processes, nonce, stage, "B", checkpoint)
        assert b.pid != a.pid
        completed = receive(pipe_b, b, "completed")
        b.join(5)
        assert not b.is_alive() and b.exitcode == 0
        evidence.update(completed["evidence"], b_exitcode=b.exitcode, close=completed["close"])
    finally:
        for process, pipe in reversed(processes):
            if process.is_alive():
                # Failure cleanup is restricted to this invocation's retained
                # direct-child handle; it is not evidence of a passing shutdown.
                assert process.name.startswith(f"svi_voice12_{os.getpid()}_{nonce}_")
                process.kill()
                process.join(5)
            assert not process.is_alive(), "owned fixture child failed to exit"
            pipe.close()
        record_property("voice12_process_restart", json.dumps(evidence, sort_keys=True))
