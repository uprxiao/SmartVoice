"""CAP15 additions: real PG/API/ASGI WS + production cache/ASR scheduler.

Only model preparation and physical decoding are substituted. Synthetic PCM,
fixture model-call rows, and controlled epoch checks are NOT physical-model,
microphone/native, TCP, throughput, or real-elapsed-600-second evidence.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import json
import struct
import time
from types import SimpleNamespace
import uuid

import httpx
import pytest
from sqlalchemy import select

from app.core.config import Config, SESSION_PCM_RESERVE
from app.models import VoiceSession
from app.providers.asr.mlx_whisper import MlxWhisperProvider, MlxAsrSession
from app.services.asr_core import AsrEngine
from app.services.inference_context import current_scope
from tests.conftest import auth_headers, register_and_login
from tests.integration.test_capacity_normal_completion import AsgiPcmStream
from tests.test_admin import login_admin


async def until(predicate, seconds=3):
    # Coordination guard only: does not alter any production deadline.
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(.01)


@dataclass
class PhysicalGate:
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    returned: asyncio.Event = field(default_factory=asyncio.Event)


class PhysicalFaultWorker:
    """Fake physical worker only; no retained PCM in diagnostic history.

    It exposes its ONE live work reference, clears it before returning or
    confirming cancellation, and fails finish calls. Production engine/session
    buffering, scheduling, scope, cancellation and ledger wiring remain real.
    """
    def __init__(self, engine, control):
        self.engine, self.control = engine, control
        self.process = SimpleNamespace(is_alive=lambda: True)
        self.active_audio = None
        self.calls = []

    async def decode(self, args, deadline):
        assert len(self.engine.scheduler.running) == 1
        owner, job = next(iter(self.engine.scheduler.running.items()))
        user_id, session_id, attempt_no = owner
        assert user_id and session_id and attempt_no
        # Partial tasks carry the explicit AsrEngine scope argument; they need
        # not inherit a ContextVar from the WS receiver. Finish also inherits it.
        scope = current_scope.get()
        if scope is not None:
            assert owner == (scope.user_id, scope.session_id, scope.attempt_no)
        audio = args[0]
        assert audio and asyncio.get_running_loop().time() < deadline
        self.active_audio = audio
        self.calls.append((owner, job.priority, len(audio), hashlib.sha256(audio).hexdigest()))
        gate = self.control.gates.get((session_id, attempt_no))
        try:
            if job.priority == "low":
                # Empty partial leaves original PCM untrimmed; finish still
                # runs the real production high-priority path and fails below.
                return {"words": [], "segments": [], "seconds": 0.0}
            if gate is not None:
                self.control.jobs[(session_id, attempt_no)] = job
                gate.entered.set()
                try:
                    await gate.release.wait()
                except asyncio.CancelledError:
                    gate.cancelled.set()
                    raise
            raise RuntimeError("CAP15 synthetic physical ASR fault")
        finally:
            self.active_audio = None
            audio, args = b"", None
            if job.priority == "high" and gate is not None:
                gate.returned.set()

    async def stop(self):
        assert self.active_audio is None
        self.process = None


@pytest.fixture
def cache_client(monkeypatch, request):
    # Validated deployment override: byte limit is independently reachable while
    # count stays 20, TTL stays 600 and active reservation/deadlines stay original.
    monkeypatch.setenv("SVI_ASR_PROVIDER", "mlx_whisper")
    monkeypatch.setenv("SVI_LLM_PROVIDER", "mock")
    from app import main as main_module
    from app.services import session_runtime as runtime_module

    control = SimpleNamespace(gates={}, jobs={}, streams={}, workers=[], epoch=None)
    original_load, original_open = main_module.load_config, MlxWhisperProvider.open

    def configured(path=None):
        base = original_load(path)
        payload = base.model_dump()
        payload["retry"].update(audio_ttl_seconds=600, max_cached_sessions=20,
                                audio_budget_bytes=SESSION_PCM_RESERVE)
        validated = Config.model_validate(payload)
        return base.model_copy(update={"retry": validated.retry})

    async def physical_prepare(engine):
        # No model path access, process creation, network or MLX import.
        engine.model_id = "fixture-cap15-no-physical-model"
        worker = PhysicalFaultWorker(engine, control)
        engine.workers = [worker]
        control.workers.append(worker)

    async def observed_open(provider, options):
        stream = await original_open(provider, options)
        assert isinstance(stream, MlxAsrSession)
        control.streams[(options.session_id, options.attempt_no)] = stream
        return stream

    # Replace this module's epoch reader only, never shared time.time or loop
    # clocks. Used in exactly the expiry parameter AFTER real retry has begun.
    epoch_reader = SimpleNamespace(
        time=lambda: time.time() if control.epoch is None else control.epoch,
        monotonic=time.monotonic,
    )
    monkeypatch.setattr(main_module, "load_config", configured)
    monkeypatch.setattr(AsrEngine, "prepare", physical_prepare)
    monkeypatch.setattr(MlxWhisperProvider, "open", observed_open)
    monkeypatch.setattr(runtime_module, "time", epoch_reader)
    client = request.getfixturevalue("app_client")
    config = client.app.state.config
    assert config.retry.audio_ttl_seconds == 600 and config.retry.max_cached_sessions == 20
    assert config.retry.audio_budget_bytes == SESSION_PCM_RESERVE == 5_760_000
    assert config.asr.execution_slots == 1 and config.asr.finish_timeout_ms == 10_000
    assert config.asr.decode_timeout_ms == 3_000
    assert config.pipeline.stop_to_result_budget_ms == 30_000
    assert isinstance(client.app.state.asr, MlxWhisperProvider)
    assert len(control.workers) == 1
    return client, control


async def read_session(app, owner, sid):
    async with app.state.db.session_factory() as db:
        row = (await db.execute(select(
            VoiceSession.state, VoiceSession.current_attempt_no,
            VoiceSession.first_asr_failed_at, VoiceSession.retry_cache_status,
            VoiceSession.received_samples, VoiceSession.audio_complete,
            VoiceSession.current_result_id,
        ).where(VoiceSession.user_id == owner, VoiceSession.id == sid))).one()
        return row._asdict()


async def wait_cache_status(app, item, expected):
    async with asyncio.timeout(3):
        while True:
            row = await read_session(app, item.runtime.user_id, item.sid)
            if row["retry_cache_status"] == expected:
                return row
            await asyncio.sleep(.02)


async def detail(http, headers, sid):
    response = await http.get(f"/api/v1/sessions/{sid}", headers=headers)
    assert response.status_code == 200
    return response.json()


async def metrics(http, admin):
    response = await http.get("/api/admin/capacity", headers=admin)
    assert response.status_code == 200
    body = response.json()
    assert body["deployment"]["retry_pcm_budget_bytes"] == SESSION_PCM_RESERVE
    assert body["runtime"] is not None
    return body["runtime"]


def assert_pcm_metrics(value, *, active, retry, slots):
    assert value["active_sessions"] == slots
    assert value["active_pcm_bytes"] == active
    assert value["retry_pcm_bytes"] == retry
    assert value["reserved_pcm_bytes"] == slots * SESSION_PCM_RESERVE


async def failed_upload(http, app, headers, control, samples):
    """Reuse the bounded actual ASGI transport, with legal long/short frames.

    Samples are sent as fast as each ACK permits: PCM length boundary evidence,
    not 180 seconds of real microphone recording/cadence evidence.
    """
    request_id = str(uuid.uuid4())
    created = await http.post("/api/v1/sessions", headers={**headers, "Idempotency-Key": request_id},
        json={"client_request_id": request_id, "learning_enabled": False})
    assert created.status_code == 201
    sid = created.json()["session_id"]
    runtime = app.state.sessions.runtimes[sid]
    original_audio = runtime.audio
    stream = control.streams[(sid, 1)]
    provider_buffer = stream.buffer
    wire = AsgiPcmStream(app, SimpleNamespace(headers=lambda: headers), sid, 73, "")
    scope = {
        "type": "websocket", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "scheme": "ws", "root_path": "",
        "path": f"/ws/v1/sessions/{sid}/audio",
        "raw_path": f"/ws/v1/sessions/{sid}/audio".encode(), "query_string": b"",
        "headers": [(b"host", b"testserver"), (b"authorization", headers["Authorization"].encode())],
        "client": ("127.0.0.1", 50000), "server": ("testserver", 80),
        "subprotocols": [], "extensions": {"websocket.http.response": {}}, "state": {},
    }
    wire.app_task = asyncio.create_task(app(scope, wire.incoming.get, wire.outgoing.put))
    try:
        await wire._send({"type": "websocket.connect"})
        assert (await wire._message())["type"] == "websocket.accept"
        ready = await wire._event()
        assert ready["type"] == "ready" and ready["audio"]["chunk_samples"] == 4000
        sent, seq = 0, 0
        while sent < samples:
            count = min(4000, samples - sent)
            await wire._send({"type": "websocket.receive",
                "bytes": struct.pack("<II", seq, count) + struct.pack("<h", 73) * count})
            # The 2-second ACK deadline includes any interleaved partials.
            async with asyncio.timeout(2):
                while True:
                    event = await wire._event(timeout=2)
                    if event["type"] != "asr.partial":
                        break
            sent += count
            assert event["type"] == "audio.ack" and event["seq"] == seq
            assert event["total_samples"] == sent
            seq += 1
        assert len(original_audio) == 2 * samples
        # A real provider bytearray exists, unlike the standard Mock provider.
        assert len(provider_buffer) == 2 * samples
        await wire._send({"type": "websocket.receive", "text": json.dumps({
            "type": "audio.end", "last_seq": seq - 1, "total_samples": samples})})
        end_ack = False
        async with asyncio.timeout(12):
            while True:
                event = await wire._event(timeout=12)
                if event["type"] == "asr.partial":
                    continue
                if event["type"] == "audio.end_ack":
                    end_ack = True
                    continue
                assert end_ack and event["type"] == "error"
                assert event["error"]["code"] == "ASR_FAILED"
                break
        closed = await wire._message()
        assert closed["type"] == "websocket.close" and closed["code"] == 1011
        async with asyncio.timeout(3):
            await wire.app_task
    finally:
        await wire.dispose()
    await until(lambda: runtime.task is None or runtime.task.done())
    saved = await detail(http, headers, sid)
    assert saved["state"] == "failed" and saved["audio_complete"]
    assert saved["received_samples"] == samples and saved["retry_kind"] == "asr"
    assert runtime.audio is original_audio and len(original_audio) == 2 * samples
    assert provider_buffer == b"" and stream.cancelled and runtime.asr is None
    assert control.workers[0].active_audio is None
    row = await read_session(app, runtime.user_id, sid)
    expiry = row["first_asr_failed_at"].timestamp() + 600
    assert runtime.retry_expires_at == expiry and 0 < expiry - time.time() <= 600
    return SimpleNamespace(sid=sid, runtime=runtime, audio=original_audio, stream=stream,
                           provider_buffer=provider_buffer, samples=samples, expiry=expiry,
                           first_failure=row["first_asr_failed_at"])


@pytest.mark.parametrize("short_samples", [1, 4000], ids=["one-sample-tail", "one-frame-tail"])
def test_cap15_failed_pcm_byte_limit_exact_then_one_sample_over(cache_client, short_samples):
    client, control = cache_client
    headers = auth_headers(register_and_login(client))
    admin = auth_headers(login_admin(client))

    async def run():
        app = client.app
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as http:
            long = await failed_upload(http, app, headers, control, 2_880_000 - short_samples)
            short = await failed_upload(http, app, headers, control, short_samples)
            assert long.expiry < short.expiry
            assert len(long.audio) + len(short.audio) == SESSION_PCM_RESERVE
            assert_pcm_metrics(await metrics(http, admin), active=0, retry=SESSION_PCM_RESERVE, slots=0)
            assert (await detail(http, headers, long.sid))["retry_kind"] == "asr"
            extra = await failed_upload(http, app, headers, control, 1)
            await wait_cache_status(app, long, "evicted")
            # Hold all original references; map deletion alone cannot pass.
            assert long.audio == b"" and long.runtime.audio == b""
            assert long.provider_buffer == b"" and long.stream.cancelled
            assert long.runtime.retry_expires_at is None and long.runtime.asr is None
            assert long.sid not in app.state.sessions.runtimes
            evicted = await detail(http, headers, long.sid)
            assert evicted["state"] == "failed" and evicted["retry_kind"] == "none"
            assert evicted["retry_unavailable_reason"] == "audio_evicted"
            assert len(short.audio) == 2 * short_samples and len(extra.audio) == 2
            cached = [r for r in app.state.sessions.runtimes.values() if r.state == "failed" and r.audio]
            assert {r.session_id for r in cached} == {short.sid, extra.sid}
            assert len(cached) == 2 < 20  # This failure is bytes, not count.
            assert_pcm_metrics(await metrics(http, admin), active=0, retry=2 * (short_samples + 1), slots=0)
            assert control.workers[0].active_audio is None
            assert app.state.asr.scheduler.snapshot() == {"running": 0, "high_waiting": 0, "low_waiting": 0}
    client.portal.call(run)


@pytest.mark.parametrize("release_kind", ["cancel", "controlled-expiry"], ids=["cancel", "epoch600-boundary"])
def test_cap15_retry_transfers_once_preserves_ttl_and_releases_all_owned_copies(cache_client, release_kind):
    client, control = cache_client
    headers = auth_headers(register_and_login(client))
    admin = auth_headers(login_admin(client))

    async def run():
        app, manager, worker = client.app, client.app.state.sessions, control.workers[0]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as http:
            item = await failed_upload(http, app, headers, control, 8000)
            size, previous = len(item.audio), item.runtime
            assert_pcm_metrics(await metrics(http, admin), active=0, retry=size, slots=0)
            try:
                for attempt in (2, 3):
                    gate = PhysicalGate()
                    control.gates[(item.sid, attempt)] = gate
                    request_key = str(uuid.uuid4())
                    response = await http.post(f"/api/v1/sessions/{item.sid}/retry",
                        headers={**headers, "Idempotency-Key": request_key}, json={"retry_kind": "asr"})
                    assert response.status_code == 202 and response.json()["current_attempt_no"] == attempt
                    current = manager.runtimes[item.sid]
                    assert current is not previous and previous.audio == b""
                    assert current.audio is item.audio and len(item.audio) == size
                    assert current.retry_expires_at == item.expiry
                    await until(gate.entered.is_set)
                    stream = control.streams[(item.sid, attempt)]
                    provider_buffer = stream.buffer
                    job = control.jobs[(item.sid, attempt)]
                    assert len(provider_buffer) == size and worker.active_audio is not None
                    assert len(worker.active_audio) == size and worker.active_audio == bytes(item.audio)
                    assert job.started and not job.done.is_set()
                    assert_pcm_metrics(await metrics(http, admin), active=size, retry=0, slots=1)
                    row = await read_session(app, current.user_id, item.sid)
                    assert row["first_asr_failed_at"] == item.first_failure
                    assert row["state"] == "finalizing" and row["current_attempt_no"] == attempt
                    if attempt == 2:
                        # Repeat failure returns the SAME object to the cache,
                        # without resetting first failure/expiry or counting twice.
                        gate.release.set()
                        await until(lambda: current.state == "failed" and current.task.done())
                        assert current.audio is item.audio and len(item.audio) == size
                        assert current.retry_expires_at == item.expiry
                        assert provider_buffer == b"" and stream.cancelled
                        assert worker.active_audio is None and job.done.is_set() and job.operation is None
                        assert_pcm_metrics(await metrics(http, admin), active=0, retry=size, slots=0)
                        row = await read_session(app, current.user_id, item.sid)
                        assert row["first_asr_failed_at"] == item.first_failure
                        assert row["retry_cache_status"] == "available"
                        calls_before = len(worker.calls)
                        replay = await http.post(f"/api/v1/sessions/{item.sid}/retry",
                            headers={**headers, "Idempotency-Key": request_key}, json={"retry_kind": "asr"})
                        assert replay.status_code == 200 and replay.json()["current_attempt_no"] == 2
                        assert len(worker.calls) == calls_before and manager.runtimes[item.sid] is current
                        previous = current
                        continue
                    if release_kind == "cancel":
                        ended = await http.post(f"/api/v1/sessions/{item.sid}/cancel", headers=headers)
                        assert ended.status_code == 200 and ended.json()["state"] == "cancelled"
                    else:
                        # Controlled observation points; neither config TTL nor
                        # DB first_failure timestamp nor monotonic clocks change.
                        control.epoch = item.expiry - .001
                        assert await manager.expire_retry_audio(control.epoch) == 0
                        assert current.state == "finalizing" and len(item.audio) == size
                        control.epoch = item.expiry
                        assert await manager.expire_retry_audio(control.epoch) == 1
                    await until(lambda: gate.returned.is_set() and current.task.done()
                                and not manager.background_tasks)
                    assert gate.cancelled.is_set(), "work did not go through physical cancellation"
                    assert item.audio == b"" and previous.audio == b"" and current.audio == b""
                    assert provider_buffer == b"" and stream.cancelled and current.asr is None
                    assert worker.active_audio is None and current.retry_expires_at is None
                    assert job.done.is_set() and job.operation is None and job.task is None
                    assert app.state.asr.scheduler.snapshot() == {"running": 0, "high_waiting": 0, "low_waiting": 0}
                    final = await detail(http, headers, item.sid)
                    expected = "cancelled" if release_kind == "cancel" else "failed"
                    assert final["state"] == expected and final["current_attempt_no"] == 3
                    assert final["result"] is None and final["retry_kind"] == "none"
                    assert final["retry_unavailable_reason"] == (
                        "cancelled" if release_kind == "cancel" else "audio_expired")
                    row = await read_session(app, current.user_id, item.sid)
                    assert row["first_asr_failed_at"] == item.first_failure
                    assert row["retry_cache_status"] == ("none" if release_kind == "cancel" else "expired")
                    assert_pcm_metrics(await metrics(http, admin), active=0, retry=0, slots=0)
                    again = await http.post(f"/api/v1/sessions/{item.sid}/retry",
                        headers={**headers, "Idempotency-Key": str(uuid.uuid4())}, json={"retry_kind": "asr"})
                    assert again.status_code == 409 and again.json()["error"]["code"] == "RETRY_UNAVAILABLE"
                    assert again.json()["error"]["details"]["reason"] == (
                        "cancelled" if release_kind == "cancel" else "audio_expired")
                    assert await manager.expire_retry_audio(item.expiry) == 0
            finally:
                # A failing assertion must not strand a gated physical stub.
                for gate in control.gates.values():
                    gate.release.set()
                active = manager.runtimes.get(item.sid)
                if active and active.state in ("created", "streaming", "finalizing", "refining"):
                    async with asyncio.timeout(5):
                        await http.post(f"/api/v1/sessions/{item.sid}/cancel", headers=headers)
                control.epoch = None
    client.portal.call(run)
