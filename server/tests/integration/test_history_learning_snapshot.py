"""Real API/PG session snapshots with declared Mock ASR/LLM; no GUI/quality claim."""
from __future__ import annotations

import time

import pytest

from app.providers.asr.base import ProviderError
from app.providers.llm.base import LlmResponse
from tests.conftest import auth_headers, register_and_login
from tests.ids import fixture_uuid
from tests.test_sessions import dictation


@pytest.mark.parametrize("requested,initial_preference,new_preference,frozen", [
    (False, True, True, False),
    (True, False, True, False),
    (True, True, False, True),
])
def test_history_learning_is_the_frozen_session_value(
    app_client, monkeypatch, requested, initial_preference, new_preference, frozen,
):
    headers = auth_headers(register_and_login(app_client, email="history-frozen-learning@example.invalid"))

    def preference(enabled):
        before = app_client.get("/api/v1/settings", headers=headers)
        assert before.status_code == 200
        changed = app_client.patch("/api/v1/settings", headers=headers,
            json={"version": before.json()["version"], "learning_enabled": enabled})
        assert changed.status_code == 200
        assert changed.json()["learning_enabled"] is enabled

    preference(initial_preference)
    inner = app_client.app.state.sessions.refiner.llm.inner
    original = inner.generate

    async def unavailable(request, deadline):
        return LlmResponse(parsed=None, provider=inner.provider_id, model_id=inner.model_id,
            finish_reason="error", error=ProviderError("unavailable", "synthetic history fixture"))

    monkeypatch.setattr(inner, "generate", unavailable)
    key = fixture_uuid("history-frozen-learning-create")
    created = app_client.post("/api/v1/sessions", headers={**headers, "Idempotency-Key": key},
        json={"client_request_id": key, "learning_enabled": requested})
    assert created.status_code == 201
    assert created.json()["learning_enabled"] is frozen
    session_id = created.json()["session_id"]
    events = dictation(app_client, headers, session_id)
    assert events[-1]["type"] == "result.ready"
    assert events[-1]["result"]["review_required"] is True
    old_result_id = events[-1]["result"]["result_id"]
    raw = events[-1]["result"]["raw_text"]
    preference(new_preference)

    fetched = app_client.get(f"/api/v1/sessions/{session_id}", headers=headers)
    assert fetched.status_code == 200
    assert fetched.json()["learning_enabled"] is frozen
    assert fetched.json()["retry_kind"] == "refine"
    listing = app_client.get("/api/v1/history", headers=headers)
    assert listing.status_code == 200
    item = next(item for item in listing.json()["items"] if item["session_id"] == session_id)
    assert item["learning_enabled"] is frozen

    monkeypatch.setattr(inner, "generate", original)
    retry_key = fixture_uuid("history-frozen-learning-retry")
    retried = app_client.post(f"/api/v1/sessions/{session_id}/retry",
        headers={**headers, "Idempotency-Key": retry_key}, json={"retry_kind": "refine"})
    assert retried.status_code == 202
    assert retried.json()["learning_enabled"] is frozen
    deadline = time.monotonic() + 32  # Anti-hang only; production 10/30 budgets unchanged.
    while True:
        response = app_client.get(f"/api/v1/sessions/{session_id}", headers=headers)
        assert response.status_code == 200
        body = response.json()
        assert body["learning_enabled"] is frozen
        if body["state"] == "ready":
            break
        assert body["state"] == "refining"
        assert time.monotonic() < deadline
        time.sleep(.05)
    assert body["current_attempt_no"] == 2
    assert body["result"]["attempt_kind"] == "refine"
    assert body["result"]["result_id"] != old_result_id
    assert body["result"]["raw_text"] == raw
    replay = app_client.post(f"/api/v1/sessions/{session_id}/retry",
        headers={**headers, "Idempotency-Key": retry_key}, json={"retry_kind": "refine"})
    assert replay.status_code == 200 and replay.json()["replayed"] is True
    assert replay.json()["learning_enabled"] is frozen
    assert replay.json()["result"]["result_id"] == body["result"]["result_id"]
