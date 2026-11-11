import asyncio
import os
from pathlib import Path
import socket
import sys
import tempfile
from types import SimpleNamespace

from fastapi import FastAPI
import pytest

from app import main
from app.core.service_watchdog import ServiceWatchdog


@pytest.mark.asyncio
async def test_production_server_observes_fence_and_requests_exit():
    application = FastAPI()
    application.state.config = SimpleNamespace(server=SimpleNamespace(host="127.0.0.1", port=17863, workers=1))
    application.state.instance_fenced = True
    server = main.create_server(application)
    assert await server.on_tick(0) is True
    assert server.exit_code == 75


def test_process_entry_propagates_recovery_exit_code(monkeypatch):
    server = SimpleNamespace(run=lambda: None, exit_code=75)
    monkeypatch.setattr(main, "create_server", lambda application: server)
    with pytest.raises(SystemExit) as exited:
        main.main()
    assert exited.value.code == 75


class ProbeConnection:
    closed = invalidated = False

    def __init__(self, result="owned", failure=None, failing_stage=None):
        self.result, self.failure, self.failing_stage = result, failure, failing_stage
        self.commits = 0

    async def scalar(self, *args):
        if self.failing_stage == "query":
            raise self.failure
        return self.result

    async def commit(self):
        if self.failing_stage == "commit":
            raise self.failure
        self.commits += 1


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["connection_missing", "connection_closed", "connection_invalidated", "backend_changed", "lock_missing"])
async def test_probe_classifies_ownership_failure(caplog, reason):
    connection = None if reason == "connection_missing" else ProbeConnection(result=reason)
    if reason == "connection_closed":
        connection.closed = True
    elif reason == "connection_invalidated":
        connection.invalidated = True
    assert await main._instance_lock_alive(connection, 42, 123) is False
    assert "reason=" + reason in caplog.text
    assert "backend_pid=123" in caplog.text


@pytest.mark.asyncio
async def test_healthy_probe_does_not_fence_or_release_session_lock(caplog):
    connection = ProbeConnection()
    assert await main._instance_lock_alive(connection, 42, 123) is True
    assert connection.commits == 1
    assert "probe_failed" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["query", "commit"])
@pytest.mark.parametrize("error", [TimeoutError("private-DSN-canary"), RuntimeError("private-DSN-canary")])
async def test_probe_exception_metadata_excludes_messages(caplog, stage, error):
    connection = ProbeConnection(failure=error, failing_stage=stage)
    with pytest.raises(type(error)):
        await main._instance_lock_alive(connection, 42, 123)
    assert "reason=" + ("probe_timeout" if isinstance(error, TimeoutError) else "probe_error") in caplog.text
    assert "stage=" + stage in caplog.text
    assert "error_type=" + type(error).__name__ in caplog.text
    assert "private-DSN-canary" not in caplog.text


@pytest.mark.asyncio
async def test_probe_records_safe_original_driver_type_and_sqlstate(caplog):
    from sqlalchemy.exc import OperationalError

    class DriverFailure(Exception):
        sqlstate = "57P01"

    error = OperationalError("private-SQL-canary", {"secret": "private-parameter-canary"}, DriverFailure("private-driver-canary"))
    with pytest.raises(OperationalError):
        await main._instance_lock_alive(ProbeConnection(failure=error, failing_stage="query"), 42, 123)
    assert "error_type=OperationalError driver_error_type=DriverFailure sqlstate=57P01" in caplog.text
    assert "private-" not in caplog.text


def test_process_entry_retains_normal_exit(monkeypatch):
    monkeypatch.setattr(main, "create_server", lambda application: SimpleNamespace(run=lambda: None, exit_code=0))
    assert main.main() is None


def test_watchdog_environment_rejects_foreign_owner_and_invalid_values(monkeypatch):
    monkeypatch.setenv("NOTIFY_SOCKET", "/tmp/unused-notification-socket")
    monkeypatch.setenv("WATCHDOG_USEC", "30000000")
    monkeypatch.setenv("WATCHDOG_PID", str(os.getpid() + 1))
    assert ServiceWatchdog.from_environment() is None
    monkeypatch.setenv("WATCHDOG_PID", str(os.getpid()))
    assert ServiceWatchdog.from_environment().interval_seconds == 5
    monkeypatch.setenv("WATCHDOG_USEC", "invalid")
    assert ServiceWatchdog.from_environment() is None
    monkeypatch.delenv("NOTIFY_SOCKET")
    assert ServiceWatchdog.from_environment() is None


@pytest.mark.parametrize("abstract", [False, True])
def test_watchdog_sends_real_unix_datagrams(abstract):
    if abstract and sys.platform != "linux":
        pytest.skip("abstract UNIX sockets are Linux-only; exercised on Linux")
    with tempfile.TemporaryDirectory(prefix="svi-notify-", dir="/tmp") as folder:
        address = ("@svi-notify-" + str(os.getpid())) if abstract else str(Path(folder) / "notify")
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
            receiver.bind("\0" + address[1:] if abstract else address)
            receiver.settimeout(1)
            watchdog = ServiceWatchdog(address, 30)
            assert watchdog.notify(b"READY=1\nWATCHDOG=1") is True
            assert receiver.recv(1024) == b"READY=1\nWATCHDOG=1"


@pytest.mark.asyncio
async def test_watchdog_stops_feeding_when_fenced():
    from app.core.server import ManagedServer
    from uvicorn import Config
    with tempfile.TemporaryDirectory(prefix="svi-notify-", dir="/tmp") as folder:
        address = str(Path(folder) / "notify")
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
            receiver.bind(address)
            receiver.setblocking(False)
            state = SimpleNamespace(fenced=False)
            server = ManagedServer(Config(FastAPI()), begin_shutdown=lambda: None,
                fatal_reason=lambda: "INSTANCE_LOCK_LOST" if state.fenced else None)
            server._watchdog = ServiceWatchdog(address, .06)
            task = asyncio.create_task(server._notify_watchdog())
            message = await asyncio.wait_for(asyncio.get_running_loop().sock_recv(receiver, 1024), 1)
            assert message == b"WATCHDOG=1"
            state.fenced = True
            await asyncio.wait_for(task, 1)
            assert server.should_exit and server.exit_code == 75
            with pytest.raises(BlockingIOError):
                receiver.recv(1024)
