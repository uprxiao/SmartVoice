"""VOICE15 server slice: actual dual-installation API/WS/PG transitions.

Only physical Mock provider entry is gated, never runtime/DB state. The native
hotkey and ready-result Desktop prompts are outside this test's evidence.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from sqlalchemy import func, select

from app.models import (
    CapacityChange, Device, DictionaryState, DictionaryTerm, Insertion,
    LearningEvent, ModelCall, ModelUsageDaily, OperationRequest, RuntimeCapacity,
    SessionAttempt, SessionContribution, SessionResult, UsageDaily, VoiceSession,
)
from app.models.session import ACTIVE_STATES
from app.providers.asr.mock import MockAsrSession
from app.providers.llm.mock import MockLlmProvider
from app.services.inference_context import current_scope
from tests.conftest import auth_headers, register_and_login
from tests.ids import fixture_uuid
from tests.integration.test_normal_shutdown_contract import WsProbe


BUSINESS = (VoiceSession, SessionAttempt, SessionResult, Insertion, LearningEvent,
            OperationRequest, SessionContribution, UsageDaily, ModelCall,
            ModelUsageDaily, DictionaryState, DictionaryTerm)


async def fingerprint(app, owner):
    async with app.state.db.session_factory() as db:
        # One observation must see one committed point. READ COMMITTED can
        # otherwise read the sessions table before the frame checkpoint and
        # the state query after it, falsely attributing progress to rejection.
        await db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        tables = {}
        for model in BUSINESS:
            rows = (await db.scalars(select(model).where(model.user_id == owner)
                .order_by(*model.__table__.primary_key.columns))).all()
            tables[model.__tablename__] = [
                tuple(getattr(row, column.name) for column in model.__table__.columns) for row in rows]
        capacity = await db.get(RuntimeCapacity, 1)
        active = int(await db.scalar(select(func.count()).select_from(VoiceSession)
            .where(VoiceSession.state.in_(ACTIVE_STATES))))
        changes = int(await db.scalar(select(func.count()).select_from(CapacityChange)))
        states = (await db.execute(select(VoiceSession.id, VoiceSession.state,
            VoiceSession.device_id, VoiceSession.current_attempt_no,
            VoiceSession.received_samples, VoiceSession.audio_complete,
            VoiceSession.current_result_id).where(VoiceSession.user_id == owner)
            .order_by(VoiceSession.id))).all()
        session_columns = [column.name for column in VoiceSession.__table__.columns]
        observed_rows = {row[session_columns.index("id")]: row for row in tables[VoiceSession.__tablename__]}
        for state in states:
            row = observed_rows[state[0]]
            assert row[session_columns.index("state")] == state[1]
            assert row[session_columns.index("received_samples")] == state[4]
    runtime = sorted((key, value.state, len(value.audio), value.attempt_no)
        for key, value in app.state.sessions.runtimes.items() if value.user_id == owner)
    return {"tables": tables, "states": [tuple(row) for row in states], "runtime": runtime,
        "active": active, "capacity": (capacity.id, capacity.version, capacity.max_active_sessions),
        "capacity_changes": changes}


async def device_ids(app, owner):
    async with app.state.db.session_factory() as db:
        rows = (await db.execute(select(Device.installation_id, Device.id).where(Device.user_id == owner))).all()
        return dict(rows)


def safe_error(response):
    return {"status": response.status_code, "code": response.json().get("error", {}).get("code")}


@pytest.mark.parametrize("phase", ["created", "streaming", "finalizing", "refining"])
def test_tc_voice_15_other_device_different_key_conflicts_only_while_active(
    app_client, monkeypatch, record_property, phase,
):
    email, password = "voice15-matrix@example.invalid", "Voice15Fixture123!"
    first = register_and_login(app_client, email=email, password=password)
    first_headers, owner = auth_headers(first), first["user"]["id"]
    installation2 = fixture_uuid("voice15-installation-two")
    second = app_client.post("/api/auth/login", json={"email": email, "password": password,
        "client_type": "desktop", "device": {"installation_id": installation2,
            "name": "Second fixture", "platform": "macos", "client_version": "0.0.1"}})
    assert second.status_code == 200 and second.json()["user"]["id"] == owner
    second_headers = auth_headers(second.json())
    ids = app_client.portal.call(device_ids, app_client.app, owner)
    assert set(ids) == {fixture_uuid("test-installation"), installation2}
    first_device, second_device = ids[fixture_uuid("test-installation")], ids[installation2]
    assert first_device != second_device

    async def scenario():
        app, manager = app_client.app, app_client.app.state.sessions
        assert app.state.config.asr.provider == app.state.config.llm.provider == "mock"
        assert app.state.config.asr.finish_timeout_ms == app.state.config.llm.call_timeout_ms == 10000
        assert app.state.config.pipeline.stop_to_result_budget_ms == 30000
        assert app.state.config.sessions.initial_max_active > 1
        entered, release = asyncio.Event(), asyncio.Event()
        target, observed = {"sid": None}, {"opens": [], "finishes": [], "llm": []}
        actual_open = manager.asr_provider.open
        actual_finish, actual_generate = MockAsrSession.finish, MockLlmProvider.generate
        wires = []
        original_key = fixture_uuid(f"voice15-{phase}-original")
        rejected_key = fixture_uuid(f"voice15-{phase}-other-device")

        async def opened(options):
            observed["opens"].append((options.session_id, options.attempt_no))
            return await actual_open(options)

        async def finished(stream, total_samples, deadline):
            observed["finishes"].append((stream.options.session_id, total_samples))
            if phase == "finalizing" and stream.options.session_id == target["sid"]:
                assert stream.samples == total_samples == 4000
                entered.set()
                await release.wait()  # The original production finish10 owns this wait.
            return await actual_finish(stream, total_samples, deadline)

        async def generated(provider, request, deadline):
            scope = current_scope.get()
            assert scope is not None
            observed["llm"].append((scope.session_id, scope.attempt_no, request.purpose))
            if phase == "refining" and scope.session_id == target["sid"] and request.purpose == "refine":
                assert request.call_id and scope.attempt_no == 1
                entered.set()
                await release.wait()  # The original Scheduled call/total budget owns it.
            return await actual_generate(provider, request, deadline)

        monkeypatch.setattr(manager.asr_provider, "open", opened)
        monkeypatch.setattr(MockAsrSession, "finish", finished)
        monkeypatch.setattr(MockLlmProvider, "generate", generated)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as http:
            async def create(headers, key):
                return await http.post("/api/v1/sessions", headers=headers | {"Idempotency-Key": key},
                    json={"client_request_id": key, "learning_enabled": True})

            async def stable_phase(sid, state, samples):
                # ACK means receipt, not persistence. Wait for the real first
                # frame checkpoint so the before/after snapshot is not racing
                # unrelated expected progress writes.
                async with asyncio.timeout(3):
                    while True:
                        snap = await fingerprint(app, owner)
                        row = next(row for row in snap["states"] if row[0] == sid)
                        if row[1] == state and row[4] == samples:
                            return snap
                        await asyncio.sleep(.01)

            async def attach(sid):
                wire = WsProbe(app, first_headers, sid)
                wires.append(wire)
                await wire.start()
                return wire

            wire, runtime = None, None
            try:
                created = await create(first_headers, original_key)
                assert created.status_code == 201 and created.json()["replayed"] is False
                sid = target["sid"] = created.json()["session_id"]
                runtime = manager.runtimes[sid]
                if phase != "created":
                    wire = await attach(sid)
                if phase in ("finalizing", "refining"):
                    await wire.end()
                    wire.drain_task = asyncio.create_task(wire.drain())
                    async with asyncio.timeout(3):
                        await entered.wait()
                before = await stable_phase(sid, phase, 0 if phase == "created" else 4000)
                assert before["active"] == 1 and before["capacity"][2] > before["active"]
                assert len(before["tables"]["sessions"]) == len(before["tables"]["session_attempts"]) == 1
                assert before["states"][0][2] == first_device and before["states"][0][3] == 1
                assert before["states"][0][5] is (phase in ("finalizing", "refining"))
                assert before["states"][0][6] is None
                assert before["tables"]["results"] == before["tables"]["insertions"] == before["tables"]["learning_events"] == []
                dispatch_before = {key: list(value) for key, value in observed.items()}

                # Same user+key is replay even from installation2, and must not
                # rewrite the original creator device or allocate another slot.
                replay = await create(second_headers, original_key)
                assert replay.status_code == 200 and replay.json()["replayed"] is True
                assert replay.json()["session_id"] == sid and replay.json()["current_attempt_no"] == 1
                assert replay.json()["state"] == phase
                for _ in range(2):
                    conflict = await create(second_headers, rejected_key)
                    assert conflict.status_code == 409, safe_error(conflict)
                    assert conflict.json()["error"]["code"] == "ACTIVE_SESSION_EXISTS"
                after_conflict = await fingerprint(app, owner)
                if after_conflict != before:
                    # Failure metadata only: no text, credentials or raw rows.
                    changes = []
                    for model in BUSINESS:
                        name = model.__tablename__
                        left, right = before["tables"][name], after_conflict["tables"][name]
                        fields = [column.name for column in model.__table__.columns]
                        if left != right:
                            changes.append({"table": name, "before_rows": len(left), "after_rows": len(right),
                                "changed_fields": sorted({fields[index] for old, new in zip(left, right)
                                    for index, (old_value, new_value) in enumerate(zip(old, new))
                                    if old_value != new_value})})
                    record_property("voice15_conflict_snapshot_difference", json.dumps(changes))
                assert after_conflict == before
                assert observed == dispatch_before

                # Positive completion is actual full audio.end and normal Mock
                # processing. No SQL/runtime state mutation frees the user slot.
                if wire is None:
                    wire = await attach(sid)
                if phase in ("created", "streaming"):
                    await wire.end()
                    wire.drain_task = asyncio.create_task(wire.drain())
                release.set()
                await wire.drain_task
                assert wire.close_code == 1000
                final = await http.get(f"/api/v1/sessions/{sid}", headers=first_headers)
                assert final.status_code == 200 and final.json()["state"] == "ready"
                assert final.json()["audio_complete"] is True
                assert final.json()["result"]["review_required"] is False
                assert final.json()["current_attempt_no"] == 1
                assert runtime.deadline == runtime.end_accepted_at + 30
                complete = await fingerprint(app, owner)
                assert complete["active"] == 0 and len(complete["tables"]["results"]) == 1
                assert complete["capacity"] == before["capacity"]

                # Reuse exactly the key rejected above: failures cannot poison
                # idempotency. New identity/device then replay are both checked.
                next_session = await create(second_headers, rejected_key)
                assert next_session.status_code == 201 and next_session.json()["replayed"] is False
                next_id = next_session.json()["session_id"]
                assert next_id != sid
                accepted = await fingerprint(app, owner)
                assert accepted["active"] == 1
                assert len(accepted["tables"]["sessions"]) == len(accepted["tables"]["session_attempts"]) == 2
                assert next(row for row in accepted["states"] if row[0] == next_id)[2] == second_device
                again = await create(second_headers, rejected_key)
                assert again.status_code == 200 and again.json()["replayed"] is True
                assert again.json()["session_id"] == next_id
                assert await fingerprint(app, owner) == accepted
                cancel = await http.post(f"/api/v1/sessions/{next_id}/cancel", headers=second_headers)
                assert cancel.status_code == 200 and cancel.json()["state"] == "cancelled"
                final_snapshot = await fingerprint(app, owner)
                assert final_snapshot["active"] == 0
                assert final_snapshot["capacity"] == before["capacity"]
                assert final_snapshot["capacity_changes"] == before["capacity_changes"] == 0
                assert len(final_snapshot["tables"]["results"]) == 1
                assert final_snapshot["tables"]["insertions"] == final_snapshot["tables"]["learning_events"] == []
                assert observed["opens"] == [(sid, 1), (next_id, 1)]
                assert observed["finishes"] == [(sid, 4000)]
                assert observed["llm"] == [(sid, 1, "refine")]
                return {"phase": phase, "installations": 2, "different_key_conflicts": 2,
                    "active_during_conflict": 1, "replayed_original_identity": True,
                    "business_rows_capacity_and_dispatch_unchanged_by_conflict": True,
                    "previously_rejected_key_accepted_after_normal_ready": True,
                    "new_session_owned_by_second_device": True, "final_active": 0,
                    "native_and_gui_executed": False}
            finally:
                release.set()
                # Failure-only cleanup. A cancelled/drained harness is never
                # used as the positive completion oracle above.
                for stream in wires:
                    if stream.task is not None and not stream.task.done():
                        await stream.send({"type": "websocket.disconnect", "code": 1000})
                    tasks = [task for task in (stream.drain_task, stream.task) if task is not None]
                    if tasks:
                        async with asyncio.timeout(35):
                            await asyncio.gather(*tasks, return_exceptions=True)

    evidence = app_client.portal.call(scenario)
    record_property("voice15_active_device_matrix", json.dumps(evidence, sort_keys=True))
