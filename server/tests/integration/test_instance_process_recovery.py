"""Real TCP/process/PG tests; only a fresh fixture-owned database is mutated."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys

import asyncpg
import httpx
import pytest

from tests.conftest import PG_HOST, PG_PASSWORD, PG_PORT, PG_USER, SERVER_ROOT, TEST_DB, TEST_URL
from tests.integration.test_instance_recovery_loss import require_process_owned_database
from tests.integration.test_normal_shutdown_contract import register, create_session
from tests.integration.test_cap22_sigterm_tcp import open_stream


async def ready(http, child):
    async with asyncio.timeout(12):
        while True:
            assert child.poll() is None, "child exited before ready; inspect owned child log"
            try:
                result = await http.get("/health/ready")
                if result.status_code == 200:
                    return
                assert result.status_code == 503 and result.json()["reason_code"] == "STARTING"
            except httpx.ConnectError:
                pass
            await asyncio.sleep(.02)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,reason", [("terminate", "probe_error"), ("unlock", "lock_missing"), ("timeout", "probe_timeout")])
async def test_real_owner_failure_exits_and_successor_recovers(clean_database, tmp_path, mode, reason):
    require_process_owned_database()
    folder = tmp_path / mode
    folder.mkdir()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    env = os.environ | {"SVI_DATABASE_URL": TEST_URL, "SVI_CONFIG": str(SERVER_ROOT / "config/app.yaml"),
        "SVI_INSTANCE_LOCK": "1", "SVI_ENVIRONMENT": "development", "SVI_ASR_PROVIDER": "mock",
        "SVI_LLM_PROVIDER": "mock", "SVI_HOST": "127.0.0.1", "SVI_PORT": str(port),
        "WEB_CONCURRENCY": "1", "UVICORN_WORKERS": "1", "PYTHONPATH": str(SERVER_ROOT),
        "SVI_JWT_SECRET": secrets.token_hex(32), "SVI_IDEMPOTENCY_SECRET": secrets.token_hex(32)}
    # A child never inherits the test runner's systemd ownership notification.
    for name in ("NOTIFY_SOCKET", "WATCHDOG_PID", "WATCHDOG_USEC"):
        env.pop(name, None)
    child = successor = ws = control = None
    path = Path(__file__).with_name("instance_process_child.py")
    with (folder / "child.log").open("wb") as log:
        try:
            child = subprocess.Popen([sys.executable, str(path), str(folder), mode], cwd=SERVER_ROOT,
                env=env, start_new_session=True, stdout=log, stderr=log)
            control = await asyncpg.connect(host=PG_HOST, port=PG_PORT, user=PG_USER,
                password=PG_PASSWORD, database=TEST_DB, command_timeout=5)
            assert await control.fetchval("SELECT current_database()") == TEST_DB
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=2, trust_env=False) as http:
                await ready(http, child)
                assert (await http.get("/api/v1/history")).status_code == 401
                headers = await register(http, secrets.token_hex(8))
                created = await create_session(http, headers)
                assert created.status_code == 201
                sid = created.json()["session_id"]
                ws = await open_stream(str(http.base_url).rstrip("/"), headers, sid)
                owners = await control.fetch("""SELECT a.pid,a.backend_start FROM pg_stat_activity a
                    WHERE a.datname=$1 AND EXISTS(SELECT 1 FROM pg_locks l WHERE l.pid=a.pid
                    AND l.locktype='advisory' AND l.granted AND l.objid=420017863 AND l.classid=0)""", TEST_DB)
                assert len(owners) == 1
                owner = owners[0]
                if mode == "terminate":
                    killed = await control.fetchval("""SELECT pg_terminate_backend(pid) FROM pg_stat_activity
                        WHERE datname=$1 AND pid=$2 AND backend_start=$3 AND pid<>pg_backend_pid()""",
                        TEST_DB, owner["pid"], owner["backend_start"])
                    assert killed is True
                else:
                    (folder / "inject").touch()
                async with asyncio.timeout(12):
                    while child.poll() is None:
                        await asyncio.sleep(.02)
                assert child.returncode == 75
                cleanup = json.loads((folder / "cleanup.json").read_text())
                assert cleanup == {"pid": child.pid, "fenced": True, "checkedout": 0, "owned_tasks_done": True}
                assert await control.fetchval("SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND pid=$1", owner["pid"]) == 0
                session = await control.fetchrow("SELECT state,error_code FROM sessions WHERE id=$1", sid)
                assert session["state"] == "failed" and session["error_code"] == "INTERNAL_ERROR"
                log.flush()
                evidence = (folder / "child.log").read_text()
                assert "reason=" + reason in evidence
                assert "service_recovery_requested" in evidence
                successor = subprocess.Popen([sys.executable, "-m", "app.main"], cwd=SERVER_ROOT,
                    env=env, start_new_session=True, stdout=log, stderr=log)
                await ready(http, successor)
                assert (await http.get("/health/ready")).json()["dependencies"] == {"database": "ok", "asr": "ready", "llm": "ready"}
        finally:
            if ws is not None:
                await ws.close()
            for process in (child, successor):
                if process is not None and process.poll() is None:
                    process.send_signal(signal.SIGTERM)
                    try:
                        await asyncio.to_thread(process.wait, timeout=8)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        await asyncio.to_thread(process.wait, timeout=3)
            if control is not None:
                await control.close()
