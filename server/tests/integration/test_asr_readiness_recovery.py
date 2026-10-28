"""Real isolated PostgreSQL/API lifecycle; Mock provider preparation fault only.

No physical model, network server, speech-quality, or performance claim. Keep
product preparation/session deadlines unchanged. The runner should retain its
normal process-level anti-hang bound for TestClient socket operations.
"""
from __future__ import annotations

import secrets
import threading

from fastapi.testclient import TestClient

from app.providers.asr.mock import MockAsrProvider
from tests.conftest import (SERVER_ROOT, TEST_URL, auth_headers, register_and_login,
                            wait_for_readiness, wait_startup_completion)
from tests.test_sessions import create_session, dictation, frame
from tests.ws_assertions import cancel_and_drain


def test_initial_asr_prepare_failure_recovers_existing_manager_and_accepts_recording(
    clean_database, monkeypatch,
):
    monkeypatch.setenv("SVI_DATABASE_URL", TEST_URL)
    monkeypatch.setenv("SVI_CONFIG", str(SERVER_ROOT / "config" / "app.yaml"))
    monkeypatch.setenv("SVI_INSTANCE_LOCK", "0")
    monkeypatch.setenv("SVI_ENVIRONMENT", "development")
    monkeypatch.setenv("SVI_ASR_PROVIDER", "mock")
    monkeypatch.setenv("SVI_LLM_PROVIDER", "mock")
    # Persist the same deployment secrets across the two app lifespans. Dev
    # defaults deliberately generate ephemeral secrets on every config load.
    monkeypatch.setenv("SVI_JWT_SECRET", secrets.token_urlsafe(48))
    monkeypatch.setenv("SVI_IDEMPOTENCY_SECRET", secrets.token_urlsafe(48))
    from app import main as main_module

    # Save real domain rows through existing authenticated APIs. A nonempty
    # history query exercises manager.output(), unlike an empty-list assertion.
    with TestClient(main_module.create_app()) as initial:
        wait_for_readiness(initial)
        credentials = auth_headers(register_and_login(
            initial, email="asr-recovery@example.test"))
        saved = create_session(initial, credentials, "asr-recovery-history")
        assert saved.status_code == 201
        saved_id = saved.json()["session_id"]
        events = dictation(initial, credentials, saved_id)
        assert events[-1]["type"] == "result.ready"
        expected_result = events[-1]["result"]

    allow_prepare = threading.Event()
    providers = []

    class FaultInjectedProvider(MockAsrProvider):
        def __init__(self):
            super().__init__()
            self.prepare_failed = False
            self.prepared = False
            self.closed = False
            self.opened = []

        async def prepare(self):
            if not allow_prepare.is_set():
                self.prepare_failed = True
                raise RuntimeError("intentional preparation failure")
            await super().prepare()
            self.prepared = True

        async def open(self, options):
            assert self.prepared and not self.closed
            self.opened.append(options)
            return await super().open(options)

        async def close(self):
            self.closed = True
            await super().close()

    def build_fault_provider(_config):
        provider = FaultInjectedProvider()
        providers.append(provider)
        return provider

    monkeypatch.setattr(main_module, "build_asr", build_fault_provider)
    with TestClient(main_module.create_app()) as recovered:
        wait_startup_completion(recovered)
        health = recovered.get("/health/ready")
        assert health.status_code == 503
        assert health.json()["reason_code"] == "ASR_UNAVAILABLE"
        manager = recovered.app.state.sessions
        assert manager is not None
        assert any(provider.prepare_failed and provider.closed for provider in providers)
        history = recovered.get("/api/v1/history", headers=credentials)
        assert history.status_code == 200
        row = next(item for item in history.json()["items"] if item["session_id"] == saved_id)
        assert row["result"]["result_id"] == expected_result["result_id"]
        assert row["result"]["refined_text"] == expected_result["refined_text"]
        unavailable = create_session(recovered, credentials, "asr-recovery-still-unavailable")
        assert unavailable.status_code == 503
        assert unavailable.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
        assert not any(provider.opened for provider in providers)

        # Let the original periodic probe retry preparation; observe formal
        # health without manually invoking a concurrent readiness refresh.
        allow_prepare.set()
        wait_for_readiness(recovered, pending_reason="ASR_UNAVAILABLE")
        assert recovered.get("/health/ready").status_code == 200
        assert recovered.app.state.sessions is manager
        live_provider = recovered.app.state.asr
        assert live_provider.prepared and not live_provider.closed

        # Main failure oracle: readiness must be usable by the real create path.
        # The unpatched version returns 503 here because manager still has None.
        created = create_session(recovered, credentials, "asr-recovery-new-recording")
        assert created.status_code == 201
        session_id = created.json()["session_id"]
        assert manager.asr_provider is live_provider
        assert len(live_provider.opened) == 1
        assert live_provider.opened[0].session_id == session_id
        with recovered.websocket_connect(
            f"/ws/v1/sessions/{session_id}/audio", headers=credentials,
        ) as ws:
            ready = ws.receive_json()
            assert ready["type"] == "ready" and ready["session_id"] == session_id
            ws.send_bytes(frame(0))
            assert ws.receive_json()["type"] == "audio.ack"
            cancel_and_drain(ws)
        assert recovered.app.state.sessions is manager
        final = recovered.get(f"/api/v1/sessions/{session_id}", headers=credentials)
        assert final.status_code == 200 and final.json()["state"] == "cancelled"
