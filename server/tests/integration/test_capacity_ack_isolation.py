"""CAP12: real PG/lifespan/20 ASGI WS/OS worker; synthetic physical transcribe.

Only the twentieth ASGI send is gated. This is NOT TCP send-buffer evidence,
real MLX/model quality/throughput, microphone, or Desktop acceptance.
All configured budgets, pool sizes and admission limits remain unchanged.
"""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from contextvars import ContextVar
from datetime import datetime
import json
import os
import struct
import time
import uuid

import asyncpg
import httpx
import pytest
from sqlalchemy import event, select

from app import main
from app.models import ModelCall
from app.providers.asr.mlx_whisper import MlxWhisperProvider
from app.services import asr_core
from tests.conftest import PG_HOST, PG_PASSWORD, PG_PORT, PG_USER, SERVER_ROOT, TEST_DB, TEST_URL, wait_for_readiness_async
from tests.integration.cap12_owned_process_fixture import bootstrap
from tests.integration.test_normal_shutdown_contract import create_session, require_owned_database, safe_status


def safe_exception_locations(exc):
    """Metadata only: never render exception values, source lines or locals."""
    chain, seen = [], set()
    while exc is not None and id(exc) not in seen and len(chain) < 4:
        seen.add(id(exc))
        frames, tb = [], exc.__traceback__
        while tb is not None and len(frames) < 64:
            code = tb.tb_frame.f_code
            frames.append({"file": os.path.basename(code.co_filename),
                           "function": code.co_name, "line": tb.tb_lineno})
            tb = tb.tb_next
        chain.append({"type": type(exc).__name__, "frames": frames,
                      "frames_truncated": tb is not None})
        exc = exc.__cause__ if exc.__cause__ is not None else (
            None if exc.__suppress_context__ else exc.__context__)
    return {"chain": chain, "chain_truncated": exc is not None}


async def until(predicate, seconds):
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(.005)


def process_alive(process, popen):
    try:
        return process.is_alive()
    except ValueError:
        assert popen.returncode is not None, "closed Process without confirmed exit"
        return False


def child_events(folder, owner):
    rows = []
    for path in folder.glob("child-*.jsonl"):
        for line in path.read_text().splitlines(keepends=True):
            if not line.endswith("\n"):
                continue  # Only an in-progress final append is ignored.
            row = json.loads(line)
            assert row["parent_pid"] == owner["parent_pid"] == os.getpid()
            assert row["nonce"] == owner["nonce"] and path.name == f"child-{row['pid']}.jsonl"
            rows.append(row)
    return rows


