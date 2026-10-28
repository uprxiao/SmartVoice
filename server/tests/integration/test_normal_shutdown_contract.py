"""Real PG/lifespan/ASGI HTTP+WS normal shutdown contract tests.

Physical provider methods are declared gated Mocks. No TCP, signal delivery,
service-manager grace, real model, microphone, or Desktop acceptance is claimed.
All state transitions originate in API/WS/production lifespan, never fake runtime
state. Shutdown exits lifespan normally; no instance fence/DB termination occurs.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import re
import struct
import sys
import uuid

import asyncpg
import httpx
import pytest

from app import main
from app.providers.asr.mock import MockAsrSession
from app.providers.llm.mock import MockLlmProvider
from app.services.inference_context import current_scope
from tests.conftest import PG_HOST, PG_PASSWORD, PG_PORT, PG_USER, SERVER_ROOT, TEST_DB, TEST_URL, wait_for_readiness_async


def require_owned_database():
    expected = (SERVER_ROOT / "tests/conftest.py").resolve()
    owners = [module for module in list(sys.modules.values())
              if isinstance(getattr(module, "__file__", None), str)
              and Path(module.__file__).resolve() == expected
              and getattr(module, "TEST_DB", None) == TEST_DB
              and getattr(module, "_CREATED_TEST_DB", False) is True]
    assert owners and re.fullmatch(r"smartvoiceinput_test_[a-zA-Z0-9_]{1,38}", TEST_DB)


def safe_status(response):
    return {"status": response.status_code, "code": response.json().get("error", {}).get("code")}


async def register(http, label):
    email, password = f"shutdown-{label}@example.invalid", "ShutdownFixture123!"
    response = await http.post("/api/auth/register", json={"email": email, "password": password})
    assert response.status_code == 201, safe_status(response)
    response = await http.post("/api/auth/login", json={
        "email": email, "password": password, "client_type": "desktop",
        "device": {"installation_id": str(uuid.uuid4()), "name": "Shutdown fixture",
                   "platform": "macos", "client_version": "0.0.1"},
    })
    assert response.status_code == 200, safe_status(response)
    return {"Authorization": "Bearer " + response.json()["access_token"]}


async def create_session(http, headers):
    request_id = str(uuid.uuid4())
    return await http.post("/api/v1/sessions", headers=headers | {"Idempotency-Key": request_id},
        json={"client_request_id": request_id, "mode": "dictation", "learning_enabled": False})


class WsProbe:
    def __init__(self, app, headers, session_id):
        self.app, self.headers, self.session_id = app, headers, session_id
        self.incoming, self.outgoing = asyncio.Queue(2), asyncio.Queue(16)
        self.events, self.close_code = [], None
        self.task = self.drain_task = None

    async def send(self, message):
        async with asyncio.timeout(2):
            await self.incoming.put(message)

    async def message(self, seconds=4):
        async with asyncio.timeout(seconds):
            return await self.outgoing.get()

    async def event(self):
        message = await self.message()
        assert message["type"] == "websocket.send", {"type": message["type"], "code": message.get("code")}
        event = json.loads(message["text"])
        self.events.append(event)
        assert event["session_id"] == self.session_id
        return event

    async def start(self):
        path = f"/ws/v1/sessions/{self.session_id}/audio"
        scope = {"type": "websocket", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1", "scheme": "ws", "root_path": "", "path": path,
            "raw_path": path.encode(), "query_string": b"", "state": {}, "subprotocols": [],
            "headers": [(b"host", b"testserver"), (b"authorization", self.headers["Authorization"].encode())],
            "client": ("127.0.0.1", 50000), "server": ("testserver", 80),
            "extensions": {"websocket.http.response": {}}}
        self.task = asyncio.create_task(self.app(scope, self.incoming.get, self.outgoing.put))
        await self.send({"type": "websocket.connect"})
        accepted = await self.message()
        assert accepted["type"] == "websocket.accept", {"type": accepted["type"], "status": accepted.get("status")}
        assert (await self.event())["type"] == "ready"
        # Valid synthetic PCM; it proves transport/sample accounting, not speech.
        await self.send({"type": "websocket.receive", "bytes": struct.pack("<II", 0, 4000) + struct.pack("<h", 100) * 4000})
        ack = await self.event()
        assert ack["type"] == "audio.ack" and ack["seq"] == 0 and ack["total_samples"] == 4000
        return self

    async def end(self):
        await self.send({"type": "websocket.receive", "text": json.dumps({
            "type": "audio.end", "last_seq": 0, "total_samples": 4000})})
        ack = await self.event()
        assert ack["type"] == "audio.end_ack" and ack["last_seq"] == 0 and ack["total_samples"] == 4000

    async def drain(self):
        async with asyncio.timeout(35):  # Harness bound, not a replacement product deadline.
            while True:
                message = await self.outgoing.get()
                if message["type"] == "websocket.close":
                    self.close_code = message["code"]
                    break
                assert message["type"] == "websocket.send", {"type": message["type"]}
                event = json.loads(message["text"])
                assert event["session_id"] == self.session_id
                self.events.append(event)
            await self.task  # Propagate ASGI errors/cancellation; never accept them as success.


async def snapshot(control, session_id):
    session = await control.fetchrow("""SELECT id,state,error_code,audio_complete,received_samples,
        current_attempt_no,current_result_id,retry_cache_status,end_accepted_at
        FROM sessions WHERE id=$1""", session_id)
    attempt = await control.fetchrow("""SELECT attempt_no,state,error_code,ended_at
        FROM session_attempts WHERE session_id=$1 ORDER BY attempt_no DESC LIMIT 1""", session_id)
    results = await control.fetch("""SELECT id,review_required,review_reason,char_count,stop_to_result_ms
        FROM results WHERE session_id=$1 ORDER BY attempt_no""", session_id)
    calls = await control.fetch("""SELECT purpose,status,error_code,prompt_tokens,completion_tokens
        FROM model_calls WHERE session_id=$1 ORDER BY started_at,id""", session_id)
    contribution = await control.fetchrow("""SELECT outcome,has_result,successful_audio_samples,received_samples
        FROM session_contributions WHERE session_id=$1""", session_id)
    return {"session": dict(session), "attempt": dict(attempt), "results": [dict(r) for r in results],
            "calls": [dict(r) for r in calls], "contribution": dict(contribution) if contribution else None}


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["asr_finish", "llm_refine"])
async def test_normal_shutdown_drains_complete_end_and_fails_only_unfinished_stream(
    clean_database, monkeypatch, record_property, stage,
):
    require_owned_database()
    for name, value in {
        "SVI_DATABASE_URL": TEST_URL, "SVI_CONFIG": str(SERVER_ROOT / "config/app.yaml"),
        "SVI_INSTANCE_LOCK": "1", "SVI_ENVIRONMENT": "development", "SVI_ASR_PROVIDER": "mock",
        "SVI_LLM_PROVIDER": "mock", "WEB_CONCURRENCY": "1", "UVICORN_WORKERS": "1",
    }.items():
        monkeypatch.setenv(name, value)
    loop = asyncio.get_running_loop()
    gate_entered, gate_release = asyncio.Event(), asyncio.Event()
    stop, started, shutdown_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    target = {"session_id": None}
    observed = {"stage": stage, "gate_cancelled": False, "gate_returned": False,
                "db_disposed": False, "pool_checkedout_before_dispose": None}
    trace = []
    origin = loop.time()

    def note(name, **safe_fields):
        trace.append({"event": name, "elapsed_ms": round((loop.time() - origin) * 1000, 3), **safe_fields})

    async def gate(deadline):
        observed["physical_deadline"] = deadline
        gate_entered.set()
        note("physical_mock_gate_entered", remaining_ms=round((deadline-loop.time())*1000, 3))
        try:
            await gate_release.wait()
        except asyncio.CancelledError:
            observed["gate_cancelled"] = True
            note("physical_mock_cancelled")
            raise
        assert loop.time() < deadline, "harness released gate after unchanged physical-call deadline"
        observed["gate_returned"] = True
        note("physical_mock_released")

    actual_finish, actual_generate = MockAsrSession.finish, MockLlmProvider.generate

    async def gated_finish(stream, total_samples, deadline):
        if stage == "asr_finish" and stream.options.session_id == target["session_id"]:
            assert total_samples == stream.samples == 4000 and stream.options.attempt_no == 1
            await gate(deadline)
        return await actual_finish(stream, total_samples, deadline)

    async def gated_generate(provider, request, deadline):
        scope = current_scope.get()
        if stage == "llm_refine" and request.purpose == "refine" and scope and scope.session_id == target["session_id"]:
            assert request.call_id and scope.attempt_no == 1  # Formal Scheduled provider/ledger dispatch.
            await gate(deadline)
        return await actual_generate(provider, request, deadline)

    monkeypatch.setattr(MockAsrSession, "finish", gated_finish)
    monkeypatch.setattr(MockLlmProvider, "generate", gated_generate)
    actual_close = main._close_instance_resources

    async def observe_shutdown(app, lock_conn, lock_task):
        note("normal_shutdown_entered", instance_fenced=app.state.instance_fenced)
        shutdown_entered.set()
        return await actual_close(app, lock_conn, lock_task)

    monkeypatch.setattr(main, "_close_instance_resources", observe_shutdown)
    app = main.create_app(str(SERVER_ROOT / "config/app.yaml"))

    async def lifespan_owner():
        async with app.router.lifespan_context(app):
            started.set()
            await stop.wait()  # Normal context exit. No task.cancel or _fence_instance on passing path.

    parent = asyncio.create_task(lifespan_owner())
    streams, control = [], None
    try:
        async with asyncio.timeout(8):
            await started.wait()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://testserver") as health_client:
                await wait_for_readiness_async(health_client)
        assert app.state.instance_fenced is False and app.state.ready
        assert app.state.config.pipeline.stop_to_result_budget_ms == 30000
        assert app.state.config.asr.finish_timeout_ms == app.state.config.llm.call_timeout_ms == 10000
        original_db_close = app.state.db.close

        async def observe_db_close():
            observed["pool_checkedout_before_dispose"] = app.state.db.engine.pool.checkedout()
            await original_db_close()
            observed["db_disposed"] = True

        monkeypatch.setattr(app.state.db, "close", observe_db_close)
        control = await asyncpg.connect(host=PG_HOST, port=PG_PORT, user=PG_USER,
                                       password=PG_PASSWORD, database=TEST_DB)
        assert await control.fetchval("SELECT current_database()") == TEST_DB
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as http:
            headers = [await register(http, str(index)) for index in range(3)]
            ids = []
            for auth in headers[:2]:
                response = await create_session(http, auth)
                assert response.status_code == 201, safe_status(response)
                ids.append(response.json()["session_id"])
                probe = WsProbe(app, auth, ids[-1])
                streams.append(probe)
                await probe.start()
            target["session_id"] = ids[0]
            complete, streaming = streams
            await complete.end()
            async with asyncio.timeout(4):
                await gate_entered.wait()
            runtime = app.state.sessions.runtimes[ids[0]]
            other_runtime = app.state.sessions.runtimes[ids[1]]
            old_deadline = runtime.deadline
            assert runtime.ended and runtime.state == ("finalizing" if stage == "asr_finish" else "refining")
            assert not other_runtime.ended and other_runtime.state == "streaming"
            assert old_deadline - runtime.end_accepted_at == pytest.approx(30)
            before = await snapshot(control, ids[0])
            assert before["session"]["audio_complete"] is True and before["session"]["received_samples"] == 4000
            assert before["attempt"]["state"] == "running" and not before["results"]
            for probe in streams:
                probe.drain_task = asyncio.create_task(probe.drain())
            stopped_at = loop.time()
            stop.set()
            async with asyncio.timeout(3):
                await shutdown_entered.wait()
                while app.state.ready:
                    await asyncio.sleep(.005)
            ready = await http.get("/health/ready")
            rejected_create = await create_session(http, headers[2])
            rejected_retry = await http.post(f"/api/v1/sessions/{ids[0]}/retry",
                headers=headers[0] | {"Idempotency-Key": str(uuid.uuid4())}, json={"retry_kind": "refine"})
            observed["ready_status"] = ready.status_code
            observed["create_rejection"] = safe_status(rejected_create)
            observed["retry_rejection"] = safe_status(rejected_retry)
            assert runtime.deadline == old_deadline  # Read only; never reset/shorten product clock.
            gate_release.set()
            async with asyncio.timeout(35):
                await asyncio.gather(*(probe.drain_task for probe in streams))
                await parent
            observed["shutdown_ms"] = round((loop.time() - stopped_at)*1000, 3)
            after = [await snapshot(control, session_id) for session_id in ids]
            observed["db_outcomes"] = [{"state": item["session"]["state"], "code": item["session"]["error_code"],
                "attempt_state": item["attempt"]["state"], "results": len(item["results"]),
                "model_calls": item["calls"]} for item in after]
            observed["ws"] = [{"types": [e["type"] for e in probe.events], "close": probe.close_code,
                "errors": [e["error"]["code"] for e in probe.events if e["type"] == "error"]} for probe in streams]
            observed["instance_fenced"] = app.state.instance_fenced
            note("normal_shutdown_finished")
            # Record safe real observations before contract assertions, retaining baseline failure evidence.
            record_property("shutdown_contract_observation", json.dumps(observed, sort_keys=True))
            record_property("shutdown_contract_trace", json.dumps(trace, sort_keys=True))
            assert ready.status_code == 503
            assert observed["create_rejection"] == observed["retry_rejection"] == {"status": 503, "code": "SERVICE_UNAVAILABLE"}
            assert observed["gate_returned"] and not observed["gate_cancelled"]
            assert app.state.instance_fenced is False, "normal shutdown must not be relabeled as lost-lock fence"
            assert after[0]["session"]["state"] == after[0]["attempt"]["state"] == "ready"
            assert after[0]["session"]["error_code"] is None and len(after[0]["results"]) == 1
            assert after[0]["results"][0]["review_required"] is False
            assert after[0]["results"][0]["stop_to_result_ms"] <= 30000
            assert after[0]["contribution"] == {"outcome": "ready", "has_result": True,
                "successful_audio_samples": 4000, "received_samples": 4000}
            assert sum(e["type"] == "result.ready" for e in complete.events) == 1
            assert not any(e["type"] == "error" for e in complete.events) and complete.close_code == 1000
            assert [(c["purpose"], c["status"]) for c in after[0]["calls"]] == [("refine", "succeeded")]
            assert after[1]["session"]["state"] == after[1]["attempt"]["state"] == "failed"
            assert after[1]["session"]["error_code"] == after[1]["attempt"]["error_code"] == "SERVER_SHUTDOWN"
            assert after[1]["session"]["audio_complete"] is False and not after[1]["results"]
            assert after[1]["contribution"] == {"outcome": "failed", "has_result": False,
                "successful_audio_samples": 0, "received_samples": 4000}
            assert not after[1]["calls"] and streaming.close_code == 1011
            assert any(e["type"] == "error" and e["error"]["code"] == "SERVER_SHUTDOWN" for e in streaming.events)
            assert not any(e["type"] == "result.ready" for e in streaming.events)
            assert all(not r.audio and r.asr is None for r in (runtime, other_runtime))
            assert app.state.sessions.runtimes == {} and not app.state.sessions.background_tasks
            assert observed["pool_checkedout_before_dispose"] == 0 and observed["db_disposed"]
            assert all(probe.task.done() and not probe.task.cancelled() for probe in streams)
            key = app.state.config.server.instance_lock_key % (1 << 64)
            assert await control.fetchval("""SELECT count(*) FROM pg_locks WHERE locktype='advisory'
                AND database=(SELECT oid FROM pg_database WHERE datname=current_database())
                AND classid::bigint=$1 AND objid::bigint=$2 AND objsubid=1""", key >> 32, key & 0xffffffff) == 0
    finally:
        # Failure cleanup only. It is never counted as a normal-shutdown result.
        gate_release.set()
        stop.set()
        owned = [parent] + [task for probe in streams for task in (probe.task, probe.drain_task) if task]
        pending = [task for task in owned if not task.done()]
        if pending:
            _, pending_set = await asyncio.wait(pending, timeout=36)
            for task in pending_set:
                if not task.cancelling():
                    task.cancel()
            if pending_set:
                _, remaining = await asyncio.wait(pending_set, timeout=4)
                record_property("shutdown_cleanup_pending", len(remaining))
                assert not remaining, "owned test cleanup did not finish; failure, never pass"
        for task in owned:
            if task.done() and not task.cancelled():
                task.exception()  # Observe failures without logging possible request details.
        if control is not None:
            await control.close()
        record_property("shutdown_final_cleanup", json.dumps({
            "owned_tasks_done": all(task.done() for task in owned),
            "normal_parent_cancelled": parent.cancelled(),
            "db_disposed": observed["db_disposed"],
            "pool_checkedout_before_dispose": observed["pool_checkedout_before_dispose"],
        }, sort_keys=True))
        record_property("shutdown_final_trace", json.dumps(trace, sort_keys=True))
