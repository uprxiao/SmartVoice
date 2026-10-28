from __future__ import annotations

from tests.ids import fixture_uuid

import asyncio
import logging
import os
import re
import time
import uuid
from pathlib import Path

import asyncpg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi.testclient import TestClient
from sqlalchemy.engine import URL

SERVER_ROOT = Path(__file__).resolve().parents[1]
# pytest can load this file as conftest while helpers import tests.conftest.
# Share one process-owned name across both module identities.
TEST_DB = os.environ.setdefault("SVI_TEST_DB_NAME", f"smartvoiceinput_test_{os.getpid()}_{uuid.uuid4().hex[:10]}")
if not re.fullmatch(r"smartvoiceinput_test_[a-zA-Z0-9_]{1,38}", TEST_DB):
    raise RuntimeError("SVI_TEST_DB_NAME must name an isolated smartvoiceinput_test_* database")
_CREATED_TEST_DB = False
PG_HOST = os.environ.get("SVI_TEST_PG_HOST", "127.0.0.1")
PG_PORT = int(os.environ.get("SVI_TEST_PG_PORT", "5433"))
PG_USER = os.environ.get("SVI_TEST_PG_USER", "svi")
PG_PASSWORD = os.environ.get("SVI_TEST_PG_PASSWORD", "svi_local_dev")
TEST_URL = URL.create("postgresql+asyncpg", username=PG_USER, password=PG_PASSWORD, host=PG_HOST, port=PG_PORT, database=TEST_DB).render_as_string(hide_password=False)

TRUNCATE = """
TRUNCATE TABLE
  learning_events, model_calls, model_usage_daily, usage_daily,
  session_contributions, insertions, results, session_attempts, sessions,
  operation_requests, dictionary_import_rows, dictionary_imports,
  dictionary_terms, dictionary_state, login_sessions, devices, users,
  runtime_capacity
RESTART IDENTITY CASCADE
"""


async def _create_database() -> None:
    global _CREATED_TEST_DB
    conn = await asyncpg.connect(
        host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, database="postgres"
    )
    try:
        # A name collision fails rather than deleting another process's database.
        await conn.execute(f'CREATE DATABASE "{TEST_DB}"')
        _CREATED_TEST_DB = True
    finally:
        await conn.close()


async def _truncate() -> None:
    if not _CREATED_TEST_DB:
        raise RuntimeError("refusing to truncate a database not created by this test session")
    conn = await asyncpg.connect(
        host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, database=TEST_DB
    )
    try:
        await conn.execute(TRUNCATE)
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def test_database():
    try:
        asyncio.run(_create_database())
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover
        pytest.fail(f"required isolated PostgreSQL unavailable ({type(exc).__name__}); no database tests ran", pytrace=False)
    os.environ["SVI_DATABASE_URL"] = TEST_URL
    os.environ["SVI_CONFIG"] = str(SERVER_ROOT / "config" / "app.yaml")
    alembic_config = AlembicConfig(str(SERVER_ROOT / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(SERVER_ROOT / "migrations"))
    # Alembic fileConfig runs in this pytest process, while deployment runs it
    # separately. Preserve the collected application's logger enablement so
    # test ordering cannot silently disable the code under observation.
    logger_states = {
        logger: logger.disabled
        for logger in logging.Logger.manager.loggerDict.values()
        if isinstance(logger, logging.Logger)
    }
    try:
        command.upgrade(alembic_config, "head")
    finally:
        for logger, disabled in logger_states.items():
            logger.disabled = disabled
    yield


@pytest.fixture()
def clean_database(test_database):
    asyncio.run(_truncate())
    yield


@pytest.fixture()
def app_client(clean_database, monkeypatch):
    monkeypatch.setenv("SVI_DATABASE_URL", TEST_URL)
    monkeypatch.setenv("SVI_CONFIG", str(SERVER_ROOT / "config" / "app.yaml"))
    monkeypatch.setenv("SVI_INSTANCE_LOCK", "0")
    monkeypatch.setenv("SVI_ENVIRONMENT", "development")
    from app.main import create_app

    app = create_app(str(SERVER_ROOT / "config" / "app.yaml"))
    with TestClient(app) as client:
        wait_for_readiness(client)
        health = client.get("/healthz")
        assert health.status_code == 200 and health.json().get("ready") is True, "mock integration server is not ready"
        yield client


def _readiness_finished(response, pending_reason):
    if response.status_code == 200:
        assert response.json().get("status") in {"ready", "degraded"}
        return True
    reason = response.json().get("reason_code")
    assert response.status_code == 503 and reason == pending_reason, (
        "unexpected readiness response", response.status_code, reason)
    return False


def wait_for_readiness(client, *, seconds=5, pending_reason="STARTING"):
    """Formal health only; expected recovery reasons must be named by the test."""
    deadline = time.monotonic() + seconds
    while True:
        response = client.get("/health/ready")
        if _readiness_finished(response, pending_reason):
            assert time.monotonic() <= deadline, "readiness exceeded fixture deadline"
            return response
        remaining = deadline - time.monotonic()
        assert remaining > 0, ("readiness did not settle", pending_reason)
        time.sleep(min(.01, remaining))


async def wait_for_readiness_async(client):
    """Call within the caller's existing lifespan-entry timeout (no new budget)."""
    while not _readiness_finished(await client.get("/health/ready"), "STARTING"):
        await asyncio.sleep(.01)


def wait_startup_completion(client, *, seconds=5):
    """Observe the owned first stage without cancelling it or invoking a probe."""
    async def join():
        async with asyncio.timeout(seconds):
            await asyncio.shield(client.app.state.startup_task)
    client.portal.call(join)


def execute_sql(sql: str, *args) -> None:
    async def run() -> None:
        conn = await asyncpg.connect(
            host=PG_HOST,
            port=PG_PORT,
            user=PG_USER,
            password=PG_PASSWORD,
            database=TEST_DB,
        )
        try:
            await conn.execute(sql, *args)
        finally:
            await conn.close()

    asyncio.run(run())


def register_and_login(
    client: TestClient,
    email: str = "user@example.com",
    password: str = "SmokePass123",
    client_type: str = "desktop",
) -> dict:
    status = client.post(
        "/api/auth/register", json={"email": email, "password": password}
    )
    assert status.status_code == 201, status.text
    body = {
        "email": email,
        "password": password,
        "client_type": client_type,
    }
    if client_type == "desktop":
        body["device"] = {
            "installation_id": fixture_uuid("test-installation"),
            "name": "test-mac",
            "platform": "macos",
            "client_version": "0.1.0",
        }
    response = client.post("/api/auth/login", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def auth_headers(tokens: dict) -> dict:
    return {"Authorization": f"Bearer {tokens['access_token']}"}
