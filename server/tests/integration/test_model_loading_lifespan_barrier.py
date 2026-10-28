"""DB recovery precedes serving; model preparation stays in the owned task.

Real process-owned PostgreSQL, formal ASGI HTTP/WS and real lifespan cleanup.
The declared Mock provider only gates preparation: no TCP, physical ASR/LLM,
startup performance, microphone, or real advisory-loss claim is made here.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
import uuid

import httpx
import pytest
from sqlalchemy import text

from app import main
from app.providers.asr.mock import MockAsrProvider
from tests.conftest import SERVER_ROOT, TEST_URL
from tests.integration.test_instance_recovery_loss import require_process_owned_database


async def signal(event, seconds=5):
    # Test anti-hang, never a timeout passed into production work.
    async with asyncio.timeout(seconds):
        await event.wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["ready", "cancel-loading", "factory-error"])
async def test_loading_is_visible_only_after_recovery_and_owned_until_settled(
    clean_database, monkeypatch, record_property, outcome,
):
    require_process_owned_database()
    for name, value in {
        "SVI_DATABASE_URL": TEST_URL, "SVI_CONFIG": str(SERVER_ROOT / "config/app.yaml"),
        "SVI_INSTANCE_LOCK": "0", "SVI_ENVIRONMENT": "development",
        "SVI_ASR_PROVIDER": "mock", "SVI_LLM_PROVIDER": "mock",
        "WEB_CONCURRENCY": "1", "UVICORN_WORKERS": "1",
    }.items():
        monkeypatch.setenv(name, value)
    recovery_entered, allow_recovery = asyncio.Event(), asyncio.Event()
    prepare_entered, allow_prepare = asyncio.Event(), asyncio.Event()
    serving, stop = asyncio.Event(), asyncio.Event()
    observed = SimpleNamespace(recovery_count=0, prepare_count=0, prepare_owner=None,
        prepare_cancel_count=0, close_count=0, db_close_count=0, pool_before_dispose=None)
    actual_recovery, actual_database, actual_build_llm = (
        main._recover_interrupted_sessions, main.Database, main.build_llm)

    async def gated_recovery(database, *, identities=None):
        result = await actual_recovery(database, identities=identities)
        observed.recovery_count += 1
        recovery_entered.set()
        await allow_recovery.wait()
        return result

    class LoadingAsr(MockAsrProvider):
        async def prepare(self):
            observed.prepare_count += 1
            observed.prepare_owner = asyncio.current_task()
            prepare_entered.set()
            try:
                await allow_prepare.wait()
                await super().prepare()
            except asyncio.CancelledError:
                observed.prepare_cancel_count += 1
                raise

        async def close(self):
            observed.close_count += 1
            await super().close()

    def database_factory(config):
        database = actual_database(config)
        close = database.close

        async def observed_close():
            observed.pool_before_dispose = database.engine.pool.checkedout()
            await close()
            observed.db_close_count += 1

        database.close = observed_close
        return database

    def build_llm(config):
        if outcome == "factory-error":
            raise ValueError("test-only invalid provider configuration")
        return actual_build_llm(config)

    provider = LoadingAsr()
    monkeypatch.setattr(main, "Database", database_factory)
    monkeypatch.setattr(main, "_recover_interrupted_sessions", gated_recovery)
    monkeypatch.setattr(main, "build_asr", lambda config: provider)
    monkeypatch.setattr(main, "build_llm", build_llm)
    app = main.create_app(str(SERVER_ROOT / "config/app.yaml"))

    async def run_lifespan():
        async with app.router.lifespan_context(app):
            serving.set()
            await stop.wait()

    parent = asyncio.create_task(run_lifespan())
    try:
        await signal(recovery_entered, 8)
        assert app.state.schema_ok and app.state.database_ok
        assert not serving.is_set() and not prepare_entered.is_set()
        assert app.state.sessions is None
        allow_recovery.set()
        await signal(prepare_entered)
        # Before-candidate negative control: the old lifespan still waits for
        # this gated prepare, so it cannot reach the ASGI startup-complete body.
        await signal(serving, 2)
        assert observed.prepare_owner is app.state.startup_task
        assert not app.state.startup_task.done()
        assert app.state.probe_task is app.state.cleanup_task is None
        assert app.state.startup_recovery_complete.done()
        assert app.state.startup_recovery_complete.result() is None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url="http://testserver") as client:
            assert (await client.get("/health/live")).status_code == 200
            health = await client.get("/health/ready")
            assert health.status_code == 503 and health.json()["reason_code"] == "STARTING"
            email = f"loading-{uuid.uuid4()}@example.test"
            credentials = {"email": email, "password": "Fictional-Only-9!"}
            assert (await client.post("/api/auth/register", json=credentials)).status_code == 201
            login = await client.post("/api/auth/login", json={**credentials,
                "client_type": "desktop", "device": {"installation_id": str(uuid.uuid4()),
                "name": "loading-test", "platform": "macos", "client_version": "0.0.1"}})
            assert login.status_code == 200
            headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
            caps = await client.get("/api/v1/capabilities", headers=headers)
            assert caps.status_code == 200 and caps.json()["ready"] is False
            assert caps.json()["unavailable_reason"] == "STARTING"
            request_id, missing_session = str(uuid.uuid4()), str(uuid.uuid4())
            correction = {"session_id": missing_session, "insertion_id": str(uuid.uuid4()),
                "correction_id": str(uuid.uuid4()), "offset": 0, "before": "甲", "after": "乙",
                "left": "", "right": ""}
            responses = [
                await client.get("/api/v1/history", headers=headers),
                await client.post("/api/v1/corrections", headers=headers, json=correction),
                await client.post("/api/v1/sessions", headers={**headers, "Idempotency-Key": request_id},
                                  json={"client_request_id": request_id}),
                await client.post(f"/api/v1/sessions/{missing_session}/retry",
                                  headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
                                  json={"retry_kind": "asr"}),
            ]
            for response in responses:
                assert response.status_code == 503
                error = response.json()["error"]
                assert error["code"] == "SERVICE_UNAVAILABLE" and error["retryable"] is True
                assert error["request_id"] == response.headers["x-request-id"]
            messages = []

            async def receive():
                return {"type": "websocket.connect"}

            async def send(message):
                messages.append(message)

            await app({"type": "websocket", "asgi": {"version": "3.0"}, "scheme": "ws",
                "path": f"/ws/v1/sessions/{missing_session}/audio", "query_string": b"", "root_path": "",
                "headers": [(b"authorization", headers["Authorization"].encode())],
                "client": ("127.0.0.1", 1234), "server": ("testserver", 80),
                "subprotocols": [], "extensions": {"websocket.http.response": {}}}, receive, send)
            assert [message["type"] for message in messages] == [
                "websocket.http.response.start", "websocket.http.response.body"]
            assert messages[0]["status"] == 503
            assert json.loads(messages[1]["body"])["error"]["code"] == "SERVICE_UNAVAILABLE"
            async with app.state.db.session_factory() as db:
                for table in ("sessions", "session_attempts", "operation_requests", "results",
                              "insertions", "learning_events", "model_calls"):
                    assert await db.scalar(text(f"SELECT count(*) FROM {table}")) == 0, table
            if outcome == "cancel-loading":
                parent.cancel()
                async with asyncio.timeout(8):
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.shield(parent)
                assert app.state.startup_task.cancelled()
                assert observed.prepare_cancel_count == 1
                assert app.state.unavailable_reason == "SERVER_SHUTDOWN"
                assert app.state.probe_task is app.state.cleanup_task is None
            else:
                allow_prepare.set()
                async with asyncio.timeout(8):
                    if outcome == "factory-error":
                        with pytest.raises(ValueError):
                            await asyncio.shield(app.state.startup_task)
                    else:
                        await asyncio.shield(app.state.startup_task)
                health = await client.get("/health/ready")
                if outcome == "factory-error":
                    assert health.status_code == 503
                    assert health.json()["reason_code"] == "STARTUP_DEPENDENCY_UNAVAILABLE"
                    assert app.state.probe_task is app.state.cleanup_task is None
                    assert (await client.get("/api/v1/history", headers=headers)).status_code == 503
                else:
                    assert health.status_code == 200 and app.state.asr is provider
                    assert app.state.probe_task is not None and app.state.cleanup_task is not None
                    history = await client.get("/api/v1/history", headers=headers)
                    assert history.status_code == 200 and history.json()["items"] == []
                    # Same formal route now reaches the tenant lookup, not a
                    # permanently installed loading rejection.
                    assert (await client.post("/api/v1/corrections", headers=headers,
                                              json=correction)).status_code == 404
                    created = await client.post("/api/v1/sessions",
                        headers={**headers, "Idempotency-Key": request_id},
                        json={"client_request_id": request_id})
                    assert created.status_code == 201
                    cancelled = await client.post(
                        f"/api/v1/sessions/{created.json()['session_id']}/cancel", headers=headers)
                    assert cancelled.status_code == 200 and cancelled.json()["state"] == "cancelled"
                stop.set()
                async with asyncio.timeout(8):
                    await asyncio.shield(parent)
        assert observed.recovery_count == observed.prepare_count == observed.close_count == 1
        assert observed.db_close_count == 1 and observed.pool_before_dispose == 0
        assert app.state.shutdown_task.done() and not app.state.shutdown_task.cancelled()
        for name in ("startup_task", "probe_task", "cleanup_task", "llm_prepare_task"):
            task = getattr(app.state, name, None)
            assert task is None or task.done(), name
        record_property("outcome", outcome)
        record_property("db_recovery_calls", observed.recovery_count)
        record_property("prepare_calls", observed.prepare_count)
        record_property("prepare_cancel_calls", observed.prepare_cancel_count)
        record_property("pool_before_dispose", observed.pool_before_dispose)
        record_property("scope", "owned-PG/formal-ASGI/mock-prepare/no-TCP/no-physical-model")
    finally:
        allow_recovery.set()
        allow_prepare.set()
        stop.set()
        if not parent.done():
            async with asyncio.timeout(8):
                await asyncio.shield(parent)
        if not parent.cancelled():
            parent.result()  # Retrieve any unexpected lifespan error.
