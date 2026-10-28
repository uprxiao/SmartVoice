"""CAP14/NFR09: original finish10 includes a real queue wait and bookkeeping.

Real PG, authenticated HTTP/ASGI WS, MlxAsrSession, AsrEngine and scheduler.
Only physical decoder preparation/execution are synthetic. No model quality,
MLX subprocess termination, microphone, TCP WebSocket or throughput claim.
No clock/config/deadline/state/recorder monkeypatch or manual finished().
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from app.models import ModelCall, ModelUsageDaily
from app.providers.asr.mlx_whisper import MlxAsrSession, MlxWhisperProvider
from app.services.asr_core import AsrEngine
from app.services.inference_context import current_scope
from tests.conftest import auth_headers, register_and_login
from tests.integration.test_normal_shutdown_contract import WsProbe, create_session, safe_status


RAW = "受控收尾排队后的正向结果。"
MODEL = "fixture-finish-queue-no-physical-model"


async def until(predicate, seconds=3):
    async with asyncio.timeout(seconds):  # Coordination guard, never a product budget.
        while not predicate():
            await asyncio.sleep(.005)


class PhysicalWorker:
    def __init__(self, engine, control):
        self.engine, self.control = engine, control
        self.process = SimpleNamespace(is_alive=lambda: True)
        self.active_audio = None

    async def decode(self, args, deadline):
        loop = asyncio.get_running_loop()
        scope = current_scope.get()
        assert scope is not None and scope.attempt_no == 1
        owner = (scope.user_id, scope.session_id, scope.attempt_no)
        assert list(self.engine.scheduler.running) == [owner]
        job = self.engine.scheduler.running[owner]
        assert job.priority == "high", "4000 PCM samples must not launch a partial decode"
        assert 0 < deadline - loop.time() <= 10
        assert deadline <= job.deadline
        audio = args[0]
        assert len(audio) == 8000
        record = {"user_id": scope.user_id, "session_id": scope.session_id,
            "attempt_no": scope.attempt_no, "started_loop": loop.time(),
            "physical_deadline": deadline, "job_deadline": job.deadline,
            "pcm_bytes": len(audio), "pcm_sha256": hashlib.sha256(audio).hexdigest(),
            "cancelled_at": None, "returned_at": None}
        self.control.calls.append(record)
        self.control.jobs[scope.session_id] = job
        self.active_audio = audio
        try:
            # Match real DecodeWorker.decode: the physical boundary enforces
            # the supplied absolute deadline and confirms its own release.
            # This does not replace the scheduler/finish10 clock with a timer.
            async with asyncio.timeout_at(deadline):
                try:
                    mode = self.control.modes[scope.session_id]
                    if mode != "success":
                        await self.control.gates[scope.session_id].wait()
                        raise RuntimeError("synthetic physical ASR failure")
                    return {"words": [(0.0, .25, RAW)], "segments": [.25], "seconds": 0.0}
                except asyncio.CancelledError:
                    record["cancelled_at"] = loop.time()
                    raise
        finally:
            self.active_audio = None
            args, audio = None, b""
            record["returned_at"] = loop.time()

    async def stop(self):
        assert self.active_audio is None
        self.process = None


@pytest.fixture
def finish_client(monkeypatch, request):
    monkeypatch.setenv("SVI_ASR_PROVIDER", "mlx_whisper")
    monkeypatch.setenv("SVI_LLM_PROVIDER", "mock")
    control = SimpleNamespace(streams={}, modes={}, gates={}, jobs={}, calls=[])
    original_open = MlxWhisperProvider.open

    async def physical_prepare(engine):
        engine.model_id = MODEL
        control.worker = PhysicalWorker(engine, control)
        engine.workers = [control.worker]

    async def observed_open(provider, options):
        stream = await original_open(provider, options)
        assert isinstance(stream, MlxAsrSession)
        control.streams[options.session_id] = stream
        return stream

    monkeypatch.setattr(AsrEngine, "prepare", physical_prepare)
    monkeypatch.setattr(MlxWhisperProvider, "open", observed_open)
    client = request.getfixturevalue("app_client")
    config = client.app.state.config
    assert config.asr.execution_slots == 1
    assert config.asr.finish_timeout_ms == 10000 and config.asr.decode_timeout_ms == 3000
    assert config.pipeline.stop_to_result_budget_ms == 30000
    assert config.retry.audio_ttl_seconds == 600
    assert isinstance(client.app.state.asr, MlxWhisperProvider)
    assert client.app.state.asr.recorder is client.app.state.recorder
    assert type(client.app.state.recorder).__name__ == "DatabaseCallRecorder"
    return client, control


async def ledger(app, sids):
    names = ("id", "user_id", "session_id", "attempt_no", "learning_event_id", "purpose",
        "provider", "model_id", "call_no", "status", "error_code", "elapsed_ms",
        "processed_audio_samples", "prompt_tokens", "completion_tokens", "ended_at", "aggregated_at")
    async with app.state.db.session_factory() as db:
        rows = (await db.scalars(select(ModelCall).where(ModelCall.session_id.in_(sids))
            .order_by(ModelCall.started_at, ModelCall.id))).all()
        return [{name: getattr(row, name) for name in names} for row in rows]


async def day_rows(app, owners):
    names = ("user_id", "purpose", "calls", "successes", "failures", "unknown_calls",
        "processed_audio_samples", "prompt_tokens_known", "completion_tokens_known",
        "prompt_tokens_unknown_calls", "completion_tokens_unknown_calls")
    async with app.state.db.session_factory() as db:
        rows = (await db.scalars(select(ModelUsageDaily).where(
            ModelUsageDaily.user_id.in_(owners), ModelUsageDaily.purpose == "asr_finish"))).all()
        return [{name: getattr(row, name) for name in names} for row in rows]


def test_finish_wait_and_execution_share_original_ten_seconds(finish_client, record_property):
    client, control = finish_client
    actors = [register_and_login(client, email=f"finish-queue-{n}@example.invalid") for n in range(2)]
    headers = [auth_headers(actor) for actor in actors]

    async def run():
        app, loop = client.app, asyncio.get_running_loop()
        manager, scheduler = app.state.sessions, app.state.asr.scheduler
        wires, tasks, timers = [], [], []
        evidence = {"scope": "PG+ASGI+production-ASR-scheduler; synthetic physical decoder", "checks": {}}

        async def begin(http, owner_headers, mode):
            response = await create_session(http, owner_headers)
            assert response.status_code == 201, safe_status(response)
            sid = response.json()["session_id"]
            control.modes[sid], control.gates[sid] = mode, asyncio.Event()
            wire = WsProbe(app, owner_headers, sid)
            wires.append(wire)
            await wire.start()
            runtime = manager.runtimes[sid]
            audio = runtime.audio
            provider_buffer = control.streams[sid].buffer
            assert len(audio) == len(provider_buffer) == 8000
            await wire.end()
            assert runtime.end_accepted_at is not None
            assert runtime.deadline == runtime.end_accepted_at + 30
            wire.drain_task = asyncio.create_task(wire.drain())
            tasks.append(wire.drain_task)
            return SimpleNamespace(sid=sid, wire=wire, runtime=runtime, audio=audio,
                buffer=provider_buffer, end=runtime.end_accepted_at,
                total=runtime.deadline, finish=runtime.end_accepted_at + 10)

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as http:
            try:
                first = await begin(http, headers[0], "fail-on-release")
                await until(lambda: first.sid in control.jobs)
                second = await begin(http, headers[1], "deadline-cancel")
                second_owner = (actors[1]["user"]["id"], second.sid, 1)
                await until(lambda: second_owner in scheduler.queues["high"])
                queued = scheduler.queues["high"][second_owner]
                assert queued.deadline == queued.wait_deadline == second.finish
                assert not queued.started and second.sid not in control.jobs
                assert await ledger(app, [second.sid]) == [], "queued work created a call before dispatch"
                assert len(control.calls) == 1
                evidence.update(original_end_loop=second.end,
                    original_finish_deadline=second.finish, original_total_deadline=second.total)
                first_row = await ledger(app, [first.sid])
                assert len(first_row) == 1 and first_row[0]["status"] == "running"
                queued_at = loop.time()
                release_at = second.end + 6
                assert queued_at < release_at < first.finish
                await asyncio.sleep(release_at - loop.time())  # Real clock: no 10s budget reduction.
                assert scheduler.queues["high"].get(second_owner) is queued
                assert second.sid not in control.jobs
                control.gates[first.sid].set()
                await until(lambda: second.sid in control.jobs)
                second_trace = next(row for row in control.calls if row["session_id"] == second.sid)
                physical_deadline = second_trace["physical_deadline"]
                assert control.jobs[second.sid] is queued
                assert queued.deadline == second.finish
                assert second_trace["started_loop"] >= release_at
                assert 0 < physical_deadline - second_trace["started_loop"] <= 4
                assert physical_deadline <= second.finish < second_trace["started_loop"] + 10
                assert loop.time() < physical_deadline - .05, "harness missed the pre-deadline observation"
                before, due = loop.create_future(), loop.create_future()

                def before_deadline():
                    before.set_result({"observed_loop": loop.time(),
                        "job_running": scheduler.running.get(second_owner) is queued,
                        "task_pending": queued.task is not None and not queued.task.done(),
                        "physical_audio_present": control.worker.active_audio is not None})

                def at_deadline():
                    due.set_result({"observed_loop": loop.time(),
                        "timer_lateness_ms": (loop.time() - physical_deadline) * 1000})

                timers.extend([loop.call_at(physical_deadline - .05, before_deadline),
                    loop.call_at(physical_deadline, at_deadline)])
                async with asyncio.timeout(12):  # Bounded observation only; actual deadline stays original.
                    await asyncio.gather(first.wire.drain_task, second.wire.drain_task)
                    before_value, due_value = await asyncio.gather(before, due)
                assert all(before_value[key] for key in ("job_running", "task_pending", "physical_audio_present"))
                assert second_trace["cancelled_at"] is not None
                assert second_trace["cancelled_at"] >= physical_deadline
                assert not control.gates[second.sid].is_set(), "fixture, not production, ended the timed-out decoder"
                evidence.update(physical_calls=list(control.calls),
                    before_deadline=before_value, at_deadline=due_value)

                outcomes = []
                evidence["session_outcomes"] = outcomes
                for item, own_headers, code in [(first, headers[0], "ASR_FAILED"), (second, headers[1], "ASR_TIMEOUT")]:
                    response = await http.get(f"/api/v1/sessions/{item.sid}", headers=own_headers)
                    assert response.status_code == 200, safe_status(response)
                    body = response.json()
                    outcomes.append({key: body[key] for key in (
                        "state", "error_code", "audio_complete", "current_attempt_no", "received_samples", "retry_kind")})
                    assert body["state"] == "failed" and body["error_code"] == code
                    assert body["audio_complete"] and body["received_samples"] == 4000
                    assert body["current_attempt_no"] == 1 and body["retry_kind"] == "asr" and body["result"] is None
                    assert item.wire.close_code == 1011
                    assert [event["type"] for event in item.wire.events] == ["ready", "audio.ack", "audio.end_ack", "error"]
                    assert item.wire.events[-1]["error"]["code"] == code
                    assert item.runtime.end_accepted_at == item.end and item.runtime.deadline == item.total
                    assert item.runtime.asr is None and item.runtime.task.done()
                    assert item.buffer == b"" and control.streams[item.sid].cancelled
                    assert item.runtime.audio is item.audio and len(item.audio) == 8000  # Complete failure cache, not work copy.
                foreign = await http.get(f"/api/v1/sessions/{second.sid}", headers=headers[0])
                assert foreign.status_code == 404
                assert control.worker.active_audio is None
                assert scheduler.snapshot() == {"running": 0, "high_waiting": 0, "low_waiting": 0}
                assert queued.done.is_set() and queued.operation is None and queued.task is None
                assert len(control.calls) == 2 and all(row["attempt_no"] == 1 for row in control.calls)

                rows = await ledger(app, [first.sid, second.sid])
                daily = await day_rows(app, [actor["user"]["id"] for actor in actors])
                readback_at = loop.time()
                evidence.update(queue_entered_loop=queued_at, first_released_loop=release_at,
                    original_end_loop=second.end, original_finish_deadline=second.finish,
                    original_total_deadline=second.total, physical_calls=list(control.calls),
                    before_deadline=before_value, at_deadline=due_value,
                    terminal_readback_loop=readback_at, finish_remaining_ms=(second.finish-readback_at)*1000,
                    session_outcomes=outcomes, model_calls=rows, model_daily=daily)
                evidence["checks"]["queue_timeout_and_owned_copy_release"] = True

                # Positive control: normal create/audio.end on the released slot
                # succeeds for the OTHER account without changing B's failed result.
                positive = await begin(http, headers[0], "success")
                async with asyncio.timeout(5):
                    await positive.wire.drain_task
                value = (await http.get(f"/api/v1/sessions/{positive.sid}", headers=headers[0])).json()
                assert value["state"] == "ready" and value["result"]["raw_text"] == RAW
                assert value["result"]["refined_text"] == RAW and not value["result"]["review_required"]
                assert positive.wire.close_code == 1000
                assert positive.audio == positive.buffer == b""
                assert len(control.calls) == 3 and control.worker.active_audio is None
                assert scheduler.snapshot() == {"running": 0, "high_waiting": 0, "low_waiting": 0}
                assert (await http.get(f"/api/v1/sessions/{second.sid}", headers=headers[1])).json()["error_code"] == "ASR_TIMEOUT"
                evidence["checks"]["other_account_positive_after_release"] = True

                # Do not conceal a permanently-running dispatched row with a
                # manual finished(), startup recovery, or a new write window.
                assert len(rows) == 2
                by_sid = {row["session_id"]: row for row in rows}
                for item, actor in [(first, actors[0]), (second, actors[1])]:
                    row = by_sid[item.sid]
                    assert row["user_id"] == actor["user"]["id"]
                    assert row["attempt_no"] == row["call_no"] == 1 and row["learning_event_id"] is None
                    assert (row["provider"], row["model_id"], row["purpose"]) == ("mlx_whisper", MODEL, "asr_finish")
                    assert row["processed_audio_samples"] == 4000
                    assert row["prompt_tokens"] is row["completion_tokens"] is None
                    assert row["status"] in ("failed", "cancelled", "unknown")
                    assert row["ended_at"] is not None and row["aggregated_at"] is not None
                assert by_sid[first.sid]["status"] == "failed" and by_sid[first.sid]["error_code"] == "ASR_FAILED"
                # ASR local cancellation and timeout are distinct truthful
                # terminal forms; neither permits a running row or success.
                assert by_sid[second.sid]["error_code"] in ("ASR_TIMEOUT", "CANCELLED", "CLIENT_INTERRUPTED")
                assert readback_at <= second.finish, "finish10 elapsed before terminal bookkeeping was readable"
                assert len(daily) == 2
                for item in daily:
                    call = next(row for row in rows if row["user_id"] == item["user_id"])
                    assert (item["calls"], item["successes"], item["failures"], item["unknown_calls"]) == (
                        1, 0, 1, int(call["status"] == "unknown"))
                    assert item["processed_audio_samples"] == 4000
                    assert item["prompt_tokens_known"] == item["completion_tokens_known"] == 0
                    assert item["prompt_tokens_unknown_calls"] == item["completion_tokens_unknown_calls"] == 1
                evidence["checks"]["terminal_accounting_inside_finish10"] = True
            finally:
                # Evidence serialization cannot prevent owned task/gate cleanup.
                try:
                    record_property("asr_finish_queue_deadline", json.dumps(evidence, default=str))
                finally:
                    for timer in timers:
                        timer.cancel()
                    for gate in control.gates.values():
                        gate.set()
                    owned = tasks + [wire.task for wire in wires if wire.task is not None]
                    for task in owned:
                        if not task.done():
                            task.cancel()
                    if owned:
                        async with asyncio.timeout(5):
                            await asyncio.gather(*owned, return_exceptions=True)
    client.portal.call(run)
