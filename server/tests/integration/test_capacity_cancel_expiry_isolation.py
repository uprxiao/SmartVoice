"""CAP11 N20 cross-owner control: real PG/API/ASGI/adapter/scheduler/ledger.

Only preparation/physical decode are synthetic. expire_retry_audio(original_D)
is an explicitly controlled cleanup invocation, NOT natural time/600s evidence.
Reuse the passed natural600 + real OS evidence; no clock/TTL/state/ledger patch.
"""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
import json
import struct
from types import SimpleNamespace
import uuid

import httpx
import pytest
from sqlalchemy import func, select

from app.core.config import Config
from app.models import Insertion, LearningEvent, ModelCall, SessionAttempt, SessionResult, VoiceSession
from app.providers.asr.mlx_whisper import MlxWhisperProvider
from app.services.asr_core import AsrEngine
from app.services.inference_context import current_scope
from tests.conftest import auth_headers
from tests.integration.test_capacity_normal_completion import AsgiPcmStream
from tests.integration.test_capacity_retry_cache_boundaries import failed_upload, PhysicalGate
from tests.integration.test_normal_shutdown_contract import create_session, require_owned_database, safe_status


async def until(predicate, seconds=3):
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(.005)


class PhysicalWorker:
    """One retained worker sentinel per real scheduler slot; no OS exit claim.

    On cancellation, deliberately return a structurally valid old decode only
    after the cleanup gate is released. Scheduler must discard that old output.
    The gate is bounded by the original physical deadline, not a fresh budget.
    """
    def __init__(self, engine, control, slot):
        self.engine, self.control, self.slot = engine, control, slot
        self.process = SimpleNamespace(is_alive=lambda: True)
        self.active_audio, self.owner = None, None
        self.stopped = False

    async def decode(self, args, deadline):
        scope = current_scope.get()
        assert scope is not None
        owner = (scope.user_id, scope.session_id, scope.attempt_no)
        index = self.control.indices[scope.user_id]
        job = self.engine.scheduler.running[owner]
        assert job.priority == "high" and self.owner is None
        assert asyncio.get_running_loop().time() < deadline
        audio, _path, language, prompt = args
        assert language == "auto" and prompt == "" and isinstance(audio, bytearray)
        samples = len(audio) // 2
        assert samples == (8000 if index == 0 else 4000)
        assert audio[:2] == struct.pack("<h", 73 if index == 0 else 100 + index)
        self.owner, self.active_audio = owner, audio
        self.control.jobs[owner] = job
        self.control.owners[owner] = self
        self.control.refs[owner] = audio  # Mutable alias only; no copied PCM.
        gate = self.control.gates.get(index)
        try:
            if index == 0 and scope.attempt_no == 1:
                raise RuntimeError("declared CAP11 initial physical failure")
            self.control.dispatched.append(owner)
            if gate is not None:
                gate.entered.set()
                async with asyncio.timeout_at(deadline):
                    while not gate.release.is_set():
                        try:
                            await gate.release.wait()
                        except asyncio.CancelledError:
                            if asyncio.get_running_loop().time() >= deadline:
                                raise
                            gate.cancelled.set()
            value = {"words": [(0.0, samples / 16000, f"容量隔离账户{index:02d}完成。")],
                     "segments": [samples / 16000], "seconds": 0.0}
            self.control.returned.append(owner)
            return value
        finally:
            self.active_audio, self.owner = None, None
            audio, args = None, None
            if gate is not None and not (index == 0 and scope.attempt_no == 1):
                gate.returned.set()

    async def stop(self):
        assert self.owner is None and self.active_audio is None
        self.process, self.stopped = None, True


