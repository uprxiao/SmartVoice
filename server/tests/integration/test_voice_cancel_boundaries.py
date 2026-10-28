"""VOICE07/19: controlled real ASGI/PG cancellation boundaries, Mock models.

No DB/runtime state is assigned. Test-owned receive/store gates explicitly model
late delivery; their cancellation behavior is observed, never called natural
frequency or real transport/model behavior. Product deadlines remain unchanged.
"""
from __future__ import annotations

import asyncio
import json
import struct

import httpx
import pytest
from sqlalchemy import func, select

from app.models import (
    Insertion, LearningEvent, ModelCall, ModelUsageDaily, SessionAttempt,
    SessionContribution, SessionResult, UsageDaily, VoiceSession,
)
from app.models.session import ACTIVE_STATES
from app.services.session_runtime import ProtocolViolation
from tests.conftest import auth_headers, register_and_login
from tests.ids import fixture_uuid
from tests.integration.test_normal_shutdown_contract import WsProbe


async def facts(app, owner, sid):
    async with app.state.db.session_factory() as db:
        session = await db.scalar(select(VoiceSession).where(
            VoiceSession.user_id == owner, VoiceSession.id == sid))
        attempt = await db.scalar(select(SessionAttempt).where(
            SessionAttempt.user_id == owner, SessionAttempt.session_id == sid,
            SessionAttempt.attempt_no == 1))
        assert session is not None and attempt is not None
        counts = {model.__tablename__: int(await db.scalar(select(func.count()).select_from(model)
            .where(model.user_id == owner, model.session_id == sid)))
            for model in (SessionResult, Insertion, LearningEvent, ModelCall)}
        permanent = {}
        for model in (SessionContribution, UsageDaily, ModelCall, ModelUsageDaily):
            rows = (await db.scalars(select(model).where(model.user_id == owner)
                .order_by(*model.__table__.primary_key.columns))).all()
            permanent[model.__tablename__] = [tuple(getattr(row, column.name)
                for column in model.__table__.columns) for row in rows]
        return {"session": {"state": session.state, "error_code": session.error_code,
                "attempt_no": session.current_attempt_no, "current_result_id": session.current_result_id,
                "received_samples": session.received_samples, "audio_complete": session.audio_complete,
                "retry_cache_status": session.retry_cache_status,
                "first_terminal_at": session.first_terminal_at},
            "attempt": {"state": attempt.state, "error_code": attempt.error_code,
                "has_raw": attempt.raw_text is not None, "ended_at": attempt.ended_at},
            "counts": counts, "permanent": permanent,
            "active": int(await db.scalar(select(func.count()).select_from(VoiceSession)
                .where(VoiceSession.state.in_(ACTIVE_STATES))))}


async def until_fact(app, owner, sid, predicate):
    # Observation guard only. The read is an independent real session, hence
    # state=cancelled here proves the API's transaction committed, not just flush.
    async with asyncio.timeout(3):
        while True:
            current = await facts(app, owner, sid)
            if predicate(current):
                return current
            await asyncio.sleep(.01)


async def create(http, headers, label):
    key = fixture_uuid(label)
    response = await http.post("/api/v1/sessions", headers=headers | {"Idempotency-Key": key},
        json={"client_request_id": key, "learning_enabled": True})
    assert response.status_code == 201
    return response.json()["session_id"]


async def settle(wire):
    # Failure-only cleanup: never used as proof of normal completion below.
    if wire is None:
        return
    if wire.task is not None and not wire.task.done():
        await wire.send({"type": "websocket.disconnect", "code": 1000})
    tasks = [task for task in (wire.drain_task, wire.task) if task is not None]
    if tasks:
        async with asyncio.timeout(35):
            await asyncio.gather(*tasks, return_exceptions=True)


def assert_cancelled(value, received_samples):
    assert value["session"]["state"] == value["attempt"]["state"] == "cancelled"
    assert value["session"]["current_result_id"] is None
    assert value["session"]["received_samples"] == received_samples
    assert value["session"]["retry_cache_status"] == "none"
    assert value["session"]["error_code"] is value["attempt"]["error_code"] is None
    assert value["attempt"]["ended_at"] is not None and not value["attempt"]["has_raw"]
    assert value["counts"]["results"] == value["counts"]["insertions"] == value["counts"]["learning_events"] == 0
    assert value["active"] == 0