class PcmPeer:
    """Independent bounded reader/sender; cumulative 32000-sample/2s credit."""
    def __init__(self, app, headers, sid, index):
        self.app, self.headers, self.sid, self.index = app, headers, sid, index
        self.incoming, self.outgoing = asyncio.Queue(2), asyncio.Queue(16)
        self.gate, self.gate_pending = asyncio.Event(), asyncio.Event()
        self.finish_requested, self.closed = asyncio.Event(), asyncio.Event()
        self.pending, self.acks, self.types = {}, [], []
        self.samples, self.acked, self.last_seq, self.ack_seq = 0, 0, -1, -1
        self.max_credit, self.in_peak, self.out_peak = 0, 0, 0
        self.end_acked = self.final_seen = False
        self.result = self.code = self.close_code = None
        self.gate_started = self.gate_cancelled = self.credit_stopped = None
        self.app_task = self.reader = self.writer = None

    async def transport_send(self, message):
        if self.index == 19 and message["type"] == "websocket.send":
            payload = json.loads(message["text"])
            if payload["type"] == "audio.ack" and not self.gate_pending.is_set():
                self.gate_started = asyncio.get_running_loop().time()
                self.gate_pending.set()
                try:
                    await self.gate.wait()  # Actual production send awaits here.
                except asyncio.CancelledError:
                    self.gate_cancelled = asyncio.get_running_loop().time()
                    raise
        await self.outgoing.put(message)
        self.out_peak = max(self.out_peak, self.outgoing.qsize())

    async def put(self, message):
        async with asyncio.timeout(2):
            await self.incoming.put(message)
        self.in_peak = max(self.in_peak, self.incoming.qsize())

    async def start(self):
        path = f"/ws/v1/sessions/{self.sid}/audio"
        scope = {"type": "websocket", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1", "scheme": "ws", "root_path": "", "path": path,
            "raw_path": path.encode(), "query_string": b"", "state": {}, "subprotocols": [],
            "headers": [(b"host", b"testserver"), (b"authorization", self.headers["Authorization"].encode())],
            "client": (f"192.0.2.{self.index + 1}", 50000 + self.index), "server": ("testserver", 80),
            "extensions": {"websocket.http.response": {}}}
        self.app_task = asyncio.create_task(self.app(scope, self.incoming.get, self.transport_send))
        await self.put({"type": "websocket.connect"})
        async with asyncio.timeout(4):
            accepted = await self.outgoing.get()
            assert accepted["type"] == "websocket.accept", {"type": accepted["type"]}
            message = await self.outgoing.get()
        assert message["type"] == "websocket.send"
        ready = json.loads(message["text"])
        assert ready == {"type": "ready", "session_id": self.sid, "attempt_no": 1,
            "audio": {"encoding": "pcm_s16le", "sample_rate_hz": 16000, "channels": 1,
                      "chunk_samples": 4000, "max_samples": 2880000}}
        self.reader = asyncio.create_task(self.read())

    async def read(self):
        loop, previous_revision = asyncio.get_running_loop(), 0
        while True:
            message = await self.outgoing.get()
            if message["type"] == "websocket.close":
                self.close_code = message["code"]
                self.closed.set()
                await self.app_task
                return
            assert message["type"] == "websocket.send"
            data = json.loads(message["text"])
            assert data["session_id"] == self.sid
            kind = data["type"]
            self.types.append(kind)
            if kind == "audio.ack":
                seq, total = data["seq"], data["total_samples"]
                assert self.ack_seq < seq <= self.last_seq and total == (seq + 1) * 4000
                assert self.acked < total <= self.samples and self.pending
                latency = loop.time() - min(self.pending.values())
                self.acks.append({"seq": seq, "samples": total, "latency_ms": latency * 1000})
                assert latency < 2, {"normal_peer": self.index, "ack_ms": latency * 1000}
                self.pending = {key: value for key, value in self.pending.items() if key > seq}
                self.acked, self.ack_seq = total, seq
            elif kind == "asr.partial":
                assert data["revision"] > previous_revision
                previous_revision = data["revision"]
                assert 0 <= data["decoded_samples"] <= data["received_samples"] <= self.samples
            elif kind == "audio.end_ack":
                assert data["attempt_no"] == 1 and not self.end_acked
                assert data["last_seq"] == self.last_seq and data["total_samples"] == self.samples
                self.end_acked = True
            elif kind == "asr.final":
                assert self.end_acked and not self.final_seen and data["attempt_no"] == 1
                assert data["text"] == f"容量测试账户{100 + self.index}完成。"
                self.final_seen = True
            elif kind == "result.ready":
                assert self.final_seen and self.result is None and data["attempt_no"] == 1
                self.result = data["result"]
                assert self.result["raw_text"] == self.result["refined_text"] == f"容量测试账户{100 + self.index}完成。"
                assert self.result["review_required"] is False
            elif kind == "error":
                self.code = data["error"]["code"]
                assert self.index == 19 and self.code == "CLIENT_BACKPRESSURE", {"peer": self.index, "code": self.code}
            elif kind != "cancelled":
                raise AssertionError({"unexpected_event": kind})

    async def stream(self, origin):
        loop = asyncio.get_running_loop()
        while not self.finish_requested.is_set() and not self.closed.is_set():
            due = origin + (self.last_seq + 1) * .25
            await asyncio.sleep(max(0, due - loop.time()))
            if self.finish_requested.is_set() or self.closed.is_set():
                break
            age = loop.time() - min(self.pending.values()) if self.pending else 0
            if self.samples - self.acked + 4000 > 32000 or age >= 2:
                assert self.index == 19, {"peer": self.index, "credit": self.samples - self.acked, "age_ms": age * 1000}
                self.credit_stopped = loop.time()
                return  # Keep socket open for actual server-side timeout; no audio.end.
            assert loop.time() - due < 2, "synthetic capture queue itself exceeded original age budget"
            self.last_seq += 1
            self.samples += 4000
            self.pending[self.last_seq] = due  # Before put: includes ASGI input-queue time.
            self.max_credit = max(self.max_credit, self.samples - self.acked)
            await self.put({"type": "websocket.receive", "bytes":
                struct.pack("<II", self.last_seq, 4000) + struct.pack("<h", 100 + self.index) * 4000})
        if self.index != 19:
            await until(lambda: not self.pending, 2)
            await self.put({"type": "websocket.receive", "text": json.dumps({
                "type": "audio.end", "last_seq": self.last_seq, "total_samples": self.samples})})


