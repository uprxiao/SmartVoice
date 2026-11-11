"""Opt-in Linux supervisor tests; transient units and a fixture-owned PG only."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import uuid

import httpx
import pytest

from tests.conftest import SERVER_ROOT, TEST_URL
from tests.integration.test_instance_recovery_loss import require_process_owned_database

pytestmark = pytest.mark.skipif(sys.platform != "linux" or os.environ.get("SVI_SYSTEMD_TEST") != "1",
    reason="requires explicit opt-in on an isolated Linux systemd/PG test environment")


def command(*args):
    return subprocess.run(args, text=True, capture_output=True, timeout=15)


def properties(unit):
    result = command("systemctl", "show", unit, "-p", "MainPID", "-p", "ActiveState", "-p", "NRestarts", "-p", "Result")
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def alive(pid):
    path = Path(f"/proc/{pid}/stat")
    try:
        return path.read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["unlock", "cleanup_hang", "loop_hang"])
async def test_real_systemd_restart_and_watchdog(clean_database, tmp_path, unused_tcp_port, record_property, mode):
    require_process_owned_database()
    assert os.geteuid() == 0
    folder = tmp_path / mode
    folder.mkdir()
    unit = "svi-lock-test-" + uuid.uuid4().hex[:12]
    env = {"SVI_DATABASE_URL": TEST_URL, "SVI_CONFIG": str(SERVER_ROOT / "config/app.yaml"),
        "SVI_INSTANCE_LOCK": "1", "SVI_ENVIRONMENT": "development", "SVI_ASR_PROVIDER": "mock",
        "SVI_LLM_PROVIDER": "mock", "SVI_HOST": "127.0.0.1", "SVI_PORT": str(unused_tcp_port),
        "WEB_CONCURRENCY": "1", "UVICORN_WORKERS": "1", "PYTHONPATH": os.environ["PYTHONPATH"],
        "SVI_JWT_SECRET": secrets.token_hex(32), "SVI_IDEMPOTENCY_SECRET": secrets.token_hex(32)}
    args = ["systemd-run", "--quiet", "--unit=" + unit, "--working-directory=" + str(SERVER_ROOT)]
    for setting in ("Type=notify", "NotifyAccess=main", "WatchdogSec=3s", "WatchdogSignal=SIGKILL",
                    "KillMode=control-group", "SendSIGKILL=yes", "TimeoutStopSec=4s", "TimeoutStartSec=15s",
                    "Restart=on-failure", "RestartSec=200ms", "StartLimitIntervalSec=30s", "StartLimitBurst=5"):
        args.append("--property=" + setting)
    args.extend("--setenv=" + key + "=" + value for key, value in env.items())
    args.extend([sys.executable, str(Path(__file__).with_name("instance_process_child.py")), str(folder), mode])
    old_pid = descendant = None
    try:
        result = command(*args)
        assert result.returncode == 0, result.stderr
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{unused_tcp_port}", timeout=1, trust_env=False) as http:
            async with asyncio.timeout(15):
                while True:
                    try:
                        if (await http.get("/health/ready")).status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    await asyncio.sleep(.05)
            old_pid = int(properties(unit)["MainPID"])
            assert old_pid > 0
            (folder / "inject").touch()
            async with asyncio.timeout(20):
                while True:
                    if (folder / "cleanup-child-pid").exists():
                        descendant = int((folder / "cleanup-child-pid").read_text())
                    state = properties(unit)
                    if state.get("ActiveState") == "active" and int(state.get("MainPID", "0")) not in {0, old_pid}:
                        try:
                            if (await http.get("/health/ready")).status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                    await asyncio.sleep(.05)
            assert int(state["NRestarts"]) == 1
            assert not alive(old_pid)
            if mode == "cleanup_hang":
                assert descendant is not None and not alive(descendant)
            journal = command("journalctl", "-u", unit, "--no-pager", "-o", "cat").stdout
            assert ("status=75" in journal) if mode == "unlock" else ("watchdog" in journal.lower())
            record_property("supervisor_recovery", json.dumps({"mode": mode, "old_pid": old_pid,
                "new_pid": int(state["MainPID"]), "restarts": int(state["NRestarts"]),
                "descendant_terminated": descendant is None or not alive(descendant)}))
    finally:
        command("systemctl", "stop", unit)
        command("systemctl", "reset-failed", unit)


@pytest.mark.asyncio
async def test_systemd_limits_repeated_failure(tmp_path, record_property):
    unit = "svi-lock-limit-" + uuid.uuid4().hex[:12]
    try:
        result = command("systemd-run", "--quiet", "--unit=" + unit,
            "--property=Type=exec", "--property=Restart=on-failure", "--property=RestartSec=100ms",
            "--property=StartLimitIntervalSec=30s", "--property=StartLimitBurst=2",
            sys.executable, "-c", "raise SystemExit(75)")
        assert result.returncode == 0
        async with asyncio.timeout(8):
            while True:
                state = properties(unit)
                journal = command("journalctl", "-u", unit, "--no-pager", "-o", "cat").stdout
                # CentOS systemd 252 retains Result=exit-code after refusing the
                # next start. Require the actual limiter event and exact attempts.
                if state.get("ActiveState") == "failed" and "Start request repeated too quickly" in journal:
                    break
                await asyncio.sleep(.05)
        assert state["ActiveState"] == "failed" and state["MainPID"] == "0"
        assert int(state["NRestarts"]) == 2
        assert journal.count("Main process exited, code=exited, status=75") == 2
        record_property("restart_limit", json.dumps(state, sort_keys=True))
    finally:
        command("systemctl", "stop", unit)
        command("systemctl", "reset-failed", unit)
