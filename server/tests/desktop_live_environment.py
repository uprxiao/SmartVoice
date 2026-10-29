"""Owned PostgreSQL fixture for actual desktop Main/preload + Admin HTTP tests.

Test command only, never imported by the server. No deployment database, credentials or
provider keys are consulted. The controller owns the server process; this command owns
only the new cluster under --run-dir. Windows here is test infrastructure, not deployment.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
import zoneinfo

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = "svi-desktop-live-environment-v1"


def write_new(path, value, private=False):
    with Path(path).open("x", encoding="utf-8") as file:
        if private:
            os.chmod(path, 0o600)
        json.dump(value, file, ensure_ascii=False, indent=2)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def environment(path):
    path = Path(path).resolve()
    value = json.loads(path.read_text(encoding="utf-8"))
    database = urlsplit(value["database_url"])
    origin = urlsplit(value["origin"])
    if (value.get("schema") != SCHEMA or Path(value["run_dir"]).resolve() != path.parent
            or Path(value["source_root"]).resolve() != ROOT
            or not value["database"].startswith("smartvoiceinput_test_desktop_")
            or database.scheme != "postgresql+asyncpg" or database.hostname != "127.0.0.1"
            or database.port != value["pg_port"] or database.username != "svi_desktop_test"
            or database.password is not None or database.path != "/" + value["database"]
            or origin.scheme != "http" or origin.hostname != "127.0.0.1"
            or origin.port != value["port"] or origin.username is not None):
        raise RuntimeError("Refusing resources outside the owned desktop fixture")
    owner = json.loads((path.parent / "owner.json").read_text(encoding="utf-8"))
    if owner != {"schema": SCHEMA, "database": value["database"], "run_dir": str(path.parent)}:
        raise RuntimeError("Fixture ownership changed")
    if Path(value["pg_data"]).resolve() != path.parent / "pg-data":
        raise RuntimeError("Cluster path changed")
    for field, filename in (("config", "server.yaml"), ("private_file", "credentials.private.json")):
        if Path(value[field]).resolve() != path.parent / filename:
            raise RuntimeError("Fixture path changed")
    return value


def child_env(value):
    # Keep Python/Windows runtime essentials. Never inherit model/deployment credentials.
    keep = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC", "PATHEXT",
            "APPDATA", "LOCALAPPDATA", "USERPROFILE", "LANG", "LC_ALL", "HOME"}
    env = {key: item for key, item in os.environ.items() if key.upper() in keep}
    private = json.loads(Path(value["private_file"]).read_text(encoding="utf-8"))
    env.update(SVI_CONFIG=value["config"], SVI_DATABASE_URL=value["database_url"],
               SVI_JWT_SECRET=private["jwt_secret"], SVI_IDEMPOTENCY_SECRET=private["idempotency_secret"],
               SVI_ENVIRONMENT="development", SVI_INSTANCE_LOCK="1", PYTHONUTF8="1")
    if value.get("tzpath"):
        env["PYTHONTZPATH"] = value["tzpath"]
    return env


def prepare(run_dir, pg_bin, port):
    import yaml
    run_dir = run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    if not 1 <= port <= 65535:
        raise RuntimeError("Explicit API port required")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", port))  # Never stop or adopt a pre-existing API listener.
    executable = lambda name: str(pg_bin / (name + (".exe" if os.name == "nt" else "")))
    for tool in ("initdb", "pg_ctl", "createdb", "pg_config"):
        if not Path(executable(tool)).is_file():
            raise RuntimeError("Frozen PostgreSQL tools missing")
    share = Path(subprocess.check_output([executable("pg_config"), "--sharedir"], text=True).strip())
    tzpath = next((p for p in [share / "timezone", share / "postgresql/timezone"]
                   if (p / "Asia/Shanghai").is_file()), None)
    if tzpath:
        zoneinfo.reset_tzpath([str(tzpath)])
    zoneinfo.ZoneInfo("Asia/Shanghai")  # Windows can use PostgreSQL's real TZif database.
    pg_port = free_port()
    while pg_port == port:
        pg_port = free_port()
    database = "smartvoiceinput_test_desktop_" + secrets.token_hex(6)
    value = {"schema": SCHEMA, "run_dir": str(run_dir), "source_root": str(ROOT),
             "database": database, "pg_port": pg_port, "port": port,
             "pg_bin": str(pg_bin.resolve()), "pg_data": str(run_dir / "pg-data"),
             "database_url": f"postgresql+asyncpg://svi_desktop_test@127.0.0.1:{pg_port}/{database}",
             "origin": f"http://127.0.0.1:{port}", "config": str(run_dir / "server.yaml"),
             "private_file": str(run_dir / "credentials.private.json"), "tzpath": str(tzpath) if tzpath else None,
             "prepared_at": datetime.now(timezone.utc).isoformat()}
    private = {"password": secrets.token_urlsafe(24), "jwt_secret": secrets.token_urlsafe(48),
               "idempotency_secret": secrets.token_urlsafe(48)}
    write_new(value["private_file"], private, private=True)
    write_new(run_dir / "owner.json", {"schema": SCHEMA, "database": database, "run_dir": str(run_dir)})
    write_new(run_dir / "environment.json", value)
    config = yaml.safe_load((ROOT / "server/config/app.yaml").read_text(encoding="utf-8"))
    config["server"].update(host="127.0.0.1", port=port)
    if config["asr"]["provider"] != "mock" or config["llm"]["provider"] != "mock":
        raise RuntimeError("Fixture refuses non-mock providers")
    with Path(value["config"]).open("x", encoding="utf-8") as file:
        yaml.safe_dump(config, file, allow_unicode=True)
    if os.name == "nt":
        # PostgreSQL restricts its own process token. Grant this exact user on the
        # new fixture, rather than relying on an Administrators-only inherited ACL.
        acl = subprocess.check_output(["pwsh.exe", "-NoProfile", "-NonInteractive", "-File",
                  str(ROOT / "desktop/native/windows/scripts/fixture-acl.ps1"), "-OwnedDir", str(run_dir)], text=True)
        write_new(run_dir / "setup-acl.json", json.loads(acl))
    with (run_dir / "setup.private.log").open("x", encoding="utf-8") as log:
        def run(stage, args, **kwargs):
            try:
                subprocess.run(args, stdout=log, stderr=subprocess.STDOUT, check=True, **kwargs)
            except subprocess.CalledProcessError as error:
                log.flush()
                tail = (run_dir / "setup.private.log").read_bytes()[-16000:].decode("utf-8", errors="replace")
                for secret in private.values():
                    tail = tail.replace(secret, "[redacted]")
                for path in (str(run_dir), str(ROOT), str(Path.home())):
                    tail = tail.replace(path, "[owned-path]")
                tail = re.sub(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[redacted-token]", tail)
                write_new(run_dir / "setup-failure.json", {"stage": stage, "exit_code": error.returncode,
                          "error_type": type(error).__name__, "sanitized_log_tail": tail})
                raise
        run("initdb", [executable("initdb"), "-D", value["pg_data"], "-U", "svi_desktop_test",
             "--auth-local=trust", "--auth-host=trust", "--encoding=UTF8", "--locale=C"])
        options = f"-h 127.0.0.1 -p {pg_port}"
        if os.name != "nt":
            sockets = Path(tempfile.mkdtemp(prefix="svd-"))
            options += f" -k {sockets}"
        run("start-postgres", [executable("pg_ctl"), "-D", value["pg_data"], "-l", str(run_dir / "postgres.private.log"),
             "-o", options, "-w", "start"])
        run("create-database", [executable("createdb"), "-h", "127.0.0.1", "-p", str(pg_port), "-U", "svi_desktop_test", database])
        env = child_env(value)
        run("migrate", [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT / "server", env=env)
        run("bootstrap-admin", [sys.executable, "-m", "app.cli", "admin", "init", "--email", "admin-live@example.test",
             "--display-name", "Synthetic Desktop Admin", "--password-stdin"], cwd=ROOT / "server", env=env,
            input=private["password"] + "\n", text=True)
    write_new(run_dir / "prepared.json", {"status": "prepared", "database_prefix_verified": True,
              "origin": value["origin"], "expected_users_before_ui": 1, "expected_sessions_before_ui": 0,
              "providers": {"asr": "mock", "llm": "mock"},
              "postgres_version": subprocess.check_output([executable("pg_config"), "--version"], text=True).strip(),
              "tzif_sha256": hashlib.sha256((tzpath / "Asia/Shanghai").read_bytes()).hexdigest() if tzpath else None})


def serve(path):
    value = environment(path)
    env = child_env(value)
    os.environ.clear()
    os.environ.update(env)
    if value.get("tzpath"):
        zoneinfo.reset_tzpath([value["tzpath"]])
    from app.main import create_app, create_server
    from fastapi.staticfiles import StaticFiles
    app = create_app(value["config"])
    app.mount("/admin", StaticFiles(directory=ROOT / "admin/dist", html=True), name="desktop-live-admin")
    create_server(app).run()


async def snapshot(path):
    import asyncpg
    value = environment(path)
    conn = await asyncpg.connect(value["database_url"].replace("postgresql+asyncpg:", "postgresql:"))
    try:
        users = await conn.fetch("SELECT email,role,status,learning_enabled,password_hash LIKE '$argon2id$%' AS argon2id FROM users ORDER BY email")
        sessions = await conn.fetchval("SELECT count(*) FROM sessions")
        logins = await conn.fetchval("SELECT count(*) FROM login_sessions WHERE revoked_at IS NULL")
        return {"users": [dict(u) for u in users], "sessions": sessions, "active_logins": logins}
    finally:
        await conn.close()


async def revoke(path):
    import asyncpg
    value = environment(path)
    conn = await asyncpg.connect(value["database_url"].replace("postgresql+asyncpg:", "postgresql:"))
    try:
        before = await conn.fetchval("SELECT count(*) FROM login_sessions WHERE revoked_at IS NULL")
        await conn.execute("UPDATE login_sessions SET revoked_at=now() WHERE revoked_at IS NULL")
        after = await conn.fetchval("SELECT count(*) FROM login_sessions WHERE revoked_at IS NULL")
        write_new(Path(value["run_dir"]) / "login-cleanup.json", {"revoked": before, "active_after_cleanup": after})
        if after != 0:
            raise RuntimeError("Fixture logins remain active")
    finally:
        await conn.close()


def stop_pg(path):
    value = environment(path)
    tool = Path(value["pg_bin"]) / ("pg_ctl.exe" if os.name == "nt" else "pg_ctl")
    process = subprocess.run([str(tool), "-D", value["pg_data"], "-m", "fast", "-w", "stop"],
                             capture_output=True, text=True)
    with socket.socket() as sock:
        closed = sock.connect_ex(("127.0.0.1", value["pg_port"])) != 0
    write_new(Path(value["run_dir"]) / ("postgres-cleanup-" + secrets.token_hex(3) + ".json"),
              {"exit_code": process.returncode, "port_closed": closed, "dataset_retained": True})
    if not closed:
        raise RuntimeError("Owned PostgreSQL did not stop")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("prepare", "serve", "snapshot", "revoke", "stop-pg"))
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--pg-bin", type=Path)
    parser.add_argument("--port", type=int, default=17863)
    parser.add_argument("--environment", type=Path)
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.run_dir, args.pg_bin, args.port)
    elif args.command == "serve":
        serve(args.environment)
    elif args.command == "snapshot":
        print(json.dumps(asyncio.run(snapshot(args.environment)), ensure_ascii=False))
    elif args.command == "revoke":
        asyncio.run(revoke(args.environment))
    else:
        stop_pg(args.environment)