@pytest.fixture
def isolation_client(monkeypatch, request):
    monkeypatch.setenv("SVI_ASR_PROVIDER", "mlx_whisper")
    monkeypatch.setenv("SVI_LLM_PROVIDER", "mock")
    from app import main
    original_load, original_open = main.load_config, MlxWhisperProvider.open
    control = SimpleNamespace(indices={}, streams={}, workers=[], jobs={}, owners={}, refs={},
                              gates={i: PhysicalGate() for i in range(3)}, dispatched=[], returned=[])

    def config(path=None):
        base = original_load(path)
        payload = base.model_dump()
        payload["asr"]["execution_slots"] = 3
        validated = Config.model_validate(payload)
        return base.model_copy(update={"asr": validated.asr})

    async def prepare(engine):
        engine.model_id = "cap11-declared-physical-stub"
        engine.workers = [PhysicalWorker(engine, control, i) for i in range(3)]
        control.workers.extend(engine.workers)

    async def opened(provider, options):
        stream = await original_open(provider, options)
        control.streams[(options.session_id, options.attempt_no)] = stream
        return stream

    monkeypatch.setattr(main, "load_config", config)
    monkeypatch.setattr(AsrEngine, "prepare", prepare)
    monkeypatch.setattr(MlxWhisperProvider, "open", opened)
    client = request.getfixturevalue("app_client")
    return client, control


class OneFramePeer(AsgiPcmStream):
    """Reuse bounded transport/start/dispose; only terminal oracle differs."""
    async def _run(self):
        await self._send({"type": "websocket.connect"})
        assert (await self._message())["type"] == "websocket.accept"
        ready = await self._event()
        assert ready["type"] == "ready" and ready["session_id"] == self.session_id
        assert ready["attempt_no"] == 1 and ready["audio"]["chunk_samples"] == 4000
        self.last_seq, self.total_samples = 0, 4000
        await self._send({"type": "websocket.receive", "bytes": struct.pack("<II", 0, 4000) + self.pcm})
        ack = await self._event(timeout=2)
        assert ack == {"type": "audio.ack", "session_id": self.session_id, "seq": 0, "total_samples": 4000}
        self.started.set()
        await asyncio.sleep(.25)  # One legal 250ms PCM frame, not a throughput load.
        await self.finish_requested.wait()
        await self._send({"type": "websocket.receive", "text": json.dumps(
            {"type": "audio.end", "last_seq": 0, "total_samples": 4000})})
        final_seen = end_seen = False
        async with asyncio.timeout(32):  # Observation guard; product total stays30.
            while True:
                event = await self._event(timeout=32)
                assert event["session_id"] == self.session_id
                kind = event["type"]
                if kind == "audio.end_ack":
                    assert event["attempt_no"] == 1 and event["last_seq"] == 0 and event["total_samples"] == 4000
                    end_seen = True
                elif kind == "asr.final":
                    assert end_seen and not final_seen and self.expected_text is not None
                    assert event["text"] == self.expected_text
                    final_seen = True
                elif kind == "result.ready":
                    assert end_seen and final_seen and self.expected_text is not None
                    self.result = event["result"]
                    assert self.result["raw_text"] == self.result["refined_text"] == self.expected_text
                    assert self.result["review_required"] is False
                    break
                elif kind == "cancelled":
                    assert self.expected_text is None and not final_seen and self.result is None
                    break
                else:
                    raise AssertionError({"type": kind, "code": event.get("error", {}).get("code")})
        closed = await self._message()
        assert closed["type"] == "websocket.close" and closed["code"] == 1000
        await self.app_task