def test_tc_voice_07_late_packet_acquired_by_asgi_is_rejected_after_cancel_commit(
    app_client, monkeypatch, record_property,
):
    pair = register_and_login(app_client, email="voice07-late@example.invalid")
    headers, owner = auth_headers(pair), pair["user"]["id"]

    async def scenario():
        app, manager = app_client.app, app_client.app.state.sessions
        acquired, released, receiver_cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        trace = {"received_seq1": 0, "delivered_seq1": 0, "receive_cancels": 0,
                 "frame_entries": [], "frame_rejections": [], "feeds": [], "finishes": 0}
        wire = None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as http:
            sid = await create(http, headers, "voice07-late")
            runtime, actual_accept = manager.runtimes[sid], manager._accept_frame
            original_audio, stream = runtime.audio, runtime.asr
            actual_feed, actual_finish = stream.feed, stream.finish

            async def accept(current, packet):
                seq = struct.unpack_from("<I", packet)[0]
                if current is runtime:
                    trace["frame_entries"].append((seq, current.cancelled, current.samples, len(current.audio)))
                try:
                    return await actual_accept(current, packet)
                except ProtocolViolation as error:
                    if current is runtime:
                        trace["frame_rejections"].append((seq, error.code))
                    raise

            async def feed(pcm, start_sample):
                trace["feeds"].append((start_sample, len(pcm)))
                return await actual_feed(pcm, start_sample)

            async def finish(total_samples, deadline):
                trace["finishes"] += 1
                return await actual_finish(total_samples, deadline)

            monkeypatch.setattr(manager, "_accept_frame", accept)
            monkeypatch.setattr(stream, "feed", feed)
            monkeypatch.setattr(stream, "finish", finish)

            async def controlled_transport(scope, receive, send):
                assert scope["path"] == f"/ws/v1/sessions/{sid}/audio"

                async def received():
                    packet = await receive()  # Real queued ASGI message is already obtained here.
                    pcm = packet.get("bytes")
                    if pcm is not None and len(pcm) >= 8 and struct.unpack_from("<I", pcm)[0] == 1:
                        assert trace["received_seq1"] == 0
                        trace["received_seq1"] += 1
                        acquired.set()
                        # Deliberately late transport delivery, not a fake frame
                        # or a production cancellation handler. The gate owns
                        # the original packet, and returns it exactly once.
                        while not released.is_set():
                            try:
                                await released.wait()
                            except asyncio.CancelledError:
                                trace["receive_cancels"] += 1
                                receiver_cancelled.set()
                        trace["delivered_seq1"] += 1
                    return packet

                await app(scope, received, send)

            try:
                wire = WsProbe(controlled_transport, headers, sid)
                await wire.start()  # positive seq0: accepted, ACK4000, real provider.feed
                await until_fact(app, owner, sid, lambda value:
                    value["session"]["state"] == "streaming" and value["session"]["received_samples"] == 4000)
                assert runtime.samples == stream.samples == 4000 and len(original_audio) == 8000
                wire.drain_task = asyncio.create_task(wire.drain())
                await wire.send({"type": "websocket.receive",
                    "bytes": struct.pack("<II", 1, 4000) + struct.pack("<h", 101) * 4000})
                async with asyncio.timeout(3):
                    await acquired.wait()
                assert trace["received_seq1"] == 1 and trace["delivered_seq1"] == 0
                cancelled = await http.post(f"/api/v1/sessions/{sid}/cancel", headers=headers)
                assert cancelled.status_code == 200 and cancelled.json()["state"] == "cancelled"
                committed = await facts(app, owner, sid)
                assert_cancelled(committed, 4000)
                assert runtime.cancelled and runtime.audio == original_audio == b"" and runtime.asr is None
                async with asyncio.timeout(3):
                    await receiver_cancelled.wait()
                # The formal sender has ended and cancelled its receiver. Now
                # force actual original packet delivery to the real frame guard.
                released.set()
                await wire.drain_task
                assert wire.close_code == 1000
                assert trace["delivered_seq1"] == 1
                assert trace["frame_entries"] == [(0, False, 0, 0), (1, True, 4000, 0)]
                assert trace["frame_rejections"] == [(1, "AUDIO_PROTOCOL_ERROR")]
                assert trace["feeds"] == [(0, 8000)] and trace["finishes"] == 0
                assert runtime.samples == stream.samples == 4000
                assert all(not (event["type"] == "audio.ack" and event["seq"] == 1) for event in wire.events)
                assert [event["type"] for event in wire.events if event["type"] in
                    ("result.ready", "session.empty", "cancelled", "error")] == ["cancelled"]
                assert await facts(app, owner, sid) == committed
                repeat = await http.post(f"/api/v1/sessions/{sid}/cancel", headers=headers)
                assert repeat.status_code == 200 and repeat.json()["state"] == "cancelled"
                assert await facts(app, owner, sid) == committed
                assert runtime.audio == original_audio == b"" and runtime.asr is None
                assert sid not in manager.runtimes
                assert committed["counts"]["model_calls"] == 0
                return {"received_seq1": 1, "delivered_seq1_after_committed_cancel": 1,
                    "actual_frame_guard_code": "AUDIO_PROTOCOL_ERROR", "late_ack_count": 0,
                    "provider_feed_calls": 1, "provider_finish_calls": 0,
                    "received_samples_retained": 4000, "receive_cancellation_observed": trace["receive_cancels"],
                    "cancel_replay_no_business_delta": True, "pcm_bytes_after_cancel": 0,
                    "scope": "controlled-late-ASGI-delivery-not-natural-network-frequency"}
            finally:
                released.set()
                await settle(wire)

    record_property("voice07_late_packet", json.dumps(app_client.portal.call(scenario), sort_keys=True))


