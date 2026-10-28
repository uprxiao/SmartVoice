"""CAP22: one real SIGKILL with two simultaneously active owners, same-DB restart.

Formal ASGI/API/PG/lifespan; only physical Mock boundaries are gated. Reuses the
VOICE12 process fixture's ownership, authentication, PCM and snapshot helpers.
Not TCP reconnect, a remote ASR node, model quality or Desktop evidence.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import re
import secrets
import signal
import traceback
import uuid

import httpx
from sqlalchemy.engine import make_url

from tests.conftest import SERVER_ROOT, TEST_DB, TEST_URL, wait_for_readiness_async
from tests.integration.test_normal_shutdown_contract import WsProbe, require_owned_database
from tests.integration.test_voice_process_restart import (
    PASSWORD, advisory_owners, bundle, complete_session, connect_control,
    fingerprint, login, new_session, receive,
)


async def child_run(pipe, parent_pid, nonce, role, prior):
    assert os.getppid() == parent_pid and multiprocessing.parent_process().pid == parent_pid
    assert re.fullmatch(r"smartvoiceinput_test_[a-zA-Z0-9_]{1,38}", TEST_DB)
    assert make_url(TEST_URL).database == os.environ["SVI_TEST_DB_NAME"] == TEST_DB
    assert os.environ["SVI_DATABASE_URL"] == TEST_URL
    from app import main
    from app.providers.asr.mock import MockAsrSession
    from app.providers.llm.mock import MockLlmProvider
    from app.services.inference_context import current_scope

    loop = asyncio.get_running_loop()
    assert type(loop).__module__ == "uvloop"
    pipe.send({"event": "owned_child", "pid": os.getpid(), "ppid": parent_pid,
        "nonce": nonce, "database": TEST_DB, "role": role, "loop_class": "uvloop.Loop"})
    targets, calls, gates = {}, [], {}
    entered = {kind: asyncio.Event() for kind in ("finalizing", "refining")}
    actual_finish, actual_generate = MockAsrSession.finish, MockLlmProvider.generate

    async def hold(kind, deadline):
        gates[kind] = {"deadline": deadline, "returned": False, "cancelled": False}
        entered[kind].set()
        try:
            # Parent never releases this gate: it must kill this exact process
            # while both original physical budgets remain positive.
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            gates[kind]["cancelled"] = True
            raise
        raise AssertionError("owned crash gate unexpectedly returned")

    async def finish(stream, samples, deadline):
        if role == "A" and stream.options.session_id == targets.get("finalizing"):
            assert samples == stream.samples == 4000
            await hold("finalizing", deadline)
        return await actual_finish(stream, samples, deadline)

    async def generate(provider, request, deadline):
        scope = current_scope.get()
        calls.append((scope.session_id if scope else None, request.purpose))
        if (role == "A" and request.purpose == "refine" and scope
                and scope.session_id == targets.get("refining")):
            assert request.call_id and scope.attempt_no == 1
            await hold("refining", deadline)
        return await actual_generate(provider, request, deadline)

    MockAsrSession.finish, MockLlmProvider.generate = finish, generate
    app = main.create_app(str(SERVER_ROOT / "config/app.yaml"))
    close, control, wires = {}, None, []
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://testserver") as http:
                await wait_for_readiness_async(http)
                assert app.state.ready and not app.state.instance_fenced
                assert app.state.config.asr.provider == app.state.config.llm.provider == "mock"
                assert app.state.config.pipeline.stop_to_result_budget_ms == 30000
                assert app.state.config.asr.finish_timeout_ms == app.state.config.llm.call_timeout_ms == 10000
                actual_close = app.state.db.close

                async def observed_close():
                    close["checkedout_before_dispose"] = app.state.db.engine.pool.checkedout()
                    await actual_close()
                    close["completed"] = True

                app.state.db.close = observed_close
                control = await connect_control()
                actors = {}
                for kind in ("finalizing", "refining"):
                    actor_nonce = nonce + "-" + kind
                    if role == "A":
                        response = await http.post("/api/auth/register", json={
                            "email": f"voice12-{actor_nonce}@example.invalid", "password": PASSWORD})
                        assert response.status_code == 201, {"status": response.status_code}
                    headers, owner = await login(http, actor_nonce)
                    actors[kind] = {"headers": headers, "owner": owner}
                assert len({value["owner"] for value in actors.values()}) == 2
                if role == "A":
                    checkpoint = {"targets": {}}
                    # Complete both histories before starting either physical
                    # gate; account hashing/setup cannot consume the ASR budget.
                    for kind, actor in actors.items():
                        history = await complete_session(app, http, actor["headers"])
                        checkpoint["targets"][kind] = {"owner": actor["owner"], "history_sid": history,
                            "history_hash": fingerprint(await bundle(control, actor["owner"], history))}
                    for kind, actor in actors.items():
                        sid = targets[kind] = await new_session(http, actor["headers"])
                        checkpoint["targets"][kind]["sid"] = sid
                        wire = await WsProbe(app, actor["headers"], sid).start()
                        wires.append(wire)
                        await wire.end()
                        wire.drain_task = asyncio.create_task(wire.drain())
                    async with asyncio.timeout(3):
                        await asyncio.gather(*(event.wait() for event in entered.values()))
                    assert await control.fetchval("SELECT count(*) FROM sessions WHERE state IN ('created','streaming','finalizing','refining')") == 2
                    for kind, actor in actors.items():
                        target = checkpoint["targets"][kind]
                        current = await bundle(control, actor["owner"], target["sid"])
                        session, attempt = current["sessions"][0], current["session_attempts"][0]
                        runtime = app.state.sessions.runtimes[target["sid"]]
                        assert session["state"] == runtime.state == kind and session["audio_complete"] is True
                        assert session["received_samples"] == runtime.samples == 4000 and session["end_accepted_at"] is not None
                        assert session["current_attempt_no"] == runtime.attempt_no == 1
                        assert attempt["state"] == "running" and attempt["ended_at"] is None
                        assert runtime.processing_accepted and runtime.deadline == runtime.end_accepted_at + 30
                        assert current["results"] == current["insertions"] == current["learning_events"] == []
                        assert not gates[kind]["returned"] and not gates[kind]["cancelled"]
                        if kind == "finalizing":
                            assert attempt["raw_text"] is None and len(runtime.audio) == 8000 and runtime.asr is not None
                            assert current["model_calls"] == []  # Mock ASR has no invented physical ledger.
                        else:
                            assert attempt["raw_text"] and len(runtime.audio) == 0 and runtime.asr is None
                            assert len(current["model_calls"]) == 1
                            assert (current["model_calls"][0]["purpose"], current["model_calls"][0]["status"]) == ("refine", "running")
                        target["bundle"] = current
                    key = app.state.config.server.instance_lock_key
                    owners = await advisory_owners(control, key)
                    assert len(owners) == 1
                    remaining = {kind: int((gate["deadline"] - loop.time()) * 1000) for kind, gate in gates.items()}
                    assert min(remaining.values()) > 0
                    pipe.send({"event": "two_active_committed_and_gated", "pid": os.getpid(),
                        "lock_key": key, "lock_backend_pid": owners[0], "remaining_physical_ms": remaining,
                        "active_count": 2, **checkpoint})
                    await asyncio.Event().wait()  # Only the retained parent SIGKILL ends A.
                else:
                    assert prior and calls == []  # Recovery must never re-dispatch inference.
                    restored = {}
                    for kind, actor in actors.items():
                        target = prior["targets"][kind]
                        assert actor["owner"] == target["owner"]
                        sid = target["sid"]
                        current = await bundle(control, actor["owner"], sid)
                        session, attempt = current["sessions"][0], current["session_attempts"][0]
                        assert session["audio_complete"] and session["received_samples"] == 4000
                        assert session["current_attempt_no"] == 1 and session["first_terminal_at"] is not None
                        assert attempt["error_code"] == "SERVER_RESTARTED" and attempt["ended_at"] is not None
                        assert sid not in app.state.sessions.runtimes
                        assert current["insertions"] == current["learning_events"] == []
                        response = await http.get(f"/api/v1/sessions/{sid}", headers=actor["headers"])
                        assert response.status_code == 200
                        body = response.json()
                        if kind == "finalizing":
                            assert (session["state"], session["error_code"], session["retry_cache_status"]) == ("failed", "SERVER_RESTARTED", "server_restarted")
                            assert attempt["state"] == "failed" and attempt["raw_text"] is None
                            assert current["results"] == current["model_calls"] == []
                            assert (body["retry_kind"], body["retry_unavailable_reason"], body["result"]) == ("none", "server_restarted", None)
                        else:
                            raw = target["bundle"]["session_attempts"][0]["raw_text"]
                            assert session["state"] == attempt["state"] == "ready" and session["error_code"] is None
                            assert len(current["results"]) == 1
                            result = current["results"][0]
                            assert result["raw_text"] == result["refined_text"] == attempt["raw_text"] == raw
                            assert result["review_required"] and result["review_reason"] == "server_restarted"
                            assert result["text_hash"] == hashlib.sha256(raw.encode()).hexdigest()
                            assert session["current_result_id"] == result["id"]
                            assert body["retry_kind"] == "refine" and body["result"]["review_required"]
                            assert len(current["model_calls"]) == 1
                            call = current["model_calls"][0]
                            assert (call["status"], call["error_code"]) == ("unknown", "SERVER_RESTARTED")
                            assert call["prompt_tokens"] is call["completion_tokens"] is None
                            assert call["ended_at"] and call["aggregated_at"]
                        assert fingerprint(await bundle(control, actor["owner"], target["history_sid"])) == target["history_hash"]
                        restored[kind] = fingerprint(current)
                        rejected = await http.post(f"/api/v1/sessions/{sid}/retry", headers=actor["headers"] | {
                            "Idempotency-Key": str(uuid.uuid4())}, json={"retry_kind": "asr"})
                        assert rejected.status_code == 409 and rejected.json()["error"]["code"] == "RETRY_UNAVAILABLE"
                        assert rejected.json()["error"]["details"]["reason"] == ("server_restarted" if kind == "finalizing" else "not_needed")
                        assert fingerprint(await bundle(control, actor["owner"], sid)) == restored[kind]
                    assert await control.fetchval("SELECT count(*) FROM sessions WHERE state IN ('created','streaming','finalizing','refining')") == 0
                    assert calls == [] and not app.state.sessions.runtimes
                    fresh = []
                    for kind, actor in actors.items():
                        fresh.append(await complete_session(app, http, actor["headers"]))
                        for old_kind, old_actor in actors.items():
                            target = prior["targets"][old_kind]
                            assert fingerprint(await bundle(control, old_actor["owner"], target["sid"])) == restored[old_kind]
                            assert fingerprint(await bundle(control, old_actor["owner"], target["history_sid"])) == target["history_hash"]
                    assert len(set(fresh)) == 2 and not set(fresh) & {item["sid"] for item in prior["targets"].values()}
                    assert calls == [(sid, purpose) for sid in fresh for purpose in ("refine",)]
                    assert not app.state.sessions.runtimes
                    assert await control.fetchval("SELECT count(*) FROM sessions WHERE state IN ('created','streaming','finalizing','refining')") == 0
                    evidence = {"active_at_crash": 2, "recovered": {"finalizing": "failed", "refining": "ready_review"},
                        "old_dispatches_on_startup": 0, "fresh_sessions_ready": 2, "fresh_llm_calls": 4,
                        "old_histories_and_recovered_rows_unchanged": True, "pcm_recovered": False,
                        "recovered_call_status": "unknown", "recovered_tokens": None}
                await control.close()
                control = None
        assert role == "B" and close == {"checkedout_before_dispose": 0, "completed": True}
        pipe.send({"event": "completed", "pid": os.getpid(), "evidence": evidence, "close": close})
    finally:
        if control is not None:
            await control.close()
        MockAsrSession.finish, MockLlmProvider.generate = actual_finish, actual_generate


def child_entry(pipe, parent_pid, nonce, role, prior):
    try:
        import uvloop
        uvloop.run(child_run(pipe, parent_pid, nonce, role, prior))
    except BaseException as error:
        frame = traceback.extract_tb(error.__traceback__)[-1]
        pipe.send({"event": "failed", "pid": os.getpid(), "exception": type(error).__name__,
            "location": {"file": os.path.basename(frame.filename), "line": frame.lineno}})
        raise SystemExit(2) from None
    finally:
        pipe.close()


def start_owned(context, processes, nonce, role, prior=None):
    parent_pipe, child_pipe = context.Pipe(duplex=False)
    process = context.Process(target=child_entry, args=(child_pipe, os.getpid(), nonce, role, prior),
        name=f"svi_cap22_crash_{os.getpid()}_{nonce}_{role}")
    process.start()
    child_pipe.close()
    processes.append((process, parent_pipe))
    greeting = receive(parent_pipe, process, "owned_child")
    assert greeting["ppid"] == os.getpid() and greeting["nonce"] == nonce
    assert greeting["database"] == TEST_DB and greeting["role"] == role and greeting["loop_class"] == "uvloop.Loop"
    return process, parent_pipe


def test_cap22_same_crash_recovers_two_active_owners(clean_database, monkeypatch, record_property):
    require_owned_database()
    assert os.name == "posix"
    nonce = uuid.uuid4().hex
    for name, value in {"SVI_DATABASE_URL": TEST_URL, "SVI_CONFIG": str(SERVER_ROOT / "config/app.yaml"),
        "SVI_INSTANCE_LOCK": "1", "SVI_ENVIRONMENT": "development", "SVI_ASR_PROVIDER": "mock",
        "SVI_LLM_PROVIDER": "mock", "WEB_CONCURRENCY": "1", "UVICORN_WORKERS": "1",
        "SVI_JWT_SECRET": secrets.token_hex(32), "SVI_IDEMPOTENCY_SECRET": secrets.token_hex(32)}.items():
        monkeypatch.setenv(name, value)
    sources = [*sorted((SERVER_ROOT / "app").rglob("*.py")), Path(__file__),
        SERVER_ROOT / "tests/integration/test_voice_process_restart.py",
        SERVER_ROOT / "tests/integration/test_normal_shutdown_contract.py",
        SERVER_ROOT / "tests/conftest.py", SERVER_ROOT / "config/app.yaml", SERVER_ROOT / "pyproject.toml", SERVER_ROOT / "uv.lock"]
    pins = {str(path.relative_to(SERVER_ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
    context, processes = multiprocessing.get_context("spawn"), []
    evidence = {"scope": "same-owned-process-SIGKILL/two-active/PG/formal-ASGI/Mock-physical", "source_sha256": pins,
        "fallback_cleanup": False}
    try:
        a, pipe_a = start_owned(context, processes, nonce, "A")
        checkpoint = receive(pipe_a, a, "two_active_committed_and_gated")
        assert checkpoint["active_count"] == 2 and min(checkpoint["remaining_physical_ms"].values()) > 0
        assert a.is_alive()
        a.kill(); a.join(5)
        assert not a.is_alive() and a.exitcode == -signal.SIGKILL

        async def inspect_after_exit(before_recovery):
            connection = await connect_control()
            try:
                async with asyncio.timeout(4):
                    while await advisory_owners(connection, checkpoint["lock_key"]):
                        await asyncio.sleep(.01)
                assert await connection.fetchval("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()") == 0
                if before_recovery:
                    for target in checkpoint["targets"].values():
                        assert fingerprint(await bundle(connection, target["owner"], target["sid"])) == fingerprint(target["bundle"])
                        assert fingerprint(await bundle(connection, target["owner"], target["history_sid"])) == target["history_hash"]
            finally:
                await connection.close()

        asyncio.run(inspect_after_exit(True))
        b, pipe_b = start_owned(context, processes, nonce, "B", checkpoint)
        assert b.pid != a.pid
        completed = receive(pipe_b, b, "completed")
        b.join(5)
        assert not b.is_alive() and b.exitcode == 0
        asyncio.run(inspect_after_exit(False))
        evidence.update(completed["evidence"], a_exitcode=a.exitcode, b_exitcode=b.exitcode,
            close=completed["close"], pg_connections_after_each_process=0, advisory_locks_after_each_process=0,
            child_loop_class="uvloop.Loop", gate_remaining_ms=checkpoint["remaining_physical_ms"])
    finally:
        for process, pipe in reversed(processes):
            if process.is_alive():
                evidence["fallback_cleanup"] = True
                assert process.name.startswith(f"svi_cap22_crash_{os.getpid()}_{nonce}_")
                process.kill(); process.join(5)
            assert not process.is_alive()
            pipe.close()
        evidence["source_unchanged"] = pins == {str(path.relative_to(SERVER_ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
        record_property("cap22_two_active_crash", json.dumps(evidence, sort_keys=True))
    assert not evidence["fallback_cleanup"] and evidence["source_unchanged"]
