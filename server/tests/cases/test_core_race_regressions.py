"""Requires isolated real PostgreSQL; mocks exercise races, not audio/model acceptance."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
import uuid

from app.providers.llm.base import LlmResponse
from tests.conftest import auth_headers, register_and_login
from tests.test_sessions import create_session, frame


def test_parallel_duplicate_create_reserves_one_session(app_client):
    headers = auth_headers(register_and_login(app_client))
    request_id = str(uuid.uuid4())
    barrier = threading.Barrier(2)
    def create():
        barrier.wait()
        return create_session(app_client, headers, request_id)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(create) for _ in range(2)]
        responses = [future.result(timeout=5) for future in futures]
    assert sorted(r.status_code for r in responses) == [200, 201]
    assert len({r.json()["session_id"] for r in responses}) == 1


def test_parallel_distinct_creates_enforce_per_user_active_limit(app_client):
    headers = auth_headers(register_and_login(app_client))
    barrier = threading.Barrier(2)
    def create():
        barrier.wait()
        return create_session(app_client, headers, str(uuid.uuid4()))
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(create) for _ in range(2)]
        responses = [future.result(timeout=5) for future in futures]
    assert sorted(r.status_code for r in responses) == [201, 409]
    rejected = next(r for r in responses if r.status_code == 409)
    assert rejected.json()["error"]["code"] == "ACTIVE_SESSION_EXISTS"


def test_cancel_wins_against_late_result_and_asr_pcm_already_released(app_client, monkeypatch):
    manager = app_client.app.state.sessions
    entered, cancelled = threading.Event(), threading.Event()
    headers = auth_headers(register_and_login(app_client))
    session = create_session(app_client, headers, str(uuid.uuid4())).json()
    sid = session["session_id"]
    captured = manager.runtimes[sid]
    async def late_generate(request, deadline):
        assert captured.audio == b""
        assert captured.asr is None
        entered.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            # Deliberately misbehaving adapter: returning a late success must
            # still lose to the persisted cancellation CAS.
            return LlmResponse(parsed={"candidate_text": request.input["transcript"], "edits": []},
                               provider="stub", model_id="fixed")
    monkeypatch.setattr(manager.refiner.llm, "generate", late_generate)
    with app_client.websocket_connect(f"/ws/v1/sessions/{sid}/audio", headers=headers) as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_bytes(frame(0))
        assert ws.receive_json()["type"] == "audio.ack"
        ws.send_json({"type": "audio.end", "last_seq": 0, "total_samples": 4000})
        while ws.receive_json()["type"] != "asr.final":
            pass
        assert entered.wait(2)
        response = app_client.post(f"/api/v1/sessions/{sid}/cancel", headers=headers)
        assert response.status_code == 200
        assert cancelled.wait(2)
        assert response.json()["state"] == "cancelled"
    fetched = app_client.get(f"/api/v1/sessions/{sid}", headers=headers).json()
    assert fetched["state"] == "cancelled"
    assert fetched["result"] is None
    assert fetched["current_result_id"] is None
    assert captured.audio == b""
    assert sid not in manager.runtimes
