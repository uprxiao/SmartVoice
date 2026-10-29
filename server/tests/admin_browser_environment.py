"""Owned, synthetic PostgreSQL/HTTP/WS fixture for TC-WIN-34/35 browser tests.

This is an explicit test command, never imported by the application or pytest.
It creates its own cluster and database; it does not inspect deployment credentials.
Run `prepare --run-dir NEW_DIRECTORY`, then `stop --environment environment.json`.
The dataset and evidence are retained; only owned processes are stopped.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo
from datetime import datetime

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = "svi-admin-browser-environment-v1"


def write_new(path: Path, value, *, private=False):
    with path.open("x", encoding="utf-8") as stream:
        if private:
            os.chmod(path, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def stop_server(path):
    environment(path)
    stopped = True
    for record in path.parent.glob("server-process*.json"):
        process = json.loads(record.read_text())
        row = subprocess.run(["ps", "-p", str(process["pid"]), "-o", "args="], capture_output=True, text=True)
        if row.returncode:
            continue
        if process["script"] not in row.stdout or str(path) not in row.stdout:
            raise RuntimeError("Server process ownership changed")
        os.kill(process["pid"], signal.SIGTERM)
        for _ in range(100):
            if subprocess.run(["ps", "-p", str(process["pid"]), "-o", "stat="], capture_output=True, text=True).returncode:
                break
            time.sleep(.1)
        else:
            stopped = False
    if not stopped:
        raise RuntimeError("Owned server termination not confirmed")
    return {"server_stopped": True}


def start_server(path):
    import httpx
    value = environment(path)
    log = (path.parent / ("server-restart-" + secrets.token_hex(3) + ".log")).open("x")
    process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "serve", "--environment", str(path)],
                               cwd=ROOT / "server", env=child_env(value), stdout=log,
                               stderr=subprocess.STDOUT, start_new_session=True)
    write_new(path.parent / ("server-process-" + secrets.token_hex(3) + ".json"),
              {"pid": process.pid, "script": str(Path(__file__).resolve())})
    end = time.monotonic() + 20
    with httpx.Client(timeout=2, trust_env=False) as client:
        while time.monotonic() < end:
            if process.poll() is not None:
                raise RuntimeError("Owned server exited during restart")
            try:
                if client.get(value["origin"] + "/health/ready").status_code == 200:
                    return {"server_ready": True}
            except httpx.TransportError:
                pass
            time.sleep(.1)
    raise RuntimeError("Owned server restart did not become ready")


async def revoke_fixture_logins(value):
    import asyncpg
    if not value["database"].startswith("smartvoiceinput_test_ui_"):
        raise RuntimeError("Refusing login cleanup outside the isolated fixture database")
    conn = await asyncpg.connect(value["database_url"].replace("postgresql+asyncpg:", "postgresql:"))
    try:
        before = await conn.fetchval("SELECT count(*) FROM login_sessions WHERE revoked_at IS NULL")
        await conn.execute("UPDATE login_sessions SET revoked_at=now() WHERE revoked_at IS NULL")
        after = await conn.fetchval("SELECT count(*) FROM login_sessions WHERE revoked_at IS NULL")
        return {"fixture_logins_revoked": before, "active_logins_after_cleanup": after}
    finally:
        await conn.close()


def environment(path: Path):
    value = json.loads(path.read_text())
    if value.get("schema") != SCHEMA or Path(value["run_dir"]).resolve() != path.resolve().parent:
        raise RuntimeError("Environment ownership mismatch")
    database = urlsplit(value["database_url"])
    origin = urlsplit(value["origin"])
    if (database.scheme != "postgresql+asyncpg" or database.hostname != "127.0.0.1"
            or database.port != value["pg_port"] or database.username != value["pg_user"]
            or database.password is not None or database.path != "/" + value["database"]
            or not value["database"].startswith("smartvoiceinput_test_ui_")
            or origin.scheme != "http" or origin.hostname != "127.0.0.1"
            or origin.username is not None or origin.password is not None
            or Path(value["source_root"]).resolve() != ROOT):
        raise RuntimeError("Refusing resources outside the owned loopback fixture")
    for field, filename in (("config", "server.yaml"), ("private_file", "credentials.private.json")):
        if Path(value[field]).resolve() != path.parent / filename:
            raise RuntimeError("Fixture configuration path mismatch")
    return value


def child_env(value):
    env = {key: item for key, item in os.environ.items()
           if not key.startswith("SVI_") and key not in {"GITHUB_TOKEN", "GH_TOKEN"}}
    private = json.loads(Path(value["private_file"]).read_text())
    env.update(SVI_CONFIG=value["config"], SVI_DATABASE_URL=value["database_url"],
               SVI_JWT_SECRET=private["jwt_secret"], SVI_IDEMPOTENCY_SECRET=private["idempotency_secret"],
               SVI_ENVIRONMENT="development", SVI_INSTANCE_LOCK="1")
    return env


def serve(path):
    value = environment(path)
    os.environ.clear()
    os.environ.update(child_env_from_private(value))
    from app.main import create_app, create_server
    from fastapi.staticfiles import StaticFiles
    app = create_app(value["config"])
    app.mount("/admin", StaticFiles(directory=ROOT / "admin" / "dist", html=True), name="admin-test-build")
    create_server(app).run()


def child_env_from_private(value):
    # Keep a minimal process environment. Test secrets never become command arguments.
    private = json.loads(Path(value["private_file"]).read_text())
    return {"PATH": os.defpath, "LANG": "en_US.UTF-8", "SVI_CONFIG": value["config"],
            "SVI_DATABASE_URL": value["database_url"], "SVI_JWT_SECRET": private["jwt_secret"],
            "SVI_IDEMPOTENCY_SECRET": private["idempotency_secret"],
            "SVI_ENVIRONMENT": "development", "SVI_INSTANCE_LOCK": "1"}


async def seed(value):
    import asyncpg
    import httpx
    import websockets
    private = json.loads(Path(value["private_file"]).read_text())
    root = Path(value["run_dir"])
    origin = value["origin"]
    outcomes = [("inserted", True, None), ("failed", False, "PERMISSION_DENIED"),
                ("unverified", False, "VERIFY_FAILED")]
    users, sessions = [], []
    async with httpx.AsyncClient(base_url=origin, timeout=15, trust_env=False) as client:
        tokens = []
        for number in (1, 2):
            email = f"windows-{number}@example.test"
            registered = await client.post("/api/auth/register", json={"email": email, "password": private["password"]})
            assert registered.status_code == 201, registered.status_code
            login = await client.post("/api/auth/login", json={"email": email, "password": private["password"],
                "client_type": "desktop", "device": {"installation_id": str(uuid.uuid4()),
                "name": "Synthetic Windows browser fixture", "platform": "windows", "client_version": "0.0.2"}})
            assert login.status_code == 200, login.status_code
            pair = login.json()
            users.append({"id": pair["user"]["id"], "email": email, "role": "user", "status": "active"})
            tokens.append(pair["access_token"])
        for index, (status, observable, code) in enumerate(outcomes):
            actor = 0 if index < 2 else 1
            headers = {"Authorization": f"Bearer {tokens[actor]}"}
            request_id = str(uuid.uuid4())
            created = await client.post("/api/v1/sessions", headers={**headers, "Idempotency-Key": request_id},
                                       json={"client_request_id": request_id, "learning_enabled": False})
            assert created.status_code == 201, created.status_code
            sid = created.json()["session_id"]
            async with websockets.connect(origin.replace("http:", "ws:") + f"/ws/v1/sessions/{sid}/audio",
                                          additional_headers=headers, proxy=None) as ws:
                assert json.loads(await ws.recv())["type"] == "ready"
                for seq in range(4):
                    await ws.send(struct.pack("<II", seq, 4000) + b"\x00\x00" * 4000)
                for _ in range(4):
                    assert json.loads(await ws.recv())["type"] == "audio.ack"
                await ws.send(json.dumps({"type": "audio.end", "last_seq": 3, "total_samples": 16000}))
                async with asyncio.timeout(15):
                    while True:
                        event = json.loads(await ws.recv())
                        if event["type"] == "result.ready":
                            result = event["result"]
                            break
                        assert event["type"] not in {"error", "cancelled", "session.empty"}, event["type"]
            assert result["raw_text"] == "嗯那个，帮我加一个按钮，不要修改数据库。"
            assert result["refined_text"] == "帮我加一个按钮，不要修改数据库。"
            insertion = str(uuid.uuid4())
            claim = {"result_id": result["result_id"], "insertion_id": insertion,
                     "text_hash": result["text_hash"], "review_confirmed": False}
            for expected in (201, 200):
                response = await client.post(f"/api/v1/sessions/{sid}/insertions/claim", headers=headers, json=claim)
                assert response.status_code == expected, response.status_code
            for _ in range(2):
                response = await client.post(f"/api/v1/sessions/{sid}/insertions/{insertion}/receipt", headers=headers,
                                            json={"status": status, "observable": observable, "error_code": code})
                assert response.status_code == 200 and response.json()["status"] == status
            sessions.append({"id": sid, "user_id": users[actor]["id"], "insertion_status": status,
                             "expected_received_samples": 16000, "learning_enabled": False})
        for token in tokens:
            response = await client.post("/api/auth/logout", headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 204
    conn = await asyncpg.connect(value["database_url"].replace("postgresql+asyncpg:", "postgresql:"))
    try:
        rows = await conn.fetch("SELECT id,user_id,state,learning_enabled FROM sessions ORDER BY id")
        receipts = await conn.fetch("SELECT session_id,status FROM insertions ORDER BY session_id")
        snapshot = {"sessions": [dict(row) for row in rows], "receipts": [dict(row) for row in receipts]}
        assert len(rows) == 3 and all(row["state"] == "ready" and not row["learning_enabled"] for row in rows)
        assert sorted(row["status"] for row in receipts) == ["failed", "inserted", "unverified"]
    finally:
        await conn.close()
    fixtures = {"schema": "svi-admin-browser-fixtures-v1", "origin": origin,
        "timezone": "Asia/Shanghai", "prepared_at": datetime.now(ZoneInfo("UTC")).isoformat(),
        "business_date": datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(),
        "users": users, "admin_email": "admin-browser@example.test", "sessions": sessions,
        "expected": {"registered_users": 3, "active_users": 3, "disabled_users": 0,
                     "sessions": 3, "result_sessions": 3, "failed_sessions": 0,
                     "inserted_sessions": 1, "insertion_success_rate": "33.3%", "learning_events": 0,
                     "received_audio_ms": 3000, "successful_audio_ms": 3000,
                     "chars": 42,  # BR-01: 14 letters/numbers per result, excluding its punctuation.
                     "users_sessions": [2, 1]},
        "oracle": "Three predeclared one-second synthetic sessions; duplicate claim/receipts must not increment statistics; raw sessions/insertions independently checked."}
    write_new(root / "database-snapshot.json", snapshot)
    write_new(root / "fixtures.json", fixtures)


def prepare(run_dir):
    import httpx
    import yaml
    run_dir.mkdir(parents=True, exist_ok=False)
    pg = Path(tempfile.mkdtemp(prefix="svi-v002-ui-pg-"))
    (pg / "socket").mkdir()
    pg_port, server_port = free_port(), free_port()
    while server_port == pg_port:
        server_port = free_port()
    db_name = "smartvoiceinput_test_ui_" + secrets.token_hex(5)
    value = {"schema": SCHEMA, "run_dir": str(run_dir), "source_root": str(ROOT),
        "pg_root": str(pg), "pg_port": pg_port, "pg_user": "svi_ui_test", "database": db_name,
        "database_url": f"postgresql+asyncpg://svi_ui_test@127.0.0.1:{pg_port}/{db_name}",
        "origin": f"http://127.0.0.1:{server_port}", "config": str(run_dir / "server.yaml"),
        "private_file": str(run_dir / "credentials.private.json")}
    private = {"password": secrets.token_urlsafe(18), "jwt_secret": secrets.token_urlsafe(48),
               "idempotency_secret": secrets.token_urlsafe(48)}
    write_new(Path(value["private_file"]), private, private=True)
    write_new(pg / "owner.json", {"schema": SCHEMA, "run_dir": str(run_dir)})
    config = yaml.safe_load((ROOT / "server/config/app.yaml").read_text())
    config["server"].update(host="127.0.0.1", port=server_port)
    with Path(value["config"]).open("x") as stream:
        yaml.safe_dump(config, stream, allow_unicode=True)
    # Record ownership before any child is started so interrupted setup can be stopped.
    write_new(run_dir / "environment.json", value)
    with (run_dir / "setup.log").open("x") as log:
        subprocess.run(["initdb", "-D", str(pg / "data"), "-U", value["pg_user"],
                        "--auth-local=trust", "--auth-host=trust", "--encoding=UTF8", "--locale=C"],
                       stdout=log, stderr=subprocess.STDOUT, check=True)
        subprocess.run(["pg_ctl", "-D", str(pg / "data"), "-l", str(pg / "postgres.log"),
                        "-o", f"-h 127.0.0.1 -p {pg_port} -k {pg / 'socket'}", "-w", "start"],
                       stdout=log, stderr=subprocess.STDOUT, check=True)
        subprocess.run(["createdb", "-h", "127.0.0.1", "-p", str(pg_port), "-U", value["pg_user"], db_name],
                       stdout=log, stderr=subprocess.STDOUT, check=True)
        env = child_env(value)
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT / "server", env=env,
                       stdout=log, stderr=subprocess.STDOUT, check=True)
        subprocess.run([sys.executable, "-m", "app.cli", "admin", "init", "--email", "admin-browser@example.test",
                        "--display-name", "Synthetic Admin", "--password-stdin"], cwd=ROOT / "server", env=env,
                       input=private["password"] + "\n", text=True, stdout=log, stderr=subprocess.STDOUT, check=True)
    log = (run_dir / "server.log").open("x")
    process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "serve", "--environment",
                                str(run_dir / "environment.json")], cwd=ROOT / "server", env=child_env(value),
                               stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    write_new(run_dir / "server-process.json", {"pid": process.pid, "script": str(Path(__file__).resolve())})
    end = time.monotonic() + 20
    with httpx.Client(timeout=2, trust_env=False) as client:
        while time.monotonic() < end:
            if process.poll() is not None:
                raise RuntimeError("Owned server exited during startup; see server.log")
            try:
                ready = client.get(value["origin"] + "/health/ready")
                if ready.status_code == 200:
                    break
            except httpx.TransportError:
                pass
            time.sleep(.1)
        else:
            raise RuntimeError("Owned server did not become ready")
    asyncio.run(seed(value))
    print(json.dumps({"status": "ready", "origin": value["origin"], "environment": str(run_dir / "environment.json")}))


def stop(path):
    value = environment(path)
    pg = Path(value["pg_root"])
    owner = json.loads((pg / "owner.json").read_text())
    if not pg.name.startswith("svi-v002-ui-pg-") or owner != {"schema": SCHEMA, "run_dir": value["run_dir"]}:
        raise RuntimeError("PostgreSQL ownership mismatch")
    receipt = {**stop_server(path), **asyncio.run(revoke_fixture_logins(value)),
               "postgres_stopped": False, "dataset_retained": True}
    stopped = subprocess.run(["pg_ctl", "-D", str(pg / "data"), "-m", "fast", "-w", "stop"], capture_output=True, text=True)
    receipt["postgres_stopped"] = stopped.returncode == 0
    write_new(path.parent / ("cleanup-" + secrets.token_hex(3) + ".json"), receipt)
    print(json.dumps(receipt))
    if not all((receipt["server_stopped"], receipt["postgres_stopped"])):
        raise RuntimeError("Owned environment cleanup incomplete")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("prepare", "serve", "pause", "resume", "stop"))
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--environment", type=Path)
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.run_dir.resolve())
    elif args.command == "serve":
        serve(args.environment.resolve())
    elif args.command == "pause":
        print(json.dumps(stop_server(args.environment.resolve())))
    elif args.command == "resume":
        print(json.dumps(start_server(args.environment.resolve())))
    else:
        stop(args.environment.resolve())