def test_cap11_n20_cancel_expiry_and_healthy_owners_isolate(isolation_client, record_property):
    require_owned_database()
    client, control = isolation_client

    async def scenario():
        app, manager, engine = client.app, client.app.state.sessions, client.app.state.asr.engine
        cfg = app.state.config
        assert cfg.sessions.initial_max_active == cfg.sessions.admission_ceiling == 20
        assert cfg.retry.audio_ttl_seconds == 600 and cfg.asr.decode_timeout_ms == 3000
        assert cfg.asr.finish_timeout_ms == 10000 and cfg.pipeline.stop_to_result_budget_ms == 30000
        assert engine.scheduler.slots == 3 and engine.recorder is app.state.recorder
        wires, owned, runtimes = [], [], []
        evidence = {"n": 20, "execution_slots": 3, "natural600": False, "real_os": False,
                    "scope": "real PG/API/ASGI/adapter/scheduler/ledger; physical-stub cleanup gates"}
        try:
            async with AsyncExitStack() as stack:
                clients, headers, user_ids = [], [], []
                for index in range(20):
                    http = await stack.enter_async_context(httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app, client=(f"192.0.2.{index+1}", 52000+index)),
                        base_url="http://testserver"))
                    email, password = f"cap11-owner-{index}@example.invalid", "Cap11Fixture123!"
                    response = await http.post("/api/auth/register", json={"email": email, "password": password})
                    assert response.status_code == 201, safe_status(response)
                    response = await http.post("/api/auth/login", json={"email": email, "password": password,
                        "client_type": "desktop", "device": {"installation_id": str(uuid.uuid4()),
                        "name": "CAP11 fixture", "platform": "macos", "client_version": "0.0.1"}})
                    assert response.status_code == 200, safe_status(response)
                    actor = response.json()
                    clients.append(http); headers.append(auth_headers(actor)); user_ids.append(actor["user"]["id"])
                    control.indices[user_ids[-1]] = index
                item = await failed_upload(clients[0], app, headers[0], control, 8000)
                assert item.expiry == item.first_failure.timestamp() + 600
                response = await clients[0].post(f"/api/v1/sessions/{item.sid}/retry",
                    headers=headers[0] | {"Idempotency-Key": str(uuid.uuid4())}, json={"retry_kind": "asr"})
                assert response.status_code == 202 and response.json()["current_attempt_no"] == 2
                ttl = manager.runtimes[item.sid]
                runtimes.append(ttl)
                for index in range(1, 20):
                    response = await create_session(clients[index], headers[index])
                    assert response.status_code == 201, safe_status(response)
                    sid = response.json()["session_id"]
                    actor = SimpleNamespace(headers=lambda h=headers[index]: h)
                    wire = OneFramePeer(app, actor, sid, 100+index,
                        None if index == 1 else f"容量隔离账户{index:02d}完成。")
                    wires.append(wire); runtimes.append(manager.runtimes[sid])
                    await wire.start()  # Each successful create immediately connects.
                    if index <= 2:
                        wire.finish_requested.set()
                await until(lambda: all(g.entered.is_set() for g in control.gates.values()))
                assert len([r for r in manager.runtimes.values() if r.state in ("created", "streaming", "finalizing", "refining")]) == 20
                async with app.state.db.session_factory() as db:
                    assert await db.scalar(select(func.count()).select_from(VoiceSession).where(
                        VoiceSession.user_id.in_(user_ids),
                        VoiceSession.state.in_(("created", "streaming", "finalizing", "refining")))) == 20
                for wire in wires[2:]:
                    wire.finish_requested.set()
                await until(lambda: engine.scheduler.snapshot() == {"running": 3, "high_waiting": 17, "low_waiting": 0})
                owners = [(r.user_id, r.session_id, r.attempt_no) for r in runtimes]
                jobs = [control.jobs[o] for o in owners[:3]]
                healthy_worker = control.owners[owners[2]]
                healthy_generation = healthy_worker.process
                healthy_audio = control.refs[owners[2]]
                cancel = asyncio.create_task(clients[1].post(f"/api/v1/sessions/{runtimes[1].session_id}/cancel", headers=headers[1]))
                owned.append(cancel)
                # Controlled cleanup entry only. No actual clock, stored first
                # failure, lease deadline, or runtime state is assigned by test.
                assert await manager.expire_retry_audio(item.expiry) == 1
                await until(lambda: control.gates[0].cancelled.is_set() and control.gates[1].cancelled.is_set())
                assert not cancel.done()
                assert engine.scheduler.snapshot() == {"running": 3, "high_waiting": 17, "low_waiting": 0}
                assert all(engine.scheduler.running[o] is j and not j.done.is_set() for o, j in zip(owners[:3], jobs))
                assert not item.audio and not control.refs[owners[0]]
                assert healthy_audio and healthy_worker.process is healthy_generation
                assert not control.gates[2].cancelled.is_set() and not healthy_worker.stopped
                assert all(r.audio and not r.cancelled for r in runtimes[2:])
                assert set(control.dispatched) == set(owners[:3])
                evidence["held"] = {"running": 3, "queued": 17, "cancel_pending": True,
                    "ttl_pcm_zero": True, "cancel_pcm_bytes": len(control.refs[owners[1]]),
                    "healthy_pcm_present": True, "healthy_generation_unchanged": True}
                control.gates[0].release.set(); control.gates[1].release.set()
                response = await cancel
                assert response.status_code == 200 and response.json()["state"] == "cancelled"
                await until(lambda: all(g.returned.is_set() for g in (control.gates[0], control.gates[1])))
                await until(lambda: all(w.runner.done() for w in wires[2:]), 32)
                await asyncio.gather(*(w.runner for w in wires[2:]))
                assert not wires[1].runner.done() and healthy_audio
                assert healthy_worker.process is healthy_generation and not control.gates[2].cancelled.is_set()
                control.gates[2].release.set()
                await asyncio.gather(*(w.runner for w in wires))
                await until(lambda: not manager._processing_tasks and not manager.background_tasks)
                assert engine.scheduler.snapshot() == {"running": 0, "high_waiting": 0, "low_waiting": 0}
                assert all(not r.audio for r in runtimes) and all(not a for a in control.refs.values())
                assert all(not stream.buffer for stream in control.streams.values())
                assert all(w.active_audio is None and not w.stopped for w in control.workers)
                assert set(control.returned) == set(owners) and len(control.returned) == 20
                async with app.state.db.session_factory() as db:
                    for index, runtime in enumerate(runtimes):
                        session = await db.scalar(select(VoiceSession).where(VoiceSession.user_id == user_ids[index], VoiceSession.id == runtime.session_id))
                        result_count = await db.scalar(select(func.count()).select_from(SessionResult).where(SessionResult.user_id == user_ids[index], SessionResult.session_id == runtime.session_id))
                        if index < 2:
                            assert session.state == ("failed" if index == 0 else "cancelled") and result_count == 0
                            assert session.current_result_id is None
                            assert not any(await db.scalars(select(SessionAttempt.raw_text).where(SessionAttempt.user_id == user_ids[index], SessionAttempt.session_id == runtime.session_id)))
                        else:
                            assert session.state == "ready" and result_count == 1
                        calls = (await db.scalars(select(ModelCall).where(ModelCall.user_id == user_ids[index], ModelCall.session_id == runtime.session_id))).all()
                        assert calls and all(c.ended_at is not None and c.aggregated_at is not None for c in calls)
                    assert (await db.get(VoiceSession, item.sid)).first_asr_failed_at == item.first_failure
                    for model in (Insertion, LearningEvent):
                        assert await db.scalar(select(func.count()).select_from(model).where(model.user_id.in_(user_ids))) == 0
                evidence.update(completed=True, ready=18, cancelled=1, expired_failed=1,
                    stale_physical_returns_discarded=2, parent_refs_zero=True, original_failure_unchanged=True)
        finally:
            try:
                for gate in control.gates.values():
                    gate.release.set()
                for wire in wires:
                    await wire.dispose()
                if owned:
                    async with asyncio.timeout(12):
                        await asyncio.gather(*owned, return_exceptions=True)
            finally:
                record_property("cap11_n20_safe", json.dumps(evidence, sort_keys=True))
    client.portal.call(scenario)
