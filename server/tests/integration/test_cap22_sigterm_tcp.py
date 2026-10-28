"""CAP22 G1 ASR/refine arms: actual owned Uvicorn/TCP/SIGTERM/PG, Mock inference.

No manual begin_close/ready changes; no real model/microphone/OS ASR worker claim.
Run in a fresh pytest process so conftest CREATEs its unique owned database.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import struct
import subprocess
import sys
import time
import uuid

import asyncpg
import httpx
import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from tests.conftest import PG_HOST, PG_PASSWORD, PG_PORT, PG_USER, SERVER_ROOT, TEST_DB, TEST_URL
from tests.integration.test_normal_shutdown_contract import (
    create_session, register, require_owned_database, safe_status, snapshot,
)


async def until(predicate, seconds):
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(.005)


async def open_stream(base, headers, sid):
    ws = await connect(base.replace("http:", "ws:") + f"/ws/v1/sessions/{sid}/audio",
        additional_headers=headers, open_timeout=2, close_timeout=1)
    async with asyncio.timeout(2):
        ready = json.loads(await ws.recv())
        assert ready["type"] == "ready" and ready["session_id"] == sid
        await ws.send(struct.pack("<II", 0, 4000) + struct.pack("<h", 100) * 4000)
        ack = json.loads(await ws.recv())
        assert ack["type"] == "audio.ack" and ack["seq"] == 0 and ack["total_samples"] == 4000
    return ws


async def end_stream(ws):
    async with asyncio.timeout(2):
        await ws.send(json.dumps({"type": "audio.end", "last_seq": 0, "total_samples": 4000}))
        ack = json.loads(await ws.recv())
        assert ack["type"] == "audio.end_ack" and ack["last_seq"] == 0 and ack["total_samples"] == 4000


async def drain(ws):
    events = []
    try:
        async for message in ws:
            event = json.loads(message)
            events.append({"type": event["type"], "error_code": event.get("error", {}).get("code")})
    except ConnectionClosed:
        pass  # Preserve actual close code below; not a synthetic success event.
    return {"close_code": ws.close_code, "events": events}


async def probe(http, method, path, **kwargs):
    started = time.monotonic()
    try:
        response = await http.request(method, path, **kwargs)
        body = response.json()
        return {"status": response.status_code, "code": body.get("error", {}).get("code"),
            "reason_code": body.get("reason_code"), "at": started}
    except httpx.TransportError as exc:
        return {"transport_error": type(exc).__name__, "at": started}


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["asr_finish", "llm_refine"])
async def test_cap22_owned_sigterm_complete_end_and_streaming_asr(clean_database, tmp_path, record_property, stage):
    require_owned_database()
    assert PG_HOST in {"127.0.0.1", "localhost", "::1"}
    folder = tmp_path / "owned-sigterm"
    folder.mkdir(mode=0o700)
    child_script = Path(__file__).with_name("cap22_sigterm_child.py")
    env = os.environ | {"SVI_DATABASE_URL": TEST_URL, "SVI_CONFIG": str(SERVER_ROOT / "config/app.yaml"),
        "SVI_INSTANCE_LOCK": "1", "SVI_ENVIRONMENT": "development", "SVI_ASR_PROVIDER": "mock",
        "SVI_LLM_PROVIDER": "mock", "WEB_CONCURRENCY": "1", "UVICORN_WORKERS": "1",
        "SVI_JWT_SECRET": secrets.token_hex(32), "SVI_IDEMPOTENCY_SECRET": secrets.token_hex(32),
        "PYTHONPATH": str(SERVER_ROOT), "SVI_CAP22_GATE_STAGE": stage}
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    base = f"http://127.0.0.1:{listener.getsockname()[1]}"
    log = os.fdopen(os.open(folder / "child.private.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb")
    child = None
    control = None
    sockets, drains = [], []
    observed = {"scope": "real SIGTERM/TCP/PG; Mock provider boundary only", "stage": stage, "fallback_cleanup": False,
                "startup_attempts": []}
    inputs = [*sorted((SERVER_ROOT / "app").rglob("*.py")), SERVER_ROOT / "config/app.yaml", child_script,
        Path(__file__), SERVER_ROOT / "tests/conftest.py", SERVER_ROOT / "tests/integration/test_normal_shutdown_contract.py",
        SERVER_ROOT / "pyproject.toml", SERVER_ROOT / "uv.lock"]
    before = {str(p.relative_to(SERVER_ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs}
    record_property("sigterm_g1_source_sha256", json.dumps(before, sort_keys=True))
    try:
        child = subprocess.Popen([sys.executable, str(child_script), str(folder), str(listener.fileno())],
            cwd=SERVER_ROOT, env=env, pass_fds=(listener.fileno(),), start_new_session=True, stdout=log, stderr=log)
        listener.close()
        control = await asyncpg.connect(host=PG_HOST, port=PG_PORT, user=PG_USER,
            password=PG_PASSWORD, database=TEST_DB, timeout=5, command_timeout=5)
        assert await control.fetchval("SELECT current_database()") == TEST_DB
        async with httpx.AsyncClient(base_url=base, timeout=2, trust_env=False) as http:
            async with asyncio.timeout(8):
                while True:
                    assert child.poll() is None, "owned child exited before readiness"
                    attempt_started, attempt = time.monotonic(), {}
                    try:
                        ready = await http.get("/health/ready")
                        attempt["status"] = ready.status_code
                        if ready.status_code == 200:
                            break
                        attempt["reason_code"] = ready.json().get("reason_code")
                        assert ready.status_code == 503 and attempt["reason_code"] == "STARTING"
                    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                        attempt["transport_error_type"] = type(exc).__name__
                    finally:
                        attempt["elapsed_ms"] = round((time.monotonic() - attempt_started) * 1000, 3)
                        if sys.exc_info()[0] is not None:
                            attempt["propagated_exception_type"] = sys.exc_info()[0].__name__
                        observed["startup_attempts"].append(attempt)
                    await asyncio.sleep(.01)
            auth = [await register(http, uuid.uuid4().hex) for _ in range(3)]
            # Third account is only a pre-existing eligible retry target, not an
            # extra active WS at SIGTERM. Both activity actors remain occupied.
            response = await create_session(http, auth[2])
            assert response.status_code == 201, safe_status(response)
            retry_id = response.json()["session_id"]
            (folder / "fail-session").write_text(retry_id)
            failed_ws = await open_stream(base, auth[2], retry_id)
            sockets.append(failed_ws)
            await end_stream(failed_ws)
            async with asyncio.timeout(4):
                await drain(failed_ws)
            failure = await http.get(f"/api/v1/sessions/{retry_id}", headers=auth[2])
            assert failure.status_code == 200
            assert failure.json()["state"] == "failed" and failure.json()["audio_complete"] is True
            assert failure.json()["retry_kind"] == "asr" and failure.json()["retry_expires_at"] is not None
            retry_before = await snapshot(control, retry_id)
            assert retry_before["session"]["retry_cache_status"] == "available"
            ids = []
            for headers in auth[:2]:
                response = await create_session(http, headers)
                assert response.status_code == 201, safe_status(response)
                ids.append(response.json()["session_id"])
                sockets.append(await open_stream(base, headers, ids[-1]))
            (folder / "gate-session").write_text(ids[0])
            await end_stream(sockets[1])
            await until((folder / "gate-entered.json").exists, 2)
            entered = json.loads((folder / "gate-entered.json").read_text())
            if (folder / "gate-control-failure.json").exists():
                raise AssertionError(json.loads((folder / "gate-control-failure.json").read_text())["code"])
            pre = [await snapshot(control, sid) for sid in ids]
            assert pre[0]["session"]["audio_complete"] and pre[0]["session"]["state"] == (
                "finalizing" if stage == "asr_finish" else "refining")
            assert pre[1]["session"]["state"] == "streaming" and not pre[1]["session"]["audio_complete"]
            if stage == "llm_refine":
                # Independent PG read: the gate is after raw COMMIT and the
                # scheduled ledger's real started dispatch, not a fake phase.
                raw = await control.fetchrow("""SELECT s.user_id,s.current_attempt_no,a.raw_text,
                    c.purpose,c.status,c.ended_at,c.aggregated_at
                    FROM sessions s JOIN session_attempts a
                      ON a.user_id=s.user_id AND a.session_id=s.id AND a.attempt_no=s.current_attempt_no
                    JOIN model_calls c ON c.user_id=s.user_id AND c.session_id=s.id
                      AND c.attempt_no=a.attempt_no
                    WHERE s.id=$1 AND s.user_id=$2 AND a.attempt_no=$3 AND c.id=$4""",
                    ids[0], entered["user_id"], entered["attempt_no"], entered["call_id"])
                assert raw is not None and isinstance(raw["raw_text"], str) and raw["raw_text"]
                assert hashlib.sha256(raw["raw_text"].encode()).hexdigest() == entered["raw_sha256"]
                assert str(raw["user_id"]) == entered["user_id"] and raw["current_attempt_no"] == 1
                assert raw["purpose"] == "refine" and raw["status"] == "running"
                assert raw["ended_at"] is None and raw["aggregated_at"] is None
                observed["refine_pre_signal"] = {"raw_persisted": True, "request_raw_sha256_matches": True,
                    "scope_and_call_id_match": True, "actual_dispatch_running": True}
                raw = None
            drains = [asyncio.create_task(drain(ws)) for ws in sockets[1:]]
            observed["signal_sent_at"] = time.monotonic()
            child.send_signal(signal.SIGTERM)  # No application lifecycle/state call before this.
            await until((folder / "signal-observed.json").exists, 1)
            # Observe the actual cutoff: a refused connection is never relabelled
            # 503. Repeated health reads avoid treating a pre-shutdown 200 race
            # as an application shutdown response. No mutating probe before it.
            async with httpx.AsyncClient(base_url=base, timeout=.3, trust_env=False) as probes:
                health = []
                deadline = time.monotonic() + .5
                while True:
                    health.append(await probe(probes, "GET", "/health/ready"))
                    if health[-1].get("status") != 200 or time.monotonic() >= deadline:
                        break
                    await asyncio.sleep(.01)
                key = str(uuid.uuid4())
                create, retry = await asyncio.gather(
                    probe(probes, "POST", "/api/v1/sessions", headers=auth[0] | {"Idempotency-Key": key},
                        json={"client_request_id": key, "mode": "dictation", "learning_enabled": False}),
                    probe(probes, "POST", f"/api/v1/sessions/{retry_id}/retry",
                        headers=auth[2] | {"Idempotency-Key": str(uuid.uuid4())}, json={"retry_kind": "asr"}))
            observed.update(health=health, create=create, retry=retry, gate_entered=entered)
            (folder / "release").touch(exist_ok=False)
            # Supervision guard only: not an added product latency criterion.
            async with asyncio.timeout(45):
                observed["ws"] = await asyncio.gather(*drains)
                observed["returncode"] = await asyncio.to_thread(child.wait)
            after = [await snapshot(control, sid) for sid in ids]
            final = json.loads((folder / "child-final.json").read_text())
            observed.update(final=final, sessions=after,
                gate_released=(folder / "gate-released.json").exists(), gate_cancelled=(folder / "gate-cancelled.json").exists())
            observed["child_binding"] = json.loads((folder / "child-binding.json").read_text())
            observed["phases"] = {name: json.loads((folder / name).read_text()) for name in (
                "signal-observed.json", "shutdown-entered.json", "gate-released.json", "gate-cancelled.json",
                "gate-control-failure.json")
                if (folder / name).exists()}
            retry_after = await snapshot(control, retry_id)
            observed["retry_attempts_before_after"] = [retry_before["session"]["current_attempt_no"], retry_after["session"]["current_attempt_no"]]
            record_property("sigterm_g1_observation", json.dumps(observed, default=str, sort_keys=True))
            assert "gate-control-failure.json" not in observed["phases"], (
                "CONTROL_GATE_MISSED_ORIGINAL_ASR_DEADLINE" if stage == "asr_finish"
                else "CONTROL_GATE_MISSED_ORIGINAL_LLM_DEADLINE")
            failures = []
            def check(ok, label):
                if not ok:
                    failures.append(label)
            check(health[-1].get("status") == 503, "readiness must return HTTP503, not transport error")
            check(health[-1].get("reason_code") == "SERVER_SHUTDOWN", "readiness shutdown reason")
            for name, item in [("create", create), ("retry", retry)]:
                check(item.get("status") == 503 and item.get("code") == "SERVICE_UNAVAILABLE", name + " must return canonical503")
            check(observed["retry_attempts_before_after"] == [1, 1], "refused retry must not create attempt")
            check(await control.fetchval("SELECT count(*) FROM sessions") == 3, "refused create must not create session")
            check(child.returncode == 0 and final["server_returned"] and not final["fenced"], "normal owned exit")
            check(observed["gate_released"] and not observed["gate_cancelled"],
                "complete ASR must drain" if stage == "asr_finish" else "complete refine must drain")
            if stage == "llm_refine":
                check(observed["child_binding"]["gate_stage"] == stage, "actual refine arm binding")
                outcome = await control.fetchrow("""SELECT status,ended_at,aggregated_at FROM model_calls
                    WHERE id=$1 AND user_id=$2 AND session_id=$3 AND attempt_no=$4 AND purpose='refine'""",
                    entered["call_id"], entered["user_id"], ids[0], entered["attempt_no"])
                check(outcome is not None and outcome["status"] == "succeeded"
                    and outcome["ended_at"] is not None and outcome["aggregated_at"] is not None,
                    "same actual refine call completed and aggregated")
            complete, streaming = after
            check(complete["session"]["state"] == complete["attempt"]["state"] == "ready", "complete end ready")
            check(complete["session"]["current_attempt_no"] == complete["attempt"]["attempt_no"] == 1
                and complete["session"]["audio_complete"] and complete["session"]["received_samples"] == 4000,
                "complete original identity/audio")
            check(len(complete["results"]) == 1 and not complete["results"][0]["review_required"], "single normal result")
            check(bool(complete["results"]) and complete["results"][0]["stop_to_result_ms"] <= 30000, "original result budget")
            check([(r["purpose"], r["status"]) for r in complete["calls"]] == [("refine", "succeeded")], "Mock actual LLM ledger")
            check(streaming["session"]["state"] == streaming["attempt"]["state"] == "failed", "stream failed")
            check(streaming["session"]["error_code"] == streaming["attempt"]["error_code"] == "SERVER_SHUTDOWN", "stream shutdown reason")
            check(not streaming["results"] and not streaming["calls"], "incomplete zero result/LLM")
            check(complete["contribution"] == {"outcome": "ready", "has_result": True,
                "successful_audio_samples": 4000, "received_samples": 4000}, "complete contribution")
            check(streaming["contribution"] == {"outcome": "failed", "has_result": False,
                "successful_audio_samples": 0, "received_samples": 4000}, "stream contribution")
            check(final["pool"].get("before_dispose") == 0 and final["runtime_count"] == final["background_count"] == final["processing_count"] == 0, "owned resource cleanup")
            check(all(r["pcm_bytes"] == 0 and not r["asr_attached"] and r["processing_done"] for r in final["runtime_refs"]), "retained PCM/processing refs")
            observed["contract_failures"] = failures
            record_property("sigterm_g1_contract_failures", json.dumps(failures))
            assert not failures, failures
    finally:
        primary_type = sys.exc_info()[0]
        observed["primary_exception_type"] = primary_type.__name__ if primary_type else None
        (folder / "release").touch(exist_ok=True)
        if child and child.poll() is None:
            observed["fallback_cleanup"] = True
            child.terminate()
            try:
                await asyncio.wait_for(asyncio.to_thread(child.wait), 40)
            except TimeoutError:
                child.kill()
                await asyncio.wait_for(asyncio.to_thread(child.wait), 5)
        await asyncio.gather(*(ws.close() for ws in sockets), return_exceptions=True)
        await asyncio.gather(*drains, return_exceptions=True)
        listener.close()
        log.close()
        if control:
            remaining = await control.fetchval("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()")
            locks = await control.fetchval("SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND database=(SELECT oid FROM pg_database WHERE datname=current_database())")
            observed.update(remaining_connections=remaining, advisory_locks=locks)
            await control.close()
        if child:
            try:
                os.killpg(child.pid, 0)
                observed["process_group_gone"] = False
            except ProcessLookupError:
                observed["process_group_gone"] = True
        observed["source_unchanged"] = all(hashlib.sha256(p.read_bytes()).hexdigest() == before[str(p.relative_to(SERVER_ROOT))] for p in inputs)
        record_property("sigterm_g1_startup_observation", json.dumps(observed["startup_attempts"], sort_keys=True))
        record_property("sigterm_g1_final_cleanup", json.dumps({k: observed.get(k) for k in (
            "primary_exception_type", "fallback_cleanup", "remaining_connections", "advisory_locks", "process_group_gone", "source_unchanged")}, sort_keys=True))
        if primary_type is None:  # Do not replace the primary contract/control failure.
            assert observed["source_unchanged"] and observed.get("remaining_connections") == observed.get("advisory_locks") == 0
            assert observed.get("process_group_gone") and not observed["fallback_cleanup"]
