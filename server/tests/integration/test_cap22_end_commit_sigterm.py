"""G2 end-only: real PG commit visibility, SIGTERM/TCP, same-database restart.

An already accepted G1 keeper preserves the shutdown503 observation window.
No ACK-delivery requirement is used as a substitute for durable acceptance.
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
from tests.integration.test_cap22_create_commit_sigterm import ready, no_owned_connections, group_gone
from tests.integration.test_cap22_sigterm_tcp import until, probe, open_stream, end_stream, drain
from tests.integration.test_normal_shutdown_contract import register, create_session, require_owned_database, safe_status, snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["after_commit_before_publish"])
async def test_cap22_end_commit_publish_sigterm(clean_database, tmp_path, record_property, stage):
    require_owned_database()
    assert PG_HOST in {"127.0.0.1", "localhost", "::1"}
    child_script = Path(__file__).with_name("cap22_end_commit_child.py")
    helpers = ["cap22_sigterm_child.py", "test_cap22_sigterm_tcp.py",
        "test_cap22_create_commit_sigterm.py", "test_normal_shutdown_contract.py"]
    sources = [*sorted((SERVER_ROOT / "app").rglob("*.py")), child_script, Path(__file__),
        *(Path(__file__).with_name(name) for name in helpers), SERVER_ROOT / "tests/conftest.py",
        SERVER_ROOT / "config/app.yaml", SERVER_ROOT / "pyproject.toml", SERVER_ROOT / "uv.lock"]
    pins = {str(p.relative_to(SERVER_ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
    env = os.environ | {"SVI_DATABASE_URL": TEST_URL, "SVI_CONFIG": str(SERVER_ROOT / "config/app.yaml"),
        "SVI_INSTANCE_LOCK": "1", "SVI_ENVIRONMENT": "development", "SVI_ASR_PROVIDER": "mock",
        "SVI_LLM_PROVIDER": "mock", "WEB_CONCURRENCY": "1", "UVICORN_WORKERS": "1",
        "SVI_JWT_SECRET": secrets.token_hex(32), "SVI_IDEMPOTENCY_SECRET": secrets.token_hex(32),
        "PYTHONPATH": str(SERVER_ROOT)}
    observed = {"stage": stage, "scope": "owned TCP/SIGTERM/PG; synthetic PCM/Mock", "starts": [],
        "fallback_cleanup": False, "source_sha256": pins, "contract_failures": []}
    children, logs, wires, readers = [], [], [], []
    control = None
    first_folder = tmp_path / "first"
    first_folder.mkdir(mode=0o700)

    def check(condition, label):
        if not condition:
            observed["contract_failures"].append(label)

    def normal_result(value, label):
        check(value["session"]["state"] == value["attempt"]["state"] == "ready", label + ":ready")
        check(value["session"]["audio_complete"] and value["session"]["received_samples"] == 4000, label + ":audio")
        check(value["session"]["current_attempt_no"] == value["attempt"]["attempt_no"] == 1, label + ":identity")
        check(len(value["results"]) == 1 and not value["results"][0]["review_required"]
            and value["results"][0]["stop_to_result_ms"] <= 30000, label + ":normal_result_budget")
        check([(r["purpose"], r["status"]) for r in value["calls"]]
            == [("refine", "succeeded")], label + ":actual_mock_calls")

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
        async with asyncio.timeout(45):
            code = await asyncio.to_thread(child.wait)
        final = json.loads((folder / "child-final.json").read_text())
        extra = json.loads((folder / "end-observer-final.json").read_text())
        assert code == 0 and final["server_returned"] and final["stopping"] and not final["fenced"]
        assert final["pool"].get("before_dispose") == 0
        assert final["runtime_count"] == final["background_count"] == final["processing_count"] == 0
        assert all(r["pcm_bytes"] == 0 and not r["asr_attached"] and r["processing_done"] for r in final["runtime_refs"])
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
            auth = [await register(http, uuid.uuid4().hex) for _ in range(2)]
            ids = []
            for headers in auth:
                response = await create_session(http, headers)
                assert response.status_code == 201, safe_status(response)
                ids.append(response.json()["session_id"])
                wires.append(await open_stream(base, headers, ids[-1]))
            target_sid, keeper_sid = ids
            (first_folder / "gate-session").write_text(keeper_sid)
            await end_stream(wires[1])
            readers.append(asyncio.create_task(drain(wires[1])))
            await until((first_folder / "gate-entered.json").exists, 2)
            owner = await control.fetchval("SELECT user_id FROM sessions WHERE id=$1", target_sid)
            initial = await snapshot(control, target_sid)
            assert initial["session"]["state"] == "streaming" and not initial["session"]["audio_complete"]
            (first_folder / "end-target.json").write_text(json.dumps({"stage": stage,
                "session_id": target_sid, "user_id": owner}))
            await wires[0].send(json.dumps({"type": "audio.end", "last_seq": 0, "total_samples": 4000}))
            target_reader = asyncio.create_task(drain(wires[0]))
            readers.append(target_reader)
            await until((first_folder / "end-gate-entered.json").exists, 2)
            gate = json.loads((first_folder / "end-gate-entered.json").read_text())
            visible = await snapshot(control, target_sid)
            committed = stage == "after_commit_before_publish"
            assert gate["committed"] is committed and gate["in_transaction"] is not committed
            assert gate["operation_count"] >= 1 and not gate["end_ack_emitted"]
            assert visible["session"]["state"] == ("finalizing" if committed else "streaming")
            assert visible["session"]["audio_complete"] is committed
            assert (visible["session"]["end_accepted_at"] is not None) is committed
            assert visible["attempt"]["attempt_no"] == 1 and visible["session"]["received_samples"] == 4000
            observed.update(gate=gate, independent_pg_at_gate=visible)
            child.send_signal(signal.SIGTERM)
            await until((first_folder / "signal-observed.json").exists, 1)
            async with httpx.AsyncClient(base_url=base, timeout=.3, trust_env=False) as probes:
                health, deadline = [], time.monotonic() + .5
                while True:
                    health.append(await probe(probes, "GET", "/health/ready"))
                    if health[-1].get("status") != 200 or time.monotonic() >= deadline:
                        break
                    await asyncio.sleep(.01)
                key = str(uuid.uuid4())
                refused = await probe(probes, "POST", "/api/v1/sessions",
                    headers=auth[0] | {"Idempotency-Key": key},
                    json={"client_request_id": key, "mode": "dictation", "learning_enabled": False})
            observed.update(health=health, refused_create=refused)
            check(health[-1].get("status") == 503 and health[-1].get("reason_code") == "SERVER_SHUTDOWN", "actual_cutoff503")
            check(refused.get("status") == 503 and refused.get("code") == "SERVICE_UNAVAILABLE", "actual_create503")
            assert child.poll() is None  # Keeper is still inside its original physical budget.
            (first_folder / "end-release").touch(exist_ok=False)
            (first_folder / "release").touch(exist_ok=False)
            async with asyncio.timeout(45):
                observed["ws"] = await asyncio.gather(*readers)
            first = await join(child, first_folder)
            assert not (first_folder / "gate-control-failure.json").exists()
            assert not (first_folder / "end-control-failure.json").exists()
            frozen = await snapshot(control, target_sid)
            keeper = await snapshot(control, keeper_sid)
            normal_result(keeper, "keeper")
            target_finishes = [f for f in first["observer"]["finishes"] if f["session_id"] == target_sid]
            if committed:
                # S2 acceptance was independently visible BEFORE SIGTERM.
                # A private flag or absent ACK cannot discard that upload.
                normal_result(frozen, "target_complete")
                check(len(target_finishes) == 1, "complete_exactly_one_asr_finish")
            else:
                check(frozen["session"]["state"] == frozen["attempt"]["state"] == "failed", "unaccepted_failed")
                check(frozen["session"]["error_code"] == frozen["attempt"]["error_code"] == "SERVER_SHUTDOWN", "unaccepted_shutdown")
                check(not frozen["results"] and not frozen["calls"] and not target_finishes, "unaccepted_no_late_provider")
            assert await control.fetchval("SELECT count(*) FROM sessions") == 2
            assert await control.fetchval("SELECT count(*) FROM operation_requests") == 0
            for table in ("insertions", "learning_events"):
                assert await control.fetchval(f"SELECT count(*) FROM {table}") == 0
            observed.update(first=first, terminal_target=frozen, terminal_keeper=keeper)

        # Even a contract failure above is retained while normal-restart
        # postconditions run. The final assertion reports the original failures.
        restart_folder = tmp_path / "restart"
        restart_folder.mkdir(mode=0o700)
        successor, base = start(restart_folder)
        async with httpx.AsyncClient(base_url=base, timeout=2, trust_env=False) as http:
            startup = []
            observed["starts"].append(startup)
            await ready(http, successor, startup)
            check(await snapshot(control, target_sid) == frozen, "restart_old_target_unchanged")
            response = await create_session(http, auth[0])
            assert response.status_code == 201, safe_status(response)
            fresh_sid = response.json()["session_id"]
            wire = await open_stream(base, auth[0], fresh_sid)
            wires.append(wire)
            await end_stream(wire)
            async with asyncio.timeout(35):
                await drain(wire)
            successful = await snapshot(control, fresh_sid)
            normal_result(successful, "restart")
            check(await snapshot(control, target_sid) == frozen, "fresh_does_not_change_old_target")
            successor.send_signal(signal.SIGTERM)
            second = await join(successor, restart_folder)
            assert not second["observer"]["used"]
            assert [f["session_id"] for f in second["observer"]["finishes"]] == [fresh_sid]
            observed.update(restart=second, restart_result=successful, experiment_completed=True)
            assert not observed["contract_failures"], observed["contract_failures"]
    finally:
        primary = sys.exc_info()[0]
        observed["primary_exception_type"] = None if primary is None else primary.__name__
        for marker in ("end-release", "release"):
            (first_folder / marker).touch(exist_ok=True)
        for child in children:
            if child.poll() is None:
                observed["fallback_cleanup"] = True
                child.terminate()
                try:
                    await asyncio.wait_for(asyncio.to_thread(child.wait), 40)
                except TimeoutError:
                    child.kill()
                    await asyncio.wait_for(asyncio.to_thread(child.wait), 5)
        await asyncio.gather(*(wire.close() for wire in wires), return_exceptions=True)
        await asyncio.gather(*readers, return_exceptions=True)
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
        record_property("sigterm_g2_end", json.dumps(observed, default=str, sort_keys=True))
        if primary is None:
            assert observed["source_unchanged"] and observed["process_groups_gone"] and not observed["fallback_cleanup"]
            assert observed.get("tail") == {"connections": 0, "advisory_locks": 0} and "tail_error_type" not in observed
