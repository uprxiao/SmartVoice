"""G2 create-only: actual TCP/SIGTERM/PG; two commit visibility boundaries.

Reuse G1 owner child and transport helpers. Synthetic PCM/Mock inference only.
The COMMIT-return gate is a controlled Python delivery delay, not TCP ACK loss.
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
import subprocess
import sys
import time
import uuid

import asyncpg
import httpx
import pytest

from tests.conftest import PG_HOST, PG_PASSWORD, PG_PORT, PG_USER, SERVER_ROOT, TEST_DB, TEST_URL
from tests.integration.test_cap22_sigterm_tcp import until, probe, open_stream, end_stream, drain
from tests.integration.test_normal_shutdown_contract import register, require_owned_database, safe_status, snapshot


async def ready(http, child, observations):
    async with asyncio.timeout(8):  # Original G1 startup observation guard.
        while True:
            assert child.poll() is None
            try:
                response = await http.get("/health/ready")
                item = {"status": response.status_code, "reason_code": response.json().get("reason_code")}
                observations.append(item)
                if response.status_code == 200:
                    return
                assert item == {"status": 503, "reason_code": "STARTING"}
            except (httpx.ConnectError, httpx.ConnectTimeout) as error:
                observations.append({"transport_error_type": type(error).__name__})
            await asyncio.sleep(.01)


async def no_owned_connections(control):
    connections = await control.fetchval("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()")
    locks = await control.fetchval("SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND database=(SELECT oid FROM pg_database WHERE datname=current_database())")
    return {"connections": connections, "advisory_locks": locks}


def group_gone(child):
    try:
        os.killpg(child.pid, 0)
    except ProcessLookupError:
        return True
    return False


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["before_commit", "after_commit_before_publish"])
async def test_cap22_create_commit_publish_sigterm(clean_database, tmp_path, record_property, stage):
    require_owned_database()
    assert PG_HOST in {"127.0.0.1", "localhost", "::1"}
    child_script = Path(__file__).with_name("cap22_create_commit_child.py")
    helpers = ["cap22_sigterm_child.py", "test_cap22_sigterm_tcp.py", "test_normal_shutdown_contract.py"]
    sources = [*sorted((SERVER_ROOT / "app").rglob("*.py")), child_script, Path(__file__),
        *(Path(__file__).with_name(name) for name in helpers), SERVER_ROOT / "tests/conftest.py",
        SERVER_ROOT / "config/app.yaml", SERVER_ROOT / "pyproject.toml", SERVER_ROOT / "uv.lock"]
    pins = {str(p.relative_to(SERVER_ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
    env = os.environ | {"SVI_DATABASE_URL": TEST_URL, "SVI_CONFIG": str(SERVER_ROOT / "config/app.yaml"),
        "SVI_INSTANCE_LOCK": "1", "SVI_ENVIRONMENT": "development", "SVI_ASR_PROVIDER": "mock",
        "SVI_LLM_PROVIDER": "mock", "WEB_CONCURRENCY": "1", "UVICORN_WORKERS": "1",
        "SVI_JWT_SECRET": secrets.token_hex(32), "SVI_IDEMPOTENCY_SECRET": secrets.token_hex(32),
        "PYTHONPATH": str(SERVER_ROOT)}
    observed = {"stage": stage, "scope": "real owned SIGTERM/TCP/PG; synthetic PCM/Mock",
        "fallback_cleanup": False, "starts": [], "source_sha256": pins}
    children, logs, wires, request_tasks = [], [], [], []
    control = None
    first_folder = tmp_path / "first"
    first_folder.mkdir(mode=0o700)
    key, target_sid, headers, frozen = str(uuid.uuid4()), None, None, None

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
        async with asyncio.timeout(45):  # G1 supervision only; not a new product shutdown SLA.
            code = await asyncio.to_thread(child.wait)
        final = json.loads((folder / "child-final.json").read_text())
        extra = json.loads((folder / "create-observer-final.json").read_text())
        assert code == 0 and final["server_returned"] and final["stopping"] and not final["fenced"]
        assert final["pool"].get("before_dispose") == 0
        assert final["runtime_count"] == final["background_count"] == final["processing_count"] == 0
        assert all(r["pcm_bytes"] == 0 and not r["asr_attached"] and r["processing_done"] for r in extra["runtime_refs"])
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
            (first_folder / "create-target.json").write_text(json.dumps({"stage": stage,
                "user_id": owner, "request_key": key}))
            body = {"client_request_id": key, "mode": "dictation", "learning_enabled": False}
            request = asyncio.create_task(http.post("/api/v1/sessions", timeout=8,
                headers=headers | {"Idempotency-Key": key}, json=body))
            request_tasks.append(request)
            await until((first_folder / "create-gate-entered.json").exists, 2)
            gate = json.loads((first_folder / "create-gate-entered.json").read_text())
            target_sid = gate["session_id"]
            assert gate["user_id"] == owner and gate["request_key"] == key
            assert gate["operation_count"] >= 1 and not gate["runtime_published"] and not request.done()
            committed = stage == "after_commit_before_publish"
            assert gate["committed"] is committed and gate["in_transaction"] is not committed
            counts = await control.fetchrow("""SELECT
                (SELECT count(*) FROM sessions WHERE user_id=$1::varchar AND id=$2::varchar) AS sessions,
                (SELECT count(*) FROM session_attempts WHERE user_id=$1::varchar AND session_id=$2::varchar) AS attempts,
                (SELECT count(*) FROM operation_requests WHERE user_id=$1::varchar) AS operations""", owner, target_sid)
            assert dict(counts) == {"sessions": int(committed), "attempts": int(committed), "operations": 0}
            identity_sql = "SELECT id,user_id,device_id,created_login_sid,client_request_id,request_hash FROM sessions WHERE user_id=$1::varchar AND id=$2::varchar"
            before_identity = await control.fetchrow(identity_sql, owner, target_sid)
            observed.update(gate=gate, visible_counts=dict(counts))
            child.send_signal(signal.SIGTERM)
            await until((first_folder / "signal-observed.json").exists, 1)
            async with httpx.AsyncClient(base_url=base, timeout=.3, trust_env=False) as probes:
                health, deadline = [], time.monotonic() + .5
                while True:
                    health.append(await probe(probes, "GET", "/health/ready"))
                    if health[-1].get("status") != 200 or time.monotonic() >= deadline:
                        break
                    await asyncio.sleep(.01)
                new_key = str(uuid.uuid4())
                refused = await probe(probes, "POST", "/api/v1/sessions",
                    headers=headers | {"Idempotency-Key": new_key},
                    json=body | {"client_request_id": new_key})
            observed.update(health=health, refused_create=refused)
            assert health[-1].get("status") == 503 and health[-1].get("reason_code") == "SERVER_SHUTDOWN"
            assert refused.get("status") == 503 and refused.get("code") == "SERVICE_UNAVAILABLE"
            assert child.poll() is None and not request.done()  # Drain owns the admitted operation.
            (first_folder / "create-release").touch(exist_ok=False)
            response = await request
            assert response.status_code == 503 and response.json()["error"]["code"] == "SERVICE_UNAVAILABLE", safe_status(response)
            first = await join(child, first_folder)
            assert not (first_folder / "create-control-failure.json").exists()
            released = json.loads((first_folder / "create-gate-release.json").read_text())
            assert released["closing"] and released["operation_count"] >= 1
            assert first["observer"]["commit_completed"] and first["observer"]["provider_opens"] == []
            frozen = await snapshot(control, target_sid)
            assert frozen["session"]["state"] == frozen["attempt"]["state"] == "failed"
            assert frozen["session"]["error_code"] == frozen["attempt"]["error_code"] == "SERVER_SHUTDOWN"
            assert frozen["session"]["current_attempt_no"] == frozen["attempt"]["attempt_no"] == 1
            assert frozen["session"]["received_samples"] == 0 and not frozen["session"]["audio_complete"]
            assert not frozen["results"] and not frozen["calls"] and frozen["session"]["current_result_id"] is None
            assert await control.fetchval("SELECT count(*) FROM sessions") == 1
            identity = await control.fetchrow(identity_sql, owner, target_sid)
            assert identity["client_request_id"] == key
            if before_identity is not None:
                assert identity == before_identity
            assert await control.fetchval("SELECT count(*) FROM operation_requests") == 0
            for table in ("insertions", "learning_events"):
                assert await control.fetchval(f"SELECT count(*) FROM {table}") == 0
            observed["first"] = first
            observed["committed_failure"] = frozen

        # Same environment/secrets and exact database/source; new owned PID/port.
        restart_folder = tmp_path / "restart"
        restart_folder.mkdir(mode=0o700)
        successor, base = start(restart_folder)
        async with httpx.AsyncClient(base_url=base, timeout=2, trust_env=False) as http:
            startup = []
            observed["starts"].append(startup)
            await ready(http, successor, startup)
            assert await snapshot(control, target_sid) == frozen
            replay = await http.post("/api/v1/sessions", headers=headers | {"Idempotency-Key": key}, json=body)
            assert replay.status_code == 200 and replay.json()["replayed"] is True, safe_status(replay)
            assert replay.json()["session_id"] == target_sid and replay.json()["state"] == "failed"
            assert await snapshot(control, target_sid) == frozen
            fresh_key = str(uuid.uuid4())
            result = await http.post("/api/v1/sessions", headers=headers | {"Idempotency-Key": fresh_key},
                json=body | {"client_request_id": fresh_key})
            assert result.status_code == 201, safe_status(result)
            sid = result.json()["session_id"]
            wire = await open_stream(base, headers, sid)
            wires.append(wire)
            await end_stream(wire)
            async with asyncio.timeout(35):
                wire_result = await drain(wire)
            successful = await snapshot(control, sid)
            assert wire_result["close_code"] == 1000
            assert [e["type"] for e in wire_result["events"]] == ["asr.final", "result.ready"]
            assert successful["session"]["state"] == successful["attempt"]["state"] == "ready"
            assert successful["session"]["received_samples"] == 4000 and len(successful["results"]) == 1
            assert not successful["results"][0]["review_required"] and successful["results"][0]["stop_to_result_ms"] <= 30000
            assert [(r["purpose"], r["status"]) for r in successful["calls"]] == [("refine", "succeeded")]
            assert await snapshot(control, target_sid) == frozen and await control.fetchval("SELECT count(*) FROM sessions") == 2
            successor.send_signal(signal.SIGTERM)
            second = await join(successor, restart_folder)
            assert not second["observer"]["gate_used"]
            assert second["observer"]["provider_opens"] == [{"session_id": sid, "attempt_no": 1}]
            observed.update(restart=second, restart_success=successful, completed=True)
    finally:
        primary = sys.exc_info()[0]
        observed["primary_exception_type"] = None if primary is None else primary.__name__
        (first_folder / "create-release").touch(exist_ok=True)
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
        observed["source_unchanged"] = all(hashlib.sha256(p.read_bytes()).hexdigest() == sha for p, sha in ((p, pins[str(p.relative_to(SERVER_ROOT))]) for p in sources))
        record_property("sigterm_g2_create", json.dumps(observed, default=str, sort_keys=True))
        if primary is None:
            assert observed["source_unchanged"] and observed["process_groups_gone"] and not observed["fallback_cleanup"]
            assert observed.get("tail") == {"connections": 0, "advisory_locks": 0} and "tail_error_type" not in observed