@pytest.mark.parametrize("mode", ["normal-release", "propagate-cancel", "late-store-after-cancel"])
def test_tc_voice_19_cancel_wins_before_ready_user_lock(
    app_client, monkeypatch, record_property, mode,
):
    pair = register_and_login(app_client, email="voice19-ready@example.invalid")
    headers, owner = auth_headers(pair), pair["user"]["id"]

    async def scenario():
        app, manager = app_client.app, app_client.app.state.sessions
        entered, released, cancellation_seen = asyncio.Event(), asyncio.Event(), asyncio.Event()
        trace = {"store_entries": 0, "original_store_calls": 0, "store_cancels": 0,
                 "locked_states": [], "store_returns": []}
        wire, cancel_task = None, None
        actual_store, actual_locked = manager._store_result, manager._locked_session
        target = {"sid": None, "store_executing": False}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as http:
            sid = target["sid"] = await create(http, headers, f"voice19-ready-{mode}")
            runtime, original_audio = manager.runtimes[sid], manager.runtimes[sid].audio
            assert app.state.config.asr.finish_timeout_ms == app.state.config.llm.call_timeout_ms == 10000
            assert app.state.config.pipeline.stop_to_result_budget_ms == 30000

            async def locked(db, user_id, session_id):
                answer = await actual_locked(db, user_id, session_id)
                if session_id == sid and target["store_executing"] and asyncio.current_task() is runtime.task:
                    trace["locked_states"].append(answer[1].state if answer[1] else None)
                return answer

            async def store(session_id, user_id, attempt_no, *args, **kwargs):
                if session_id != sid:
                    return await actual_store(session_id, user_id, attempt_no, *args, **kwargs)
                assert user_id == owner and attempt_no == 1 and asyncio.current_task() is runtime.task
                trace["store_entries"] += 1
                assert trace["store_entries"] == 1
                # Nothing in the original store has executed: no DB session,
                # User/Session lock, result row, or commit exists at this gate.
                entered.set()
                while not released.is_set():
                    try:
                        await released.wait()
                    except asyncio.CancelledError:
                        trace["store_cancels"] += 1
                        cancellation_seen.set()
                        if mode != "late-store-after-cancel":
                            raise
                assert asyncio.get_running_loop().time() < runtime.deadline
                trace["original_store_calls"] += 1
                target["store_executing"] = True
                try:
                    answer = await actual_store(session_id, user_id, attempt_no, *args, **kwargs)
                    trace["store_returns"].append("none" if answer is None else "result")
                    return answer
                finally:
                    target["store_executing"] = False

            monkeypatch.setattr(manager, "_locked_session", locked)
            monkeypatch.setattr(manager, "_store_result", store)
            try:
                wire = WsProbe(app, headers, sid)
                await wire.start()
                await wire.end()
                wire.drain_task = asyncio.create_task(wire.drain())
                async with asyncio.timeout(3):
                    await entered.wait()
                before = await facts(app, owner, sid)
                assert before["session"]["state"] == "refining"
                assert before["session"]["audio_complete"] and before["attempt"]["has_raw"]
                assert before["counts"]["results"] == before["counts"]["insertions"] == before["counts"]["learning_events"] == 0
                assert runtime.audio == original_audio == b"" and runtime.asr is None
                assert before["counts"]["model_calls"] == 1  # normal refine already returned
                assert trace["original_store_calls"] == 0 and trace["locked_states"] == []
                if mode == "normal-release":
                    released.set()
                    await wire.drain_task
                    after = await facts(app, owner, sid)
                    assert after["session"]["state"] == after["attempt"]["state"] == "ready"
                    assert after["session"]["current_result_id"] is not None and after["counts"]["results"] == 1
                    assert after["counts"]["insertions"] == after["counts"]["learning_events"] == 0
                    assert trace["locked_states"] == ["refining"] and trace["store_returns"] == ["result"]
                    assert trace["original_store_calls"] == 1 and trace["store_cancels"] == 0
                    too_late = await http.post(f"/api/v1/sessions/{sid}/cancel", headers=headers)
                    assert too_late.status_code == 409 and too_late.json()["error"]["code"] == "SESSION_TERMINAL"
                    assert await facts(app, owner, sid) == after
                    assert [event["type"] for event in wire.events if event["type"] in
                        ("result.ready", "cancelled", "error")] == ["result.ready"]
                else:
                    cancel_task = asyncio.create_task(http.post(f"/api/v1/sessions/{sid}/cancel", headers=headers))
                    committed = await until_fact(app, owner, sid,
                        lambda value: value["session"]["state"] == "cancelled")
                    assert_cancelled(committed, 4000)
                    async with asyncio.timeout(3):
                        await cancellation_seen.wait()
                    # cancel() waits for runtime.task after committing. Waiting
                    # for HTTP200 before releasing the deliberately late store
                    # would deadlock the harness, not reproduce a product race.
                    released.set()
                    async with asyncio.timeout(3):
                        cancelled = await asyncio.shield(cancel_task)
                    assert cancelled.status_code == 200 and cancelled.json()["state"] == "cancelled"
                    await wire.drain_task
                    after = await facts(app, owner, sid)
                    assert_cancelled(after, 4000)
                    assert after == committed
                    if mode == "late-store-after-cancel":
                        assert trace["original_store_calls"] == 1
                        assert trace["locked_states"] == ["cancelled"] and trace["store_returns"] == ["none"]
                    else:
                        assert trace["original_store_calls"] == 0 and trace["locked_states"] == trace["store_returns"] == []
                    repeat = await http.post(f"/api/v1/sessions/{sid}/cancel", headers=headers)
                    assert repeat.status_code == 200 and repeat.json()["state"] == "cancelled"
                    assert await facts(app, owner, sid) == after
                    assert [event["type"] for event in wire.events if event["type"] in
                        ("result.ready", "cancelled", "error")] == ["cancelled"]
                assert wire.close_code == 1000
                assert runtime.task.done() and runtime.audio == original_audio == b"" and runtime.asr is None
                assert sid not in manager.runtimes and after["active"] == 0
                assert runtime.deadline == runtime.end_accepted_at + 30
                assert after["counts"]["model_calls"] == before["counts"]["model_calls"] == 1
                return {"mode": mode, **trace, "terminal": after["session"]["state"],
                    "results": after["counts"]["results"], "insertions": 0, "learning_events": 0,
                    "pcm_bytes": 0, "original_total_budget_seconds": 30,
                    "scope": "ready-store-before-User-lock-not-LLM-await-gate"}
            finally:
                released.set()
                if cancel_task is not None:
                    async with asyncio.timeout(35):
                        await asyncio.gather(cancel_task, return_exceptions=True)
                await settle(wire)

    record_property("voice19_pre_ready_cancel", json.dumps(app_client.portal.call(scenario), sort_keys=True))
