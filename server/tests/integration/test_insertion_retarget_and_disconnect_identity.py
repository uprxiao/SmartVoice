"""INSERT18/VOICE18 server-only missing sequence/identity controls.

Real API/WS/PG, synthetic PCM, delegated existing Mock providers. No native
zero-write verification, focus behavior, microphone or real model claim.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime

import httpx

from app.providers.asr.mock import MockAsrSession
from app.providers.llm.mock import MockLlmProvider
from app.services.inference_context import current_scope
from tests.conftest import auth_headers, register_and_login
from tests.cases.test_dict_cases import key
from tests.integration.test_insertion_device_role_matrix import business_snapshot, stats
from tests.integration.test_normal_shutdown_contract import WsProbe
from tests.test_insertions_learning import claim, receipt
from tests.test_sessions import create_session, dictation


RAW = "嗯，先保存日志，然后检查配置。"
REFINED = "先保存日志，然后检查配置。"


def observe_provider_dispatches(client, monkeypatch, before_finish=None):
    """Count real provider entry, always delegate; never create ledger records."""
    manager = client.app.state.sessions
    assert client.app.state.config.asr.provider == client.app.state.config.llm.provider == "mock"
    manager.asr_provider.text = RAW
    observed = {"open": [], "finish": [], "llm": []}
    actual_open = manager.asr_provider.open
    actual_finish, actual_generate = MockAsrSession.finish, MockLlmProvider.generate

    async def opened(options):
        observed["open"].append((options.user_id, options.session_id, options.attempt_no))
        return await actual_open(options)

    async def finished(stream, total_samples, deadline):
        scope = current_scope.get()
        assert scope is not None
        assert (scope.user_id, scope.session_id, scope.attempt_no) == (
            stream.options.user_id, stream.options.session_id, stream.options.attempt_no,
        )
        observed["finish"].append((scope.user_id, scope.session_id, scope.attempt_no, total_samples))
        if before_finish is not None:
            await before_finish()
        return await actual_finish(stream, total_samples, deadline)

    async def generated(provider, request, deadline):
        scope = current_scope.get()
        assert scope is not None and request.call_id
        observed["llm"].append((scope.user_id, scope.session_id, scope.attempt_no,
                                request.purpose, request.call_id))
        return await actual_generate(provider, request, deadline)

    monkeypatch.setattr(manager.asr_provider, "open", opened)
    monkeypatch.setattr(MockAsrSession, "finish", finished)
    monkeypatch.setattr(MockLlmProvider, "generate", generated)
    return observed


def assert_single_processing(observed, owner, session_id, samples):
    assert observed["open"] == [(owner, session_id, 1)]
    assert observed["finish"] == [(owner, session_id, 1, samples)]
    assert [entry[:4] for entry in observed["llm"]] == [
        (owner, session_id, 1, "refine"),
    ]
    call_ids = [entry[4] for entry in observed["llm"]]
    assert len(set(call_ids)) == 1


def assert_identity(snapshot, owner, session_id, result):
    assert len(snapshot["sessions"]) == len(snapshot["session_attempts"]) == len(snapshot["results"]) == 1
    session, attempt, stored = (snapshot[table][0] for table in ("sessions", "session_attempts", "results"))
    assert (session["id"], session["user_id"], session["current_attempt_no"], session["current_result_id"]) == (
        session_id, owner, 1, result["result_id"],
    )
    assert session["state"] == "ready" and session["audio_complete"] is True
    assert (attempt["user_id"], attempt["session_id"], attempt["attempt_no"], attempt["kind"]) == (
        owner, session_id, 1, "initial",
    )
    assert (stored["id"], stored["user_id"], stored["session_id"], stored["attempt_no"]) == (
        result["result_id"], owner, session_id, 1,
    )
    assert result["attempt_no"] == 1 and result["attempt_kind"] == "initial"
    assert (stored["raw_text"], stored["refined_text"], stored["text_hash"]) == (
        RAW, REFINED, result["text_hash"],
    )


def assert_no_learning(snapshot):
    assert snapshot["learning_events"] == snapshot["dictionary_terms"] == []
    assert len(snapshot["dictionary_state"]) == 1
    assert snapshot["dictionary_state"][0]["version"] == 0


def test_tc_insert_18_old_failed_claim_replay_after_new_insertion_is_read_only(app_client, monkeypatch):
    tokens = register_and_login(app_client)
    headers, owner = auth_headers(tokens), tokens["user"]["id"]
    observed = observe_provider_dispatches(app_client, monkeypatch)
    created = create_session(app_client, headers, key())
    assert created.status_code == 201, created.text
    session_id = created.json()["session_id"]
    events = dictation(app_client, headers, session_id)
    assert events[-1]["type"] == "result.ready", events[-1]
    result = events[-1]["result"]
    assert result["raw_text"] == RAW and result["refined_text"] == REFINED
    assert result["review_required"] is False
    assert_single_processing(observed, owner, session_id, 8000)
    processing = deepcopy(observed)
    original = business_snapshot(owner)
    assert_identity(original, owner, session_id, result)
    assert original["insertions"] == []
    assert_no_learning(original)

    old_id, new_id = key(), key()
    old_claim = claim(app_client, headers, session_id, result, old_id)
    assert old_claim.status_code == 201 and old_claim.json()["status"] == "reserved"
    assert old_claim.json()["replayed"] is False
    zero_write = receipt(app_client, headers, session_id, old_id, "failed", False, "ZERO_WRITE")
    assert zero_write.status_code == 200, zero_write.text
    assert zero_write.json()["status"] == "failed" and zero_write.json()["replayed"] is False
    assert zero_write.json()["completed_at"] is not None
    assert stats(app_client, headers, session_id)["inserted_sessions"] == 0

    # This API call represents explicit retarget intent. No native effect is
    # asserted: ZERO_WRITE is a declared valid receipt, not a simulated OS read.
    new_claim = claim(app_client, headers, session_id, result, new_id)
    assert new_claim.status_code == 201 and new_claim.json()["status"] == "reserved"
    assert new_claim.json()["replayed"] is False
    assert new_claim.json()["result_id"] == result["result_id"]
    inserted = receipt(app_client, headers, session_id, new_id, "inserted", True)
    assert inserted.status_code == 200, inserted.text
    assert inserted.json()["status"] == "inserted" and inserted.json()["replayed"] is False
    stable = business_snapshot(owner)
    assert_identity(stable, owner, session_id, result)
    assert_no_learning(stable)
    rows = {row["id"]: row for row in stable["insertions"]}
    assert set(rows) == {old_id, new_id}
    assert (rows[old_id]["status"], rows[old_id]["error_code"]) == ("failed", "ZERO_WRITE")
    assert rows[new_id]["status"] == "inserted" and rows[new_id]["observable"] is True
    assert rows[new_id]["error_code"] is None
    assert len({row["result_id"] for row in rows.values()}) == 1
    assert sum(row["status"] in ("reserved", "inserted", "unverified") for row in rows.values()) == 1
    for table in ("sessions", "session_attempts", "results", "dictionary_state", "dictionary_terms",
                  "learning_events", "model_calls", "model_usage_daily", "operation_requests"):
        assert stable[table] == original[table], table
    totals = stats(app_client, headers, session_id)
    assert (totals["sessions"], totals["result_sessions"], totals["inserted_sessions"], totals["learning_events"]) == (1, 1, 1, 0)
    assert sum(row["inserted_sessions"] for row in stable["usage_daily"]) == 1
    assert stable["session_contributions"][0]["inserted"] is True
    assert observed == processing

    # Last operations replay the OLD identity, after NEW already became inserted.
    replay_claim = claim(app_client, headers, session_id, result, old_id)
    assert replay_claim.status_code == 200, replay_claim.text
    assert replay_claim.json() == {**zero_write.json(), "replayed": True}
    assert business_snapshot(owner) == stable
    replay_receipt = receipt(app_client, headers, session_id, old_id, "failed", False, "ZERO_WRITE")
    assert replay_receipt.status_code == 200, replay_receipt.text
    assert replay_receipt.json() == {**zero_write.json(), "replayed": True}
    assert business_snapshot(owner) == stable
    assert stats(app_client, headers, session_id) == totals
    current = app_client.get(f"/api/v1/sessions/{session_id}", headers=headers)
    assert current.status_code == 200, current.text
    assert current.json()["insertion_status"] == "inserted"
    current_result = current.json()["result"]
    assert {k: v for k, v in current_result.items() if k != "created_at"} == {
        k: v for k, v in result.items() if k != "created_at"
    }
    # WS serializes UTC as +00:00 while Pydantic HTTP may emit Z: compare
    # the exact instant rather than demanding a non-contractual spelling.
    assert datetime.fromisoformat(current_result["created_at"]) == datetime.fromisoformat(result["created_at"])
    new_read = app_client.get(f"/api/v1/sessions/{session_id}/insertions/{new_id}", headers=headers)
    assert new_read.status_code == 200 and new_read.json() == inserted.json()
    assert observed == processing


def test_tc_voice_18_post_end_disconnect_keeps_identity_and_single_dispatch(app_client, monkeypatch):
    tokens = register_and_login(app_client)
    headers, owner = auth_headers(tokens), tokens["user"]["id"]

    async def scenario():
        app = app_client.app
        assert app.state.config.asr.finish_timeout_ms == app.state.config.llm.call_timeout_ms == 10000
        assert app.state.config.pipeline.stop_to_result_budget_ms == 30000
        entered, release = asyncio.Event(), asyncio.Event()

        async def before_finish():
            entered.set()
            await release.wait()  # Still charged to production's original 10 s.

        observed = observe_provider_dispatches(app_client, monkeypatch, before_finish)
        wire = None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as http:
            try:
                request_id = key()
                created = await http.post("/api/v1/sessions", headers=headers | {"Idempotency-Key": request_id},
                    json={"client_request_id": request_id, "learning_enabled": True})
                assert created.status_code == 201, created.text
                session_id = created.json()["session_id"]
                assert created.json()["current_attempt_no"] == 1
                wire = await WsProbe(app, headers, session_id).start()
                await wire.end()
                assert [event["type"] for event in wire.events] == ["ready", "audio.ack", "audio.end_ack"]
                assert wire.events[-1]["attempt_no"] == 1
                async with asyncio.timeout(2):
                    await entered.wait()
                runtime = app.state.sessions.runtimes[session_id]
                processing_task = runtime.task
                assert processing_task is not None and not processing_task.done()
                assert observed["llm"] == []
                before_disconnect = deepcopy(observed)

                await wire.send({"type": "websocket.disconnect", "code": 1000})
                async with asyncio.timeout(2):
                    await wire.task  # A cancelled/error ASGI task is failure, never swallowed.
                assert wire.task.done() and not wire.task.cancelled() and wire.task.exception() is None
                closed = await wire.message()
                assert closed["type"] == "websocket.close" and closed["code"] == 1000
                assert runtime.connected is False and not processing_task.done()
                pending = await http.get(f"/api/v1/sessions/{session_id}", headers=headers)
                assert pending.status_code == 200, pending.text
                pending_body = pending.json()
                assert (pending_body["session_id"], pending_body["current_attempt_no"], pending_body["state"]) == (session_id, 1, "finalizing")
                assert pending_body["audio_complete"] is True and pending_body["result"] is None
                assert observed == before_disconnect

                release.set()
                # Anti-hang guard does not modify either production deadline.
                async with asyncio.timeout(app.state.config.pipeline.stop_to_result_budget_ms / 1000 + 2):
                    await asyncio.shield(processing_task)
                first = await http.get(f"/api/v1/sessions/{session_id}", headers=headers)
                assert first.status_code == 200, first.text
                body = first.json()
                assert body["session_id"] == session_id and body["current_attempt_no"] == 1
                assert body["state"] == "ready" and body["audio_complete"] is True
                result = body["result"]
                assert result["raw_text"] == RAW and result["refined_text"] == REFINED
                assert result["attempt_no"] == 1 and result["attempt_kind"] == "initial"
                assert result["review_required"] is False
                assert body["current_result_id"] == result["result_id"]
                assert_single_processing(observed, owner, session_id, 4000)
                processing = deepcopy(observed)
                for _ in range(2):
                    fetched = await http.get(f"/api/v1/sessions/{session_id}", headers=headers)
                    assert fetched.status_code == 200 and fetched.json() == body
                    assert observed == processing
                return session_id, result, observed
            finally:
                release.set()
                # Failure-only cleanup; no cancellation is accepted as a pass.
                if wire is not None and wire.task is not None and not wire.task.done():
                    wire.task.cancel()
                    async with asyncio.timeout(4):
                        await asyncio.gather(wire.task, return_exceptions=True)

    session_id, result, observed = app_client.portal.call(scenario)
    processing = deepcopy(observed)
    persisted = business_snapshot(owner)
    assert_identity(persisted, owner, session_id, result)
    assert persisted["insertions"] == []
    assert_no_learning(persisted)
    calls = persisted["model_calls"]
    assert {row["id"] for row in calls} == {entry[4] for entry in observed["llm"]}
    assert sorted(row["purpose"] for row in calls) == ["refine"]
    assert all(row["user_id"] == owner and row["session_id"] == session_id and row["attempt_no"] == 1
               and row["status"] == "succeeded" for row in calls)
    # Mock ASR exposes real finish entry above but no physical decoder ledger;
    # do not invent an asr_finish row or claim GPU/microphone usage evidence.
    assert stats(app_client, headers, session_id)["inserted_sessions"] == 0
    assert stats(app_client, headers, session_id)["learning_events"] == 0
    fetched = app_client.get(f"/api/v1/sessions/{session_id}", headers=headers)
    assert fetched.status_code == 200 and fetched.json()["result"] == result
    assert business_snapshot(owner) == persisted
    assert observed == processing
