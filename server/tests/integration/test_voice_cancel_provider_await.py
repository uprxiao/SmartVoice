"""VOICE19: actual API/WS/PG cancellation at two provider-await boundaries.

Only the existing Mock physical finish/generate is gated. The original provider,
ScheduledLlmProvider, recorder, CAS and deadlines still execute. This is neither
real-model/PCM-decoder nor Desktop/native acceptance. Prepared, not executed.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.services.inference_context import current_scope
from tests.conftest import auth_headers, register_and_login
from tests.integration.test_insertion_device_role_matrix import business_snapshot
from tests.integration.test_normal_shutdown_contract import WsProbe
from tests.integration.test_voice_cancel_boundaries import (
    assert_cancelled, create, facts, settle, until_fact,
)


MODES = ("normal-release", "propagate-cancel", "late-delegate-after-cancel")


async def all_business(owner):
    # Existing helper uses one real PG statement/snapshot for all 12 tables.
    # Its sync wrapper owns an event loop, so run it outside the application loop.
    return await asyncio.to_thread(business_snapshot, owner)


def without_model_outcomes(value):
    # A dispatched call may legitimately finish recording after cancel commits.
    # Exclude only these two projections during that interval, never at replay.
    return {key: rows for key, rows in value.items()
            if key not in {"model_calls", "model_usage_daily"}}


def assert_call_outcomes(snapshot, owner, sid, expected):
    rows = snapshot["model_calls"]
    assert sorted((row["purpose"], row["status"], row["error_code"]) for row in rows) == sorted(expected)
    assert all(row["user_id"] == owner and row["session_id"] == sid and row["attempt_no"] == 1
               and row["learning_event_id"] is None and row["provider"] == "mock"
               and row["model_id"] == "mock-llm" and row["call_no"] == 1
               and row["ended_at"] is not None and row["aggregated_at"] is not None
               and row["prompt_tokens"] is None and row["completion_tokens"] is None
               for row in rows)
    daily = snapshot["model_usage_daily"]
    assert sum(row["calls"] for row in daily) == len(rows)
    assert sum(row["successes"] for row in daily) == sum(row["status"] == "succeeded" for row in rows)
    assert sum(row["unknown_calls"] for row in daily) == sum(row["status"] == "unknown" for row in rows)
    assert sum(row["failures"] for row in daily) == sum(row["status"] != "succeeded" for row in rows)
    assert sum(row["prompt_tokens_unknown_calls"] for row in daily) == len(rows)
    assert sum(row["completion_tokens_unknown_calls"] for row in daily) == len(rows)
    # Known-token sums being zero must not erase the unknown-call dimension.
    assert all(row["prompt_tokens_known"] == row["completion_tokens_known"] == 0 for row in daily)


@pytest.mark.parametrize("stage", ["asr_finish", "llm_generate"])
@pytest.mark.parametrize("mode", MODES)
def test_tc_voice_19_cancel_at_provider_await_has_no_late_result_or_replay_effect(
    app_client, monkeypatch, record_property, stage, mode,
):
    pair = register_and_login(app_client, email="voice19-provider@example.invalid")
    headers, owner = auth_headers(pair), pair["user"]["id"]

    async def scenario():
        app, manager = app_client.app, app_client.app.state.sessions
        entered, released, cancellation_seen = asyncio.Event(), asyncio.Event(), asyncio.Event()
        trace = {"gate_entries": 0, "gate_cancels": 0,
                 "finish_entries": 0, "finish_delegates": 0, "finish_returns": 0,
                 "generate_entries": [], "generate_delegates": [], "generate_returns": [],
                 "delegate_after_committed_cancel": False}
        observed = {"committed": False}
        wire = cancel_task = None
        loop = asyncio.get_running_loop()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as http:
            sid = await create(http, headers, f"voice19-{stage}-{mode}")
            runtime = manager.runtimes[sid]
            original_audio, stream = runtime.audio, runtime.asr
            llm = app.state.llm
            actual_finish, actual_generate = stream.finish, llm.inner.generate
            assert manager.refiner.llm is llm and llm.recorder is app.state.recorder
            assert app.state.config.asr.finish_timeout_ms == app.state.config.llm.call_timeout_ms == 10000
            assert app.state.config.pipeline.stop_to_result_budget_ms == 30000

            async def gate(deadline):
                trace["gate_entries"] += 1
                assert trace["gate_entries"] == 1
                observed["deadline"] = deadline
                observed["entered_at"] = loop.time()
                assert 0 < deadline - observed["entered_at"] <= 10
                entered.set()
                while not released.is_set():
                    try:
                        await released.wait()
                    except asyncio.CancelledError:
                        trace["gate_cancels"] += 1
                        cancellation_seen.set()
                        if mode != "late-delegate-after-cancel":
                            raise
                # A late-success control is only valid within the unchanged
                # original physical budget; never revive an expired operation.
                assert loop.time() < deadline
                trace["delegate_after_committed_cancel"] = observed["committed"]

            async def finish(total_samples, deadline):
                scope = current_scope.get()
                assert scope and (scope.user_id, scope.session_id, scope.attempt_no) == (owner, sid, 1)
                assert total_samples == stream.samples == 4000
                trace["finish_entries"] += 1
                if stage == "asr_finish":
                    await gate(deadline)
                trace["finish_delegates"] += 1
                answer = await actual_finish(total_samples, deadline)
                assert answer.processed_samples == 4000 and answer.text == stream.text and not answer.no_speech
                trace["finish_returns"] += 1
                return answer

            async def generate(request, deadline):
                scope = current_scope.get()
                assert scope and (scope.user_id, scope.session_id, scope.attempt_no) == (owner, sid, 1)
                assert scope.learning_event_id is None and not scope.synthetic and request.call_id
                trace["generate_entries"].append((request.purpose, request.call_id))
                if stage == "llm_generate" and request.purpose == "refine":
                    await gate(deadline)
                trace["generate_delegates"].append(request.purpose)
                answer = await actual_generate(request, deadline)
                assert answer.error is None and answer.finish_reason == "stop"
                assert answer.provider == "mock" and answer.model_id == llm.model_id
                trace["generate_returns"].append(request.purpose)
                return answer

            monkeypatch.setattr(stream, "finish", finish)
            monkeypatch.setattr(llm.inner, "generate", generate)
            try:
                wire = WsProbe(app, headers, sid)
                await wire.start()
                assert original_audio is runtime.audio and len(original_audio) == 8000
                await wire.end()
                wire.drain_task = asyncio.create_task(wire.drain())
                async with asyncio.timeout(3):
                    await entered.wait()
                before = await facts(app, owner, sid)
                original_deadline = runtime.deadline
                assert original_deadline == runtime.end_accepted_at + 30
                assert before["session"]["state"] == ("finalizing" if stage == "asr_finish" else "refining")
                assert before["session"]["audio_complete"] and before["session"]["attempt_no"] == 1
                assert before["counts"]["results"] == before["counts"]["insertions"] == before["counts"]["learning_events"] == 0
                assert before["counts"]["model_calls"] == (0 if stage == "asr_finish" else 1)
                assert before["attempt"]["has_raw"] == (stage == "llm_generate")
                if stage == "asr_finish":
                    assert len(original_audio) == 8000 and runtime.asr is stream
                    assert observed["deadline"] == runtime.end_accepted_at + 10
                else:
                    assert runtime.audio == original_audio == b"" and runtime.asr is None
                    dispatched = await all_business(owner)
                    assert len(dispatched["model_calls"]) == 1
                    assert dispatched["model_calls"][0]["status"] == "running"
                    assert dispatched["model_calls"][0]["id"] == trace["generate_entries"][0][1]
                    assert llm.scheduler.snapshot()["running"] == 1
                if mode == "normal-release":
                    released.set()
                    await wire.drain_task
                    after = await facts(app, owner, sid)
                    stable = await all_business(owner)
                    assert after["session"]["state"] == after["attempt"]["state"] == "ready"
                    assert after["session"]["current_result_id"] is not None
                    assert after["counts"]["results"] == 1
                    assert after["counts"]["insertions"] == after["counts"]["learning_events"] == 0
                    assert len(stable["results"]) == 1 and stable["results"][0]["review_required"] is False
                    result_row = stable["results"][0]
                    assert (result_row["user_id"], result_row["session_id"], result_row["attempt_no"]) == (owner, sid, 1)
                    assert result_row["id"] == after["session"]["current_result_id"]
                    assert_call_outcomes(stable, owner, sid, [("refine", "succeeded", None)])
                    assert trace["gate_cancels"] == 0 and not trace["delegate_after_committed_cancel"]
                    assert trace["generate_delegates"] == trace["generate_returns"] == ["refine"]
                    assert trace["finish_delegates"] == trace["finish_returns"] == 1
                    expected_terminal = "result.ready"
                    late = await http.post(f"/api/v1/sessions/{sid}/cancel", headers=headers)
                    assert late.status_code == 409 and late.json()["error"]["code"] == "SESSION_TERMINAL"
                    assert await all_business(owner) == stable and await facts(app, owner, sid) == after
                else:
                    observed["cancel_started_at"] = loop.time()
                    cancel_task = asyncio.create_task(http.post(f"/api/v1/sessions/{sid}/cancel", headers=headers))
                    committed = await until_fact(app, owner, sid, lambda value: value["session"]["state"] == "cancelled")
                    assert_cancelled(committed, 4000)
                    observed["committed"] = True
                    committed_rows = await all_business(owner)
                    assert committed_rows["results"] == committed_rows["insertions"] == committed_rows["learning_events"] == []
                    async with asyncio.timeout(3):
                        await cancellation_seen.wait()
                    if mode == "late-delegate-after-cancel":
                        assert not cancel_task.done()  # It is genuinely joining the gated processing task.
                        assert trace["finish_returns"] == (0 if stage == "asr_finish" else 1)
                        assert trace["generate_returns"] == []
                    # Do not await cancel HTTP200 before releasing: its real
                    # implementation commits first and then joins processing.
                    released.set()
                    async with asyncio.timeout(35):  # Anti-hang, not a replacement 10/30-second budget.
                        cancelled = await asyncio.shield(cancel_task)
                    observed["cancel_completed_at"] = loop.time()
                    assert cancelled.status_code == 200 and cancelled.json()["state"] == "cancelled"
                    await wire.drain_task
                    after = await facts(app, owner, sid)
                    assert_cancelled(after, 4000)
                    assert after["session"] == committed["session"] and after["attempt"] == committed["attempt"]
                    stable = await all_business(owner)
                    assert without_model_outcomes(stable) == without_model_outcomes(committed_rows)
                    if stage == "asr_finish":
                        expected_calls = []
                        expected_delegates = int(mode == "late-delegate-after-cancel")
                        assert trace["finish_delegates"] == trace["finish_returns"] == expected_delegates
                        assert trace["generate_entries"] == trace["generate_delegates"] == trace["generate_returns"] == []
                    else:
                        assert trace["finish_delegates"] == trace["finish_returns"] == 1
                        assert [purpose for purpose, _ in trace["generate_entries"]] == ["refine"]
                        late_success = mode == "late-delegate-after-cancel"
                        expected_calls = [("refine", "succeeded" if late_success else "unknown",
                                           None if late_success else "CLIENT_INTERRUPTED")]
                        assert trace["generate_delegates"] == trace["generate_returns"] == (["refine"] if late_success else [])
                    assert_call_outcomes(stable, owner, sid, expected_calls)
                    assert trace["gate_cancels"] >= 1
                    assert trace["delegate_after_committed_cancel"] == (mode == "late-delegate-after-cancel")
                    # Exact all-12-table comparison starts only once original
                    # cancel and its legitimate ledger terminal writes finish.
                    for _ in range(2):
                        repeat = await http.post(f"/api/v1/sessions/{sid}/cancel", headers=headers)
                        assert repeat.status_code == 200 and repeat.json()["state"] == "cancelled"
                        assert await all_business(owner) == stable
                        assert await facts(app, owner, sid) == after
                    expected_terminal = "cancelled"
                fetched = await http.get(f"/api/v1/sessions/{sid}", headers=headers)
                assert fetched.status_code == 200 and fetched.json()["state"] == after["session"]["state"]
                assert fetched.json()["current_attempt_no"] == 1
                assert fetched.json()["current_result_id"] == after["session"]["current_result_id"]
                if mode == "normal-release":
                    assert fetched.json()["result"]["result_id"] == after["session"]["current_result_id"]
                else:
                    assert fetched.json()["result"] is None
                assert await all_business(owner) == stable
                assert wire.close_code == 1000
                assert [event["type"] for event in wire.events if event["type"] in
                        ("result.ready", "session.empty", "cancelled", "error")] == [expected_terminal]
                assert runtime.task.done() and runtime.audio == original_audio == b"" and runtime.asr is None
                assert stream.cancelled and runtime.retry_expires_at is None
                assert sid not in manager.runtimes and after["active"] == 0
                assert runtime.deadline == original_deadline
                assert llm.scheduler.snapshot() == {"running": 0, "high_waiting": 0, "low_waiting": 0}
                assert {call_id for _, call_id in trace["generate_entries"]} == {row["id"] for row in stable["model_calls"]}
                assert stable["dictionary_terms"] == stable["learning_events"] == stable["insertions"] == []
                assert all(row["version"] == 0 for row in stable["dictionary_state"])
                return {"stage": stage, "mode": mode, **trace,
                    "terminal": after["session"]["state"], "pcm_bytes": 0,
                    "physical_budget_remaining_at_gate_ms": round((observed["deadline"] - observed["entered_at"]) * 1000, 3),
                    "cancel_join_elapsed_ms": (round((observed["cancel_completed_at"] - observed["cancel_started_at"]) * 1000, 3)
                        if "cancel_completed_at" in observed else None),
                    "model_call_outcomes": [(row["purpose"], row["status"], row["error_code"]) for row in stable["model_calls"]],
                    "original_total_budget_seconds": 30, "original_call_limit_seconds": 10,
                    "scope": "controlled-Mock-provider-await-real-PG-API-WS-scheduler-not-native-or-real-model"}
            finally:
                released.set()
                if cancel_task is not None:
                    async with asyncio.timeout(35):
                        await asyncio.gather(cancel_task, return_exceptions=True)
                await settle(wire)

    record_property("voice19_provider_await", json.dumps(app_client.portal.call(scenario), sort_keys=True))
