"""Original 3000ms partial low-queue deadline, true PG/API/ASGI integration.

Physical decode only is synthetic and honors its supplied absolute deadline.
No model/MLX child process/real microphone/throughput or 600s claim.
"""
from __future__ import annotations

import asyncio
import json
import struct

import httpx
import pytest
from sqlalchemy import select

from app.models import ModelUsageDaily
from tests.conftest import auth_headers, register_and_login
from tests.integration.test_asr_finish_queue_deadline import (
    RAW, finish_client, ledger, until,
)
from tests.integration.test_normal_shutdown_contract import WsProbe, create_session, safe_status


@pytest.fixture
def partial_client(finish_client, monkeypatch):
    client, control = finish_client
    engine = client.app.state.asr.engine
    control.partial_returned = asyncio.Event()
    control.partial_calls, control.finish_calls = [], []

    async def physical_decode(args, deadline):
        loop = asyncio.get_running_loop()
        assert len(engine.scheduler.running) == 1
        owner, job = next(iter(engine.scheduler.running.items()))
        stream = control.streams[owner[1]]
        assert owner == (stream.scope.user_id, stream.scope.session_id, stream.scope.attempt_no)
        assert deadline <= job.deadline and deadline > loop.time()
        audio = args[0]
        control.worker.active_audio = audio
        record = {"user_id": owner[0], "session_id": owner[1], "attempt_no": owner[2],
            "priority": job.priority, "job_deadline": job.deadline,
            "physical_deadline": deadline, "entered_loop": loop.time(),
            "samples": len(audio)//2, "cancelled_loop": None, "returned_loop": None}
        try:
            if job.priority == "low":
                assert not control.partial_calls
                assert len(audio) >= 32000
                assert 0 < job.deadline - record["entered_loop"] <= 3
                control.partial_calls.append(record)
                async with asyncio.timeout_at(deadline):
                    try:
                        await asyncio.Future()
                    except asyncio.CancelledError:
                        record["cancelled_loop"] = loop.time()
                        raise
            assert job.priority == "high"
            control.finish_calls.append(record)
            return {"words": [(0.0, len(audio)/32000, RAW)],
                "segments": [len(audio)/32000], "seconds": 0.0}
        finally:
            control.worker.active_audio = None
            args, audio = None, b""
            record["returned_loop"] = loop.time()
            if job.priority == "low":
                control.partial_returned.set()

    monkeypatch.setattr(control.worker, "decode", physical_decode)
    return client, control


def test_partial_original_three_second_timeout_has_terminal_ledger_and_finish_still_works(
    partial_client, record_property,
):
    client, control = partial_client
    actor = register_and_login(client, email="partial-deadline@example.invalid")
    headers = auth_headers(actor)

    async def run():
        app, loop = client.app, asyncio.get_running_loop()
        assert app.state.config.asr.decode_timeout_ms == 3000
        assert app.state.config.asr.finish_timeout_ms == 10000
        assert app.state.config.pipeline.stop_to_result_budget_ms == 30000
        evidence, wire, sid = {"scope": "PG+ASGI+MlxSession+AsrEngine; synthetic physical decode"}, None, None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as http:
            try:
                response = await create_session(http, headers)
                assert response.status_code == 201, safe_status(response)
                sid = response.json()["session_id"]
                wire = WsProbe(app, headers, sid)
                await wire.start()  # Legal frame seq0 and its ACK already received.
                runtime, stream = app.state.sessions.runtimes[sid], control.streams[sid]
                original_audio, original_buffer = runtime.audio, stream.buffer
                last_seq, samples = 0, 4000
                # Keep a legitimate 250ms PCM/ACK cadence until the physical
                # partial confirms deadline cancellation. No fake heartbeat,
                # finished() call, clock movement or state transition is used.
                async with asyncio.timeout(7):
                    while not control.partial_returned.is_set():
                        try:
                            async with asyncio.timeout(.25):
                                await control.partial_returned.wait()
                            break
                        except TimeoutError:
                            pass
                        if control.partial_returned.is_set():
                            break
                        last_seq += 1
                        await wire.send({"type": "websocket.receive", "bytes": struct.pack("<II", last_seq, 4000)
                            + struct.pack("<h", 100)*4000})
                        async with asyncio.timeout(2):
                            ack = await wire.event()
                        samples += 4000
                        assert ack["type"] == "audio.ack" and ack["seq"] == last_seq
                        assert ack["total_samples"] == samples
                assert len(control.partial_calls) == 1 and not control.finish_calls
                trace = control.partial_calls[0]
                assert trace["cancelled_loop"] is not None and trace["cancelled_loop"] >= trace["physical_deadline"]
                await until(lambda: stream.task is not None and stream.task.done())
                # Query the actual dispatch's durable state before finish can
                # create another call; preserve the raw observation on failure.
                rows = await ledger(app, [sid])
                readback_at = loop.time()
                async with app.state.db.session_factory() as db:
                    daily_rows = (await db.scalars(select(ModelUsageDaily).where(
                        ModelUsageDaily.user_id == actor["user"]["id"],
                        ModelUsageDaily.purpose == "asr_decode"))).all()
                    daily = [{key: getattr(row, key) for key in ("calls", "successes", "failures", "unknown_calls",
                        "processed_audio_samples", "prompt_tokens_known", "completion_tokens_known",
                        "prompt_tokens_unknown_calls", "completion_tokens_unknown_calls")} for row in daily_rows]
                evidence.update(partial=trace, model_calls=rows, model_daily=daily,
                    readback_loop=readback_at, original_deadline_remaining_ms=(trace["job_deadline"]-readback_at)*1000,
                    received_samples=samples)
                assert runtime.state == "streaming" and not runtime.ended
                assert control.worker.active_audio is None
                assert app.state.asr.scheduler.snapshot() == {"running": 0, "high_waiting": 0, "low_waiting": 0}

                # A failed partial is nonfatal: the authoritative normal end
                # must still use this session and all acknowledged PCM.
                await wire.send({"type": "websocket.receive", "text": json.dumps({
                    "type": "audio.end", "last_seq": last_seq, "total_samples": samples})})
                ack = await wire.event()
                assert ack["type"] == "audio.end_ack" and ack["last_seq"] == last_seq and ack["total_samples"] == samples
                await wire.drain()
                response = await http.get(f"/api/v1/sessions/{sid}", headers=headers)
                assert response.status_code == 200, safe_status(response)
                value = response.json()
                assert value["state"] == "ready" and value["current_attempt_no"] == 1
                assert value["audio_complete"] and value["received_samples"] == samples
                assert value["result"]["raw_text"] == value["result"]["refined_text"] == RAW
                assert value["result"]["review_required"] is False
                assert wire.close_code == 1000 and len(control.finish_calls) == 1
                assert control.finish_calls[0]["samples"] == samples
                assert original_audio == original_buffer == b"" and control.worker.active_audio is None
                assert not any(event["type"] in ("asr.partial", "error") for event in wire.events)
                evidence["normal_finish_after_failed_partial"] = True

                assert len(rows) == 1
                row = rows[0]
                assert row["user_id"] == actor["user"]["id"] and row["session_id"] == sid
                assert row["attempt_no"] == row["call_no"] == 1 and row["purpose"] == "asr_decode"
                assert row["processed_audio_samples"] == trace["samples"]
                assert row["status"] in ("failed", "cancelled", "unknown")
                assert row["error_code"] in ("ASR_TIMEOUT", "CANCELLED", "CLIENT_INTERRUPTED")
                assert row["ended_at"] is not None and row["aggregated_at"] is not None
                assert row["prompt_tokens"] is row["completion_tokens"] is None
                assert readback_at <= trace["job_deadline"]
                assert len(daily) == 1
                d = daily[0]
                assert (d["calls"], d["successes"], d["failures"], d["unknown_calls"]) == (
                    1, 0, 1, int(row["status"] == "unknown"))
                assert d["processed_audio_samples"] == trace["samples"]
                assert d["prompt_tokens_known"] == d["completion_tokens_known"] == 0
                assert d["prompt_tokens_unknown_calls"] == d["completion_tokens_unknown_calls"] == 1
            finally:
                try:
                    record_property("asr_partial_deadline_ledger", json.dumps(evidence, default=str))
                finally:
                    try:
                        if sid is not None:
                            runtime = app.state.sessions.runtimes.get(sid)
                            if runtime is not None and runtime.state in ("created", "streaming", "finalizing", "refining"):
                                async with asyncio.timeout(5):
                                    await http.post(f"/api/v1/sessions/{sid}/cancel", headers=headers)
                    finally:
                        if wire is not None and wire.task is not None:
                            if not wire.task.done():
                                wire.task.cancel()
                            async with asyncio.timeout(5):
                                await asyncio.gather(wire.task, return_exceptions=True)
    client.portal.call(run)
