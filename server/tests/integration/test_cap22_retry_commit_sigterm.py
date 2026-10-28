"""G2 ASR retry: real TCP/PG/SIGTERM and restart, two COMMIT boundaries.

Post-COMMIT is already accepted according to DB authority, even if the Python
return/handoff is held. It must drain within the unchanged retry/total/D budget.
Pre-COMMIT is not accepted at cutoff: no late retry provider dispatch is allowed.
Mock inference + synthetic PCM; no real-model, natural-600 or crash-ACK claim.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
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

from tests.conftest import PG_HOST, PG_PASSWORD, PG_PORT, PG_USER, SERVER_ROOT, TEST_DB, TEST_URL
from tests.integration.test_cap22_create_commit_sigterm import ready, no_owned_connections, group_gone
from tests.integration.test_cap22_sigterm_tcp import until, probe, open_stream, end_stream, drain
from tests.integration.test_normal_shutdown_contract import register, create_session, require_owned_database, safe_status, snapshot


async def facts(control, owner, sid, key):
    identity = await control.fetchrow("""SELECT id,user_id,device_id,created_login_sid,
        client_request_id,request_hash,first_asr_failed_at,first_terminal_at
        FROM sessions WHERE user_id=$1 AND id=$2""", owner, sid)
    attempts = await control.fetch("""SELECT attempt_no,kind,source_attempt_no,state,error_code,
        started_at,ended_at,raw_text IS NOT NULL AS raw_present
        FROM session_attempts WHERE user_id=$1 AND session_id=$2 ORDER BY attempt_no""", owner, sid)
    keys = await control.fetch("""SELECT operation,request_key,target_type,target_id,request_hash
        FROM operation_requests WHERE user_id=$1 AND operation='session.retry' AND request_key=$2""", owner, key)
    return {"identity": dict(identity), "attempts": [dict(row) for row in attempts],
        "keys": [dict(row) for row in keys], "snapshot": await snapshot(control, sid)}


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["after_commit_before_publish"])
async def test_cap22_asr_retry_commit_sigterm(clean_database, tmp_path, record_property, stage):
    require_owned_database()
    assert PG_HOST in {"127.0.0.1", "localhost", "::1"}
    child_script = Path(__file__).with_name("cap22_retry_commit_child.py")
    helper_names = ["cap22_sigterm_child.py", "test_cap22_sigterm_tcp.py",
        "test_cap22_create_commit_sigterm.py", "test_normal_shutdown_contract.py"]
    sources = [*sorted((SERVER_ROOT / "app").rglob("*.py")), child_script, Path(__file__),
        *(Path(__file__).with_name(name) for name in helper_names), SERVER_ROOT / "tests/conftest.py",
        SERVER_ROOT / "config/app.yaml", SERVER_ROOT / "pyproject.toml", SERVER_ROOT / "uv.lock"]
    pins = {str(p.relative_to(SERVER_ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
    env = os.environ | {"SVI_DATABASE_URL": TEST_URL, "SVI_CONFIG": str(SERVER_ROOT / "config/app.yaml"),
        "SVI_INSTANCE_LOCK": "1", "SVI_ENVIRONMENT": "development", "SVI_ASR_PROVIDER": "mock",
        "SVI_LLM_PROVIDER": "mock", "WEB_CONCURRENCY": "1", "UVICORN_WORKERS": "1",
        "SVI_JWT_SECRET": secrets.token_hex(32), "SVI_IDEMPOTENCY_SECRET": secrets.token_hex(32),
        "PYTHONPATH": str(SERVER_ROOT), "SVI_CAP22_GATE_STAGE": "asr_finish"}
    post = stage == "after_commit_before_publish"
    observed = {"stage": stage, "scope": "owned SIGTERM/TCP/PG; synthetic PCM/Mock",
        "post_commit_accepted_by_database": post, "fallback_cleanup": False,
        "starts": [], "source_sha256": pins}
    children, logs, wires, request_tasks, failures = [], [], [], [], []
    control = None
    first_folder = tmp_path / "first"
    first_folder.mkdir(mode=0o700)
    key = str(uuid.uuid4())
    pcm_hash = hashlib.sha256(struct.pack("<h", 100) * 4000).hexdigest()

    def check(ok, label):
        if not ok:
            failures.append(label)

    def start(folder):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        base = f"http://127.0.0.1:{listener.getsockname()[1]}"
        log = os.fdopen(os.open(folder / "child.private.log", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb")
        logs.append(log)
        try:
            child = subprocess.Popen([sys.executable, str(child_script), str(folder), str(listener.fileno())],
                cwd=SERVER_ROOT, env=env, pass_fds=(listener.fileno(),), start_new_session=True,
                stdout=log, stderr=log)
        finally:
            listener.close()
        children.append(child)
        return child, base

    async def join(child, folder):
        async with asyncio.timeout(45):  # Original G1 supervision; not a new business SLA.
            code = await asyncio.to_thread(child.wait)
        final = json.loads((folder / "child-final.json").read_text())
        extra = json.loads((folder / "retry-observer-final.json").read_text())
        assert code == 0 and final["server_returned"] and final["stopping"] and not final["fenced"]
        assert final["pool"].get("before_dispose") == 0
        assert final["runtime_count"] == final["background_count"] == final["processing_count"] == 0
        assert extra["operation_count"] == extra["websocket_count"] == extra["closing_runtime_count"] == extra["auth_task_count"] == 0
        assert all(r["pcm_bytes"] == 0 and not r["asr_attached"] and r["processing_done"] for r in extra["runtime_refs"])
        assert not any(extra["known_pcm_bytes"])
        assert all(not lease["owner_present"] and not lease["timer_present"] for lease in extra["leases"])
        assert group_gone(child)
        tail = await no_owned_connections(control)
        assert tail == {"connections": 0, "advisory_locks": 0}
        return {"final": final, "observer": extra, "tail": tail}

    try:
        control = await asyncpg.connect(host=PG_HOST, port=PG_PORT, user=PG_USER,
            password=PG_PASSWORD, database=TEST_DB, timeout=5, command_timeout=5)
        assert await control.fetchval("SELECT current_database()") == TEST_DB
        child, base = start(first_folder)
        async with httpx.AsyncClient(base_url=base, timeout=2, trust_env=False) as http:
            startup = []
            observed["starts"].append(startup)
            await ready(http, child, startup)
            headers = await register(http, uuid.uuid4().hex)
            owner = await control.fetchval("SELECT id FROM users")
            created = await create_session(http, headers)
            assert created.status_code == 201, safe_status(created)
            sid = created.json()["session_id"]
            (first_folder / "fail-session").write_text(sid)
            wire = await open_stream(base, headers, sid)
            wires.append(wire)
            await end_stream(wire)
            async with asyncio.timeout(4):
                failed_wire = await drain(wire)
            failed = await http.get(f"/api/v1/sessions/{sid}", headers=headers)
            assert failed.status_code == 200
            data = failed.json()
            assert data["state"] == "failed" and data["audio_complete"] and data["retry_kind"] == "asr"
            expiry = datetime.fromisoformat(data["retry_expires_at"].replace("Z", "+00:00")).timestamp()
            original = await facts(control, owner, sid, key)
            assert expiry == original["identity"]["first_asr_failed_at"].timestamp() + 600
            assert original["snapshot"]["session"]["retry_cache_status"] == "available"
            assert original["snapshot"]["session"]["received_samples"] == 4000
            assert original["snapshot"]["attempt"]["error_code"] == "ASR_FAILED"
            assert len(original["attempts"]) == 1 and not original["keys"]
            assert not original["snapshot"]["results"] and not original["snapshot"]["calls"]
            # Remove only this fixture failure selector: retry uses actual Mock success.
            (first_folder / "fail-session").unlink()
            pending = first_folder / "retry-target.pending"
            pending.write_text(json.dumps({"stage": stage, "user_id": owner, "session_id": sid,
                "request_key": key, "expiry_epoch": expiry}))
            os.link(pending, first_folder / "retry-target.json")
            pending.unlink()
            body = {"retry_kind": "asr"}
            request = asyncio.create_task(http.post(f"/api/v1/sessions/{sid}/retry", timeout=12,
                headers=headers | {"Idempotency-Key": key}, json=body))
            request_tasks.append(request)
            await until((first_folder / "retry-gate-entered.json").exists, 2)
            gate = json.loads((first_folder / "retry-gate-entered.json").read_text())
            assert gate["user_id"] == owner and gate["session_id"] == sid and gate["request_key"] == key
            assert gate["attempt_no"] == 2 and gate["operation_count"] >= 1 and not request.done()
            assert gate["runtime_published"] and not gate["task_present"] and gate["same_pcm_and_lease"]
            assert gate["pcm_bytes"] == 8000 and gate["previous_pcm_bytes"] == 0 and gate["pcm_sha256"] == pcm_hash
            assert gate["expiry_epoch"] == expiry and gate["committed"] is post and gate["in_transaction"] is not post
            assert gate["total_deadline_loop"] == gate["end_accepted_at_loop"] + 30
            assert gate["asr_deadline_loop"] == min(gate["end_accepted_at_loop"] + 10, gate["lease_deadline_loop"])
            visible = await facts(control, owner, sid, key)  # Independent actual PG visibility.
            assert visible["identity"] == original["identity"]
            assert visible["snapshot"]["session"]["current_attempt_no"] == (2 if post else 1)
            assert visible["snapshot"]["session"]["state"] == ("finalizing" if post else "failed")
            assert len(visible["attempts"]) == (2 if post else 1) and len(visible["keys"]) == int(post)
            if post:
                assert visible["attempts"][-1]["kind"] == "asr" and visible["attempts"][-1]["source_attempt_no"] == 1
                assert visible["attempts"][-1]["state"] == "running"
                assert visible["keys"][0]["target_id"] == sid
            observed.update(original=original, failed_ws=failed_wire, gate=gate, visible=visible)
            child.send_signal(signal.SIGTERM)
            await until((first_folder / "signal-observed.json").exists, 1)
            async with httpx.AsyncClient(base_url=base, timeout=.3, trust_env=False) as probes:
                health, deadline = [], time.monotonic() + .5
                while True:
                    health.append(await probe(probes, "GET", "/health/ready"))
                    if health[-1].get("status") != 200 or time.monotonic() >= deadline:
                        break
                    await asyncio.sleep(.01)
                fresh_key, retry_key = str(uuid.uuid4()), str(uuid.uuid4())
                create, retry = await asyncio.gather(
                    probe(probes, "POST", "/api/v1/sessions", headers=headers | {"Idempotency-Key": fresh_key},
                        json={"client_request_id": fresh_key, "mode": "dictation", "learning_enabled": False}),
                    probe(probes, "POST", f"/api/v1/sessions/{sid}/retry",
                        headers=headers | {"Idempotency-Key": retry_key}, json=body))
            observed.update(health=health, refused_create=create, refused_retry=retry)
            assert health[-1].get("status") == 503 and health[-1].get("reason_code") == "SERVER_SHUTDOWN"
            assert all(item.get("status") == 503 and item.get("code") == "SERVICE_UNAVAILABLE" for item in (create, retry))
            assert child.poll() is None and not request.done()
            (first_folder / "retry-release").touch(exist_ok=False)
            response = await request
            observed["retry_response"] = safe_status(response)
            check(response.status_code == (202 if post else 503), "held retry response follows durable pre-cutoff acceptance")
            if response.status_code == 503:
                check(response.json()["error"]["code"] == "SERVICE_UNAVAILABLE", "rejected retry canonical code")
            first = await join(child, first_folder)
            assert not (first_folder / "retry-control-failure.json").exists()
            released = json.loads((first_folder / "retry-gate-release.json").read_text())
            assert released["closing"] and released["operation_count"] >= 1
            assert released["original_expiry_unchanged"] and released["original_total_deadline_unchanged"]
            assert first["observer"]["commit_completed"]
            frozen = await facts(control, owner, sid, key)
            observed.update(first=first, release=released, terminal=frozen)
            assert frozen["identity"] == original["identity"] and frozen["attempts"][0] == original["attempts"][0]
            assert len(frozen["attempts"]) == 2 and len(frozen["keys"]) == 1
            assert frozen["snapshot"]["session"]["current_attempt_no"] == 2
            assert frozen["snapshot"]["session"]["received_samples"] == 4000 and frozen["snapshot"]["session"]["audio_complete"]
            if post:
                check(frozen["snapshot"]["session"]["state"] == frozen["attempts"][-1]["state"] == "ready", "committed retry drains ready")
                check(len(frozen["snapshot"]["results"]) == 1, "one committed retry result")
                check(frozen["attempts"][-1]["raw_present"], "retry ASR raw actually persisted")
                check(await control.fetchval("SELECT count(*) FROM results WHERE user_id=$1 AND session_id=$2 AND attempt_no=2", owner, sid) == 1,
                    "result belongs to the committed second attempt")
                check([(c["purpose"], c["status"]) for c in frozen["snapshot"]["calls"]] == [("refine", "succeeded")], "actual retry refine/audit success")
                check(await control.fetchval("SELECT count(*) FROM model_calls WHERE user_id=$1 AND session_id=$2 AND attempt_no=2", owner, sid) == 1,
                    "actual LLM dispatch identities belong to retry attempt2")
                if frozen["snapshot"]["results"]:
                    result = frozen["snapshot"]["results"][0]
                    check(not result["review_required"] and result["stop_to_result_ms"] <= 30000, "original total budget and ordinary result")
            else:
                check(frozen["snapshot"]["session"]["state"] == frozen["attempts"][-1]["state"] == "failed", "uncommitted retry not admitted at cutoff")
                check(frozen["snapshot"]["session"]["error_code"] == frozen["attempts"][-1]["error_code"] == "SERVER_SHUTDOWN", "unaccepted retry shutdown reason")
                check(not frozen["snapshot"]["results"] and not frozen["snapshot"]["calls"], "unaccepted retry no result/model dispatch")
            expected_opens = [{"session_id": sid, "attempt_no": 1}] + ([{"session_id": sid, "attempt_no": 2}] if post else [])
            check(first["observer"]["provider_opens"] == expected_opens, "only previously committed retry may dispatch after cutoff")
            check(first["observer"]["feeds"] == [dict(item, start=0, samples=4000, sha256=pcm_hash) for item in expected_opens], "exact retained PCM no new audio upload")
            finishes = first["observer"]["finishes"]
            check(len(finishes) == int(post), "one physical retry finish iff pre-cutoff committed")
            if finishes:
                check(finishes[0]["attempt_no"] == 2 and finishes[0]["nonempty_final"] and finishes[0]["samples"] == 4000,
                    "actual retry final has correct attempt and audio")
                check(finishes[0]["returned_loop"] < finishes[0]["deadline_loop"] <= gate["asr_deadline_loop"], "retry finish original 10/D deadline")
            assert await control.fetchval("SELECT count(*) FROM sessions") == 1
            assert await control.fetchval("SELECT count(*) FROM operation_requests") == 1
            for table in ("insertions", "learning_events"):
                assert await control.fetchval(f"SELECT count(*) FROM {table}") == 0

        restart_folder = tmp_path / "restart"
        restart_folder.mkdir(mode=0o700)
        successor, base = start(restart_folder)
        async with httpx.AsyncClient(base_url=base, timeout=2, trust_env=False) as http:
            startup = []
            observed["starts"].append(startup)
            await ready(http, successor, startup)
            assert await facts(control, owner, sid, key) == frozen
            replay = await http.post(f"/api/v1/sessions/{sid}/retry", headers=headers | {"Idempotency-Key": key}, json=body)
            assert replay.status_code == 200 and replay.json()["replayed"] is True, safe_status(replay)
            assert replay.json()["session_id"] == sid and replay.json()["current_attempt_no"] == 2
            check(replay.json()["state"] == ("ready" if post else "failed"), "restart same-key replay preserves expected outcome")
            assert replay.json()["retry_kind"] == "none" and replay.json()["retry_expires_at"] is None
            new_retry = await http.post(f"/api/v1/sessions/{sid}/retry", headers=headers | {"Idempotency-Key": str(uuid.uuid4())}, json=body)
            assert new_retry.status_code == 409 and new_retry.json()["error"]["code"] == "RETRY_UNAVAILABLE", safe_status(new_retry)
            assert await facts(control, owner, sid, key) == frozen
            assert await control.fetchval("SELECT count(*) FROM operation_requests") == 1
            created = await create_session(http, headers)
            assert created.status_code == 201, safe_status(created)
            fresh_sid = created.json()["session_id"]
            wire = await open_stream(base, headers, fresh_sid)
            wires.append(wire)
            await end_stream(wire)
            async with asyncio.timeout(35):
                successful_wire = await drain(wire)
            successful = await snapshot(control, fresh_sid)
            assert successful_wire["close_code"] == 1000
            assert [event["type"] for event in successful_wire["events"]] == ["asr.final", "result.ready"]
            assert successful["session"]["state"] == successful["attempt"]["state"] == "ready" and len(successful["results"]) == 1
            assert successful["session"]["received_samples"] == 4000 and not successful["results"][0]["review_required"]
            assert successful["results"][0]["stop_to_result_ms"] <= 30000
            assert await facts(control, owner, sid, key) == frozen and await control.fetchval("SELECT count(*) FROM sessions") == 2
            successor.send_signal(signal.SIGTERM)
            second = await join(successor, restart_folder)
            assert not second["observer"]["gate_used"]
            assert second["observer"]["provider_opens"] == [{"session_id": fresh_sid, "attempt_no": 1}]
            observed.update(restart=second, restart_success=successful, completed=True)
        assert not failures, failures
    finally:
        primary = sys.exc_info()[0]
        observed.update(primary_exception_type=None if primary is None else primary.__name__, failures=failures)
        (first_folder / "retry-release").touch(exist_ok=True)
        for child in children:
            if child.poll() is None:
                observed["fallback_cleanup"] = True
                child.terminate()
                try:
                    await asyncio.wait_for(asyncio.to_thread(child.wait), 40)
                except TimeoutError:
                    child.kill()
                    await asyncio.wait_for(asyncio.to_thread(child.wait), 5)
        await asyncio.gather(*request_tasks, return_exceptions=True)
        await asyncio.gather(*(wire.close() for wire in wires), return_exceptions=True)
        for log in logs:
            log.close()
        if control is not None:
            try:
                observed["tail"] = await no_owned_connections(control)
            except BaseException as error:
                observed["tail_error_type"] = type(error).__name__
            finally:
                await control.close()
        observed["process_groups_gone"] = all(group_gone(child) for child in children)
        observed["source_unchanged"] = all(hashlib.sha256(p.read_bytes()).hexdigest() == pins[str(p.relative_to(SERVER_ROOT))] for p in sources)
        record_property("sigterm_g2_retry", json.dumps(observed, default=str, sort_keys=True))
        if primary is None:
            assert observed["source_unchanged"] and observed["process_groups_gone"] and not observed["fallback_cleanup"]
            assert observed.get("tail") == {"connections": 0, "advisory_locks": 0} and "tail_error_type" not in observed