@pytest.mark.asyncio
async def test_tc_cap_12_twenty_realtime_streams_isolate_slow_send_and_reclaim_decoder(
    clean_database, monkeypatch, tmp_path, record_property,
):
    require_owned_database()
    if os.name != "posix":
        pytest.skip("Owned real spawn/terminate path is this Mac/POSIX parameter")
    folder = tmp_path / "owned-cap12-physical-stub"
    folder.mkdir(mode=0o700)
    owner = {"parent_pid": os.getpid(), "nonce": uuid.uuid4().hex}
    (folder / "owner.json").write_text(json.dumps(owner))
    (folder / "config.json").write_text('{"synthetic_only":true}\n')
    (folder / "weights.npz").write_bytes(b"CAP12 synthetic bootstrap, not model weights\n")
    for name, value in {"SVI_DATABASE_URL": TEST_URL, "SVI_CONFIG": str(SERVER_ROOT / "config/app.yaml"),
        "SVI_INSTANCE_LOCK": "1", "SVI_ENVIRONMENT": "development", "SVI_ASR_PROVIDER": "mlx_whisper",
        "SVI_ASR_MODEL_PATH": str(folder), "SVI_CAP12_OWNED_DIR": str(folder), "SVI_LLM_PROVIDER": "mock",
        "WEB_CONCURRENCY": "1", "UVICORN_WORKERS": "1"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(asr_core, "_worker_main", bootstrap)
    app, loop = main.create_app(str(SERVER_ROOT / "config/app.yaml")), asyncio.get_running_loop()
    started, stop, monitor_stop = asyncio.Event(), asyncio.Event(), asyncio.Event()
    evidence = {"scope": "real-PG+ASGI+owned-OS; synthetic-physical-transcribe; not-TCP",
                "issues": [], "worker_states": [], "frames": [], "leases": [], "other_begins": 0}
    peers, clients, retained, leases, tasks = [], [], {}, {}, []
    control = monitor = None
    tag = ContextVar("cap12_frame", default=None)
    listeners, original_close, processing_started = [], None, None
    deployment_lock_record = None

    actual_instance_watch = main._watch_instance_lock

    async def observed_instance_watch(watched_app, connection, backend_pid):
        nonlocal deployment_lock_record
        # Read the already-owned connection object only; no SQL, checkout or PID lookup.
        record = connection.sync_connection._dbapi_connection._connection_record
        deployment_lock_record = record
        evidence["deployment_lock_record_id"] = id(record)
        return await actual_instance_watch(watched_app, connection, backend_pid)

    monkeypatch.setattr(main, "_watch_instance_lock", observed_instance_watch)

    async def lifespan():
        async with app.router.lifespan_context(app):
            started.set()
            await stop.wait()

    parent = asyncio.create_task(lifespan())
    try:
        async with asyncio.timeout(8):
            await started.wait()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://testserver") as health_client:
                await wait_for_readiness_async(health_client)
        cfg, manager, engine = app.state.config, app.state.sessions, app.state.asr.engine
        assert isinstance(app.state.asr, MlxWhisperProvider) and type(engine.workers[0]) is asr_core.DecodeWorker
        assert cfg.database.pool_size == 5 and cfg.database.max_overflow == 5
        assert cfg.sessions.initial_max_active == cfg.sessions.admission_ceiling == 20
        assert cfg.sessions.unacked_limit_ms == 2000 and cfg.asr.decode_timeout_ms == 3000
        assert cfg.asr.finish_timeout_ms == 10000 and cfg.pipeline.stop_to_result_budget_ms == 30000
        assert engine.scheduler.slots == len(engine.workers) == 1
        assert engine.recorder is app.state.recorder and type(engine.recorder).__name__ == "DatabaseCallRecorder"
        worker = engine.workers[0]
        original_close = app.state.db.close

        async def observed_close():
            evidence["pool_before_dispose"] = app.state.db.engine.pool.checkedout()
            await original_close()
            evidence["db_disposed"] = True

        monkeypatch.setattr(app.state.db, "close", observed_close)
        actual_frame = manager._accept_frame

        async def observed_frame(runtime, frame):
            row = {"session_id": runtime.session_id, "seq": struct.unpack_from("<I", frame)[0],
                   "entered": time.monotonic(), "saved_before": runtime.progress_saved_at, "begins": 0}
            token = tag.set((asyncio.current_task(), row))
            try:
                return await actual_frame(runtime, frame)
            finally:
                row.update(exited=time.monotonic(), saved_after=runtime.progress_saved_at)
                evidence["frames"].append(row)
                tag.reset(token)

        monkeypatch.setattr(manager, "_accept_frame", observed_frame)

        def current_frame():
            value = tag.get()
            return value[1] if value and value[0] is asyncio.current_task() else None

        def on_begin(connection):
            row = current_frame()
            if row is None:
                evidence["other_begins"] += 1
            else:
                row["begins"] += 1

        def on_checkout(connection, record, proxy):
            row = current_frame()
            task = asyncio.current_task()
            assert id(record) not in leases
            leases[id(record)] = {"start": loop.time(), "phase": "ws_frame" if row else "independent",
                "task": getattr(task.get_coro(), "__qualname__", "unknown") if task else "none",
                "seq": row["seq"] if row else None}

        def on_checkin(connection, record):
            if id(record) not in leases:
                observed = {
                    "record_id": id(record),
                    "deployment_lock_match": id(record) == evidence.get("deployment_lock_record_id"),
                }
                evidence.setdefault("unmatched_checkins", []).append(observed)
                if record is deployment_lock_record:
                    # Only this already-held connection predates our checkout listener.
                    # All other unmatched records still reach the original strict pop.
                    observed.update(shutdown_requested=stop.is_set(),
                        shutdown_owner=asyncio.current_task() is app.state.shutdown_task,
                        outside_frame=current_frame() is None)
                    assert observed["shutdown_requested"] and observed["shutdown_owner"]
                    assert observed["outside_frame"]
                    returned = evidence.setdefault("preexisting_deployment_lock_checkins", [])
                    assert not returned, "deployment lock checked in more than once without checkout"
                    returned.append(observed.copy())
                    return
            lease = leases.pop(id(record))
            lease.update(end=loop.time())
            evidence["leases"].append(lease)

        for target, name, callback in ((app.state.db.engine.sync_engine, "begin", on_begin),
            (app.state.db.engine.sync_engine.pool, "checkout", on_checkout),
            (app.state.db.engine.sync_engine.pool, "checkin", on_checkin)):
            event.listen(target, name, callback)
            listeners.append((target, name, callback))
        control = await asyncpg.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, database=TEST_DB)
        assert await control.fetchval("SELECT current_database()") == TEST_DB
        async with AsyncExitStack() as stack:
            actors = []
            for index in range(20):
                http = await stack.enter_async_context(httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app, client=(f"192.0.2.{index + 1}", 51000 + index)),
                    base_url="http://testserver"))
                clients.append(http)
                email, password = f"cap12-{index}@example.invalid", "Cap12Fixture123!"
                response = await http.post("/api/auth/register", json={"email": email, "password": password})
                assert response.status_code == 201, safe_status(response)
                response = await http.post("/api/auth/login", json={"email": email, "password": password,
                    "client_type": "desktop", "device": {"installation_id": str(uuid.uuid4()), "name": "CAP12 fixture",
                    "platform": "macos", "client_version": "0.0.1"}})
                assert response.status_code == 200, safe_status(response)
                actors.append((response.json()["user"]["id"], {"Authorization": "Bearer " + response.json()["access_token"]}))

            async def connect(index):
                response = await create_session(clients[index], actors[index][1])
                assert response.status_code == 201, safe_status(response)
                peer = PcmPeer(app, actors[index][1], response.json()["session_id"], index)
                peers.append(peer)
                await peer.start()  # Immediately after 201; not after the twenty-request barrier.

            connects = [asyncio.create_task(connect(index)) for index in range(20)]
            tasks.extend(connects)
            async with asyncio.timeout(8):
                await asyncio.gather(*connects)
            peers.sort(key=lambda peer: peer.index)
            runtimes = [manager.runtimes[peer.sid] for peer in peers]
            asr_sessions = [runtime.asr for runtime in runtimes]
            assert all(runtime.state == "streaming" and runtime.connected for runtime in runtimes)
            assert all(runtime.events.maxsize == 256 for runtime in runtimes)
            assert all(session.queue.maxsize == 8 for session in asr_sessions)
            assert await control.fetchval("SELECT count(*) FROM sessions WHERE state='streaming'") == 20
            original_process = worker.process
            assert original_process is not None and original_process.is_alive()
            original_pid, original_popen = original_process.pid, original_process._popen
            retained[original_pid] = (original_process, original_popen)
            blocking_job = None
            actual_worker_decode = worker.decode

            async def observed_worker_decode(value, deadline):
                nonlocal blocking_job
                # Capture synchronously before the original physical call. A
                # separate monitor tick may miss a short dispatch window.
                key = (actors[0][0], peers[0].sid, 1)
                current_job = engine.scheduler.running.get(key)
                if (blocking_job is None and current_job is not None
                        and current_job.owner == key
                        and current_job.task is asyncio.current_task()):
                    blocking_job = current_job
                    evidence["first_job"] = {
                        "deadline": current_job.deadline,
                        "wait_deadline": current_job.wait_deadline,
                        "physical_deadline": deadline,
                        "priority": current_job.priority,
                        "capture_source": "original_worker_decode_entry",
                        "current_task_matches_original_job": True,
                        "captured_loop": loop.time(),
                    }
                return await actual_worker_decode(value, deadline)

            monkeypatch.setattr(worker, "decode", observed_worker_decode)
            max_events = [0] * 20
            max_partials = [0] * 20
            evidence.update(event_loop_max_lag_ms=0, pg_idle_max=0, pg_idle_age_max_ms=0)

            async def observe():
                previous, last_tick, next_pg = None, loop.time(), loop.time()
                while not monitor_stop.is_set():
                    now = loop.time()
                    evidence["event_loop_max_lag_ms"] = max(evidence["event_loop_max_lag_ms"], max(0, now-last_tick-.01)*1000)
                    last_tick = now
                    process = worker.process
                    if process is not None:
                        assert process._parent_pid == os.getpid()
                        retained.setdefault(process.pid, (process, process._popen))
                    key = (actors[0][0], peers[0].sid, 1)
                    old_alive = process_alive(original_process, original_popen)
                    state = (old_alive, bool(blocking_job and engine.scheduler.running.get(key) is blocking_job),
                             bool(blocking_job and blocking_job.done.is_set()), process.pid if process else None)
                    if state != previous:
                        evidence["worker_states"].append({"loop": now, "old_alive": state[0], "original_job_running": state[1],
                            "original_job_done": state[2], "current_pid": state[3]})
                        previous = state
                    if blocking_job and old_alive and (not state[1] or state[2]):
                        evidence["issues"].append("permit released before original worker exit")
                    if old_alive and state[3] not in (None, original_pid):
                        evidence["issues"].append("repair overlapped original worker")
                    for index, runtime in enumerate(runtimes):
                        max_events[index] = max(max_events[index], runtime.events.qsize())
                        max_partials[index] = max(max_partials[index], asr_sessions[index].queue.qsize())
                    if now >= next_pg:
                        row = await control.fetchrow("""SELECT count(*) AS n,
                            coalesce(max(extract(epoch FROM (clock_timestamp()-xact_start))*1000),0) AS age
                            FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()
                            AND state='idle in transaction'""")
                        evidence["pg_idle_max"] = max(evidence["pg_idle_max"], row["n"])
                        evidence["pg_idle_age_max_ms"] = max(evidence["pg_idle_age_max_ms"], float(row["age"]))
                        next_pg = now + .1
                    await asyncio.sleep(.01)

            monitor = asyncio.create_task(observe())
            processing_started = loop.time() + .01
            for peer in peers:
                peer.writer = asyncio.create_task(peer.stream(processing_started))
            await until(lambda: any(row["event"] == "three_second_block_enter" for row in child_events(folder, owner)), 5)
            block = next(row for row in child_events(folder, owner) if row["event"] == "three_second_block_enter")
            assert block["pid"] == original_pid and block["configured_seconds"] == 3
            assert blocking_job is not None and evidence["first_job"]["priority"] == "low"
            assert evidence["first_job"]["deadline"] == evidence["first_job"]["wait_deadline"]
            assert evidence["first_job"]["deadline"] - evidence["first_job"]["physical_deadline"] == pytest.approx(1)
            # Observe at least the complete unchanged decode budget while 19 senders continue.
            await asyncio.sleep(3)
            await until(lambda: not process_alive(original_process, original_popen) and worker.process is not None
                        and worker.process.pid != original_pid, 3)
            slow = peers[19]
            async with asyncio.timeout(3):
                await slow.reader
                await slow.writer
            assert slow.gate_pending.is_set() and slow.gate_cancelled is not None and not slow.gate.is_set()
            assert slow.close_code == 1011 and slow.code == "CLIENT_BACKPRESSURE" and slow.result is None
            assert slow.samples <= 32000 and slow.acked == 0 and not slow.end_acked
            assert all(runtime.state == "streaming" for runtime in runtimes[:19])
            assert all(peer.acks and peer.samples >= 48000 for peer in peers[:19])
            for peer in peers[:19]:
                peer.finish_requested.set()
            async with asyncio.timeout(33):
                await asyncio.gather(*(peer.writer for peer in peers[:19]))
                await asyncio.gather(*(peer.reader for peer in peers[:19]))
            for peer in peers:
                response = await clients[peer.index].get(f"/api/v1/sessions/{peer.sid}", headers=peer.headers)
                assert response.status_code == 200, safe_status(response)
                view = response.json()
                assert view["session_id"] == peer.sid and view["current_attempt_no"] == 1
                if peer.index == 19:
                    assert view["state"] == "failed" and view["result"] is None and not view["audio_complete"]
                    assert view["error_code"] == "CLIENT_BACKPRESSURE"
                else:
                    assert view["state"] == "ready" and view["audio_complete"] and view["received_samples"] == peer.samples
                    assert peer.close_code == 1000 and peer.acked == peer.samples and peer.code is None
                    left, right = dict(view["result"]), dict(peer.result)
                    for result in (left, right):
                        result["created_at"] = datetime.fromisoformat(result["created_at"].replace("Z", "+00:00"))
                    assert left == right and left["stop_to_result_ms"] <= 30000
            async with app.state.db.session_factory() as db:
                rows = (await db.scalars(select(ModelCall).where(ModelCall.session_id.in_([peer.sid for peer in peers])))).all()
                timed_out = [row for row in rows if row.session_id == peers[0].sid and row.purpose == "asr_decode" and row.error_code == "ASR_TIMEOUT"]
                assert len(timed_out) == 1 and timed_out[0].status == "failed"
                assert timed_out[0].ended_at is not None and timed_out[0].aggregated_at is not None
                assert timed_out[0].prompt_tokens is timed_out[0].completion_tokens is None
                assert all(row.status != "running" and row.ended_at is not None and row.aggregated_at is not None for row in rows)
                evidence["ledger"] = [{"session_id": row.session_id, "purpose": row.purpose, "status": row.status,
                    "error": row.error_code, "samples": row.processed_audio_samples} for row in rows]
            evidence.update(child_events=child_events(folder, owner), runtime_queue_peaks=max_events, partial_queue_peaks=max_partials)
            assert not any(row["event"] == "three_second_block_return" for row in evidence["child_events"])
            assert not evidence["issues"] and all(value <= 256 for value in max_events) and all(value <= 8 for value in max_partials)
            for row in evidence["frames"]:
                checkpoint = row["saved_after"] != row["saved_before"]
                assert row["begins"] == int(checkpoint), {"seq": row["seq"], "begins": row["begins"], "checkpoint": checkpoint}
                if checkpoint and row["seq"] != 0:
                    assert row["saved_after"] - row["saved_before"] >= 1
            assert any(row["begins"] == 0 for row in evidence["frames"])
            # No connection lease may be held across the actual blocked call.
            # Independent auth/ledger transactions are allowed, observed by task.
            alive_states = [row for row in evidence["worker_states"] if row["old_alive"] and row["original_job_running"]]
            dead_states = [row for row in evidence["worker_states"] if not row["old_alive"]]
            assert alive_states and dead_states
            began, exited = alive_states[0]["loop"], dead_states[0]["loop"]
            assert not any(lease["start"] <= began and lease["end"] >= exited for lease in evidence["leases"])
            assert not any(runtime.audio or runtime.asr is not None for runtime in runtimes)
            assert not engine.scheduler.running and all(not queue for queue in engine.scheduler.queues.values())
    except BaseException as exc:
        evidence["primary_exception"] = safe_exception_locations(exc)
        record_property("cap12_primary_exception", json.dumps(evidence["primary_exception"], sort_keys=True))
        raise
    else:
        evidence["primary_exception"] = None
    finally:
        monitor_stop.set()
        for peer in peers:
            peer.gate.set()  # Failure cleanup only; never released on passing path.
            peer.finish_requested.set()
        stop.set()  # Production normal lifespan owns the real worker shutdown.
        owned = [parent] + tasks + ([monitor] if monitor is not None else []) + [
            task for peer in peers for task in (peer.writer, peer.reader, peer.app_task) if task]
        pending = [task for task in owned if not task.done()]
        if pending:
            _, pending_set = await asyncio.wait(pending, timeout=36)
            for task in pending_set:
                task.cancel()
            if pending_set:
                _, remaining = await asyncio.wait(pending_set, timeout=4)
                evidence["cleanup_remaining"] = len(remaining)
        evidence["task_errors"] = [type(task.exception()).__name__ for task in owned
            if task.done() and not task.cancelled() and task.exception() is not None]
        labels = {parent: "lifespan", **{task: f"connect[{index}]" for index, task in enumerate(tasks)}}
        if monitor is not None:
            labels[monitor] = "monitor"
        for peer in peers:
            for role, task in (("writer", peer.writer), ("reader", peer.reader), ("asgi", peer.app_task)):
                if task is not None:
                    labels[task] = f"peer[{peer.index}].{role}"
        evidence["task_error_details"] = [
            {"owner": labels.get(task, "owned"), "exception": safe_exception_locations(task.exception())}
            for task in owned if task.done() and not task.cancelled() and task.exception() is not None]
        record_property("cap12_exception_diagnostics", json.dumps({
            "primary_exception": evidence.get("primary_exception"),
            "task_error_details": evidence["task_error_details"],
            "deployment_lock_record_id": evidence.get("deployment_lock_record_id"),
            "unmatched_checkins": evidence.get("unmatched_checkins", []),
            "preexisting_deployment_lock_checkins": evidence.get("preexisting_deployment_lock_checkins", []),
        }, sort_keys=True))
        evidence["peers"] = [{"index": peer.index, "session_id": peer.sid, "samples": peer.samples,
            "acked": peer.acked, "max_credit": peer.max_credit, "ack_max_ms": max((a["latency_ms"] for a in peer.acks), default=None),
            "ack_count": len(peer.acks), "in_peak": peer.in_peak, "out_peak": peer.out_peak,
            "close_code": peer.close_code, "code": peer.code, "gate_cancelled": peer.gate_cancelled is not None,
            "gate_pending_ms": (peer.gate_cancelled-peer.gate_started)*1000 if peer.gate_cancelled else None,
            "types": peer.types} for peer in peers]
        evidence["cleanup"] = {"tasks_done": all(task.done() for task in owned), "parent_cancelled": parent.cancelled(),
            "live_processes": [pid for pid, (process, popen) in retained.items() if process_alive(process, popen)],
            "pool_leases_remaining": len(leases)}
        for target, name, callback in listeners:
            event.remove(target, name, callback)
        if control is not None:
            await control.close()
        record_property("cap12_observation", json.dumps(evidence, sort_keys=True))
        assert not evidence.get("cleanup_remaining"), "owned CAP12 cleanup did not finish"
        assert not evidence["task_errors"] and evidence["cleanup"]["tasks_done"]
        assert not evidence["cleanup"]["live_processes"] and not leases
        assert len(evidence.get("preexisting_deployment_lock_checkins", [])) <= 1
        assert evidence.get("pool_before_dispose") == 0 and evidence.get("db_disposed") is True
