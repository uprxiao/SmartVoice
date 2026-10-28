"""HIST10 backend parameter: real 120 s user wait, real PG/API, stub inference.

Opt in explicitly; no production clock/config/DB timestamp is altered. This is
not Desktop-click, microphone, real-ASR or real-LLM quality evidence.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import struct
import time
import uuid

import httpx
import pytest
from sqlalchemy import select

from app.models import SessionAttempt, SessionContribution, SessionResult, UsageDaily, VoiceSession
from app.providers.asr.base import ProviderError
from app.providers.llm.base import LlmResponse
from app.services.inference_context import current_scope
from tests.conftest import auth_headers, register_and_login

RAW = "嗯那个，保留接口文档，不要删除日志。"
REFINED = "保留接口文档，不要删除日志。"
WAIT_SECONDS = 120.0


def _sources():
    root = Path(__file__).resolve().parents[2]
    paths = sorted(path for folder in ("app", "tests", "migrations")
                   for path in (root / folder).rglob("*.py"))
    paths += [root / name for name in ("config/app.yaml", "pyproject.toml", "alembic.ini")]
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths if path.is_file()}


async def _rows(app, user_id, session_id):
    # Only short read transactions; the caller receives detached dictionaries.
    async with app.state.db.session_factory() as db:
        result = {}
        for name, model in (("session", VoiceSession), ("attempts", SessionAttempt),
                            ("results", SessionResult), ("contributions", SessionContribution)):
            table = model.__table__
            condition = table.c.id == session_id if model is VoiceSession else table.c.session_id == session_id
            result[name] = [dict(row) for row in (await db.execute(
                select(table).where(table.c.user_id == user_id, condition))).mappings()]
        result["daily"] = [dict(row) for row in (await db.execute(
            select(UsageDaily.__table__).where(UsageDaily.user_id == user_id))).mappings()]
    result["attempts"].sort(key=lambda row: row["attempt_no"])
    result["results"].sort(key=lambda row: row["attempt_no"])
    return result


async def _initial_ws(app, headers, session_id):
    incoming, outgoing = asyncio.Queue(maxsize=2), asyncio.Queue(maxsize=8)
    scope = {"type": "websocket", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "scheme": "ws", "root_path": "",
        "path": f"/ws/v1/sessions/{session_id}/audio",
        "raw_path": f"/ws/v1/sessions/{session_id}/audio".encode(), "query_string": b"",
        "headers": [(b"host", b"testserver"), (b"authorization", headers["Authorization"].encode())],
        "client": ("127.0.0.1", 50000), "server": ("testserver", 80), "state": {},
        "subprotocols": [], "extensions": {"websocket.http.response": {}}}
    task = asyncio.create_task(app(scope, incoming.get, outgoing.put))

    async def send(message):
        async with asyncio.timeout(2):
            await incoming.put(message)

    async def receive(timeout=2):
        async with asyncio.timeout(timeout):
            return await outgoing.get()

    async def event(timeout=2):
        message = await receive(timeout)
        assert message["type"] == "websocket.send", {"unexpected_type": message["type"]}
        return json.loads(message["text"])

    try:
        await send({"type": "websocket.connect"})
        assert (await receive())["type"] == "websocket.accept"
        ready = await event()
        assert (ready["type"], ready["session_id"], ready["attempt_no"]) == ("ready", session_id, 1)
        for seq in range(2):
            await send({"type": "websocket.receive", "bytes": struct.pack("<II", seq, 4000) + b"\x01\x00" * 4000})
            ack = await event()
            assert (ack["type"], ack["seq"], ack["total_samples"]) == ("audio.ack", seq, (seq + 1) * 4000)
            assert ack["session_id"] == session_id
            if seq == 0:
                await asyncio.sleep(.25)
        await send({"type": "websocket.receive", "text": json.dumps(
            {"type": "audio.end", "last_seq": 1, "total_samples": 8000})})
        seen = []
        async with asyncio.timeout(32):  # Transport anti-hang; does not change the 30 s business deadline.
            while True:
                item = await event(32)
                assert item["session_id"] == session_id and item["attempt_no"] == 1
                seen.append(item["type"])
                assert item["type"] in {"audio.end_ack", "asr.final", "result.ready"}, item["type"]
                if item["type"] == "asr.final":
                    assert item["text"] == RAW
                if item["type"] == "result.ready":
                    assert seen == ["audio.end_ack", "asr.final", "result.ready"]
                    break
        close = await receive()
        assert close["type"] == "websocket.close" and close["code"] == 1000
        async with asyncio.timeout(3):
            await task
        return item["result"]
    finally:
        if not task.done():
            task.cancel()
        async with asyncio.timeout(3):
            await asyncio.gather(task, return_exceptions=True)


def test_hist10_refine_retry_after_real_120_seconds(app_client, monkeypatch):
    if os.environ.get("SVI_RUN_HIST10_REAL120") != "1":
        pytest.fail("explicit SVI_RUN_HIST10_REAL120=1 required; this node waits a real 120 seconds")
    evidence_dir = os.environ.get("SVI_HIST10_EVIDENCE_DIR")
    assert evidence_dir, "SVI_HIST10_EVIDENCE_DIR is required"
    destination = Path(evidence_dir)
    destination.mkdir(parents=True, exist_ok=True)
    evidence_file = destination / f"hist10-real120-{uuid.uuid4().hex}.json"
    report = {"status": "started", "scope": "backend real PG/API/WS; stub inference only",
              "wait_seconds_required": WAIT_SECONDS, "sources_before": _sources()}
    tokens = register_and_login(app_client, email="hist10-real120@example.com")
    headers = auth_headers(tokens)
    app = app_client.app

    async def exercise():
        manager = app.state.sessions
        asr = manager.asr_provider
        inner = manager.refiner.llm.inner
        assert asr.model_id == "mock-asr" and inner.model_id == "mock-llm"
        assert manager.config.pipeline.stop_to_result_budget_ms == 30000
        assert manager.config.retry.audio_ttl_seconds == 600
        entered, release = asyncio.Event(), asyncio.Event()
        asr_calls, llm_calls, streams = [], [], []
        original_open, original_generate = asr.open, inner.generate
        gate_started = None

        async def observed_open(options):
            asr_calls.append((options.user_id, options.session_id, options.attempt_no, "open"))
            stream = await original_open(options)
            stream.text = RAW  # Explicit physical Mock fixture output, not injected durable state.
            streams.append(stream)
            original_finish = stream.finish

            async def finish(total_samples, deadline):
                asr_calls.append((options.user_id, options.session_id, options.attempt_no, "finish"))
                return await original_finish(total_samples, deadline)

            monkeypatch.setattr(stream, "finish", finish)
            return stream

        async def controlled_generate(request, deadline):
            nonlocal gate_started
            scope = current_scope.get()
            assert scope is not None
            llm_calls.append((scope.user_id, scope.session_id, scope.attempt_no, request.purpose))
            if scope.attempt_no == 1:
                assert request.purpose == "refine" and request.input["transcript"] == RAW
                return LlmResponse(parsed=None, provider=inner.provider_id, model_id=inner.model_id,
                    finish_reason="error", error=ProviderError("unavailable", "synthetic controlled unavailability"))
            assert scope.attempt_no == 2
            if request.purpose == "refine":
                assert request.input["transcript"] == RAW
                gate_started = time.monotonic()
                entered.set()
                await release.wait()  # Remains inside production ScheduledLlmProvider's original 10 s call deadline.
            return await original_generate(request, deadline)

        monkeypatch.setattr(asr, "open", observed_open)
        monkeypatch.setattr(inner, "generate", controlled_generate)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                                     headers=headers) as http:
            me = await http.get("/api/users/me")
            assert me.status_code == 200
            user_id = me.json()["user"]["id"]
            request_id = str(uuid.uuid4())
            created = await http.post("/api/v1/sessions", headers={"Idempotency-Key": request_id},
                                     json={"client_request_id": request_id, "learning_enabled": True})
            assert created.status_code == 201
            sid = created.json()["session_id"]
            old_runtime = manager.runtimes[sid]
            old_audio = old_runtime.audio
            initial = await _initial_ws(app, headers, sid)
            assert initial["attempt_no"] == 1 and initial["attempt_kind"] == "initial"
            assert initial["raw_text"] == initial["refined_text"] == RAW
            assert initial["review_required"] is True and initial["review_reason"] == "llm_unavailable"
            assert old_audio == old_runtime.audio == b"" and streams[0].cancelled
            assert old_runtime.retry_expires_at is None
            before = await _rows(app, user_id, sid)
            assert len(before["session"]) == len(before["attempts"]) == len(before["results"]) == 1
            baseline_session = before["session"][0]
            assert baseline_session["current_result_id"] == initial["result_id"]
            assert before["attempts"][0]["raw_text"] == RAW
            assert before["results"][0]["raw_text"] == RAW
            assert len(before["contributions"]) == len(before["daily"]) == 1
            assert before["contributions"][0]["fallback"] is True
            assert before["daily"][0]["sessions"] == before["daily"][0]["result_sessions"] == 1
            assert before["daily"][0]["fallback_sessions"] == 1
            assert before["daily"][0]["received_audio_samples"] == 8000
            initial_asr_calls, initial_llm_calls = list(asr_calls), list(llm_calls)
            assert initial_asr_calls == [(user_id, sid, 1, "open"), (user_id, sid, 1, "finish")]
            assert initial_llm_calls == [(user_id, sid, 1, "refine")]
            report["identity"] = {"user_id": user_id, "session_id": sid,
                "device_id": baseline_session["device_id"], "first_result_id": initial["result_id"]}
            report["first_terminal_at"] = baseline_session["first_terminal_at"].isoformat()

            # The snapshot session has exited. No DB connection/transaction is
            # retained during this actual user wait; no heartbeat request needed.
            waiting = time.monotonic()
            waiting_epoch = time.time()
            checkpoints = []
            while time.monotonic() - waiting < WAIT_SECONDS:
                await asyncio.sleep(min(10, WAIT_SECONDS - (time.monotonic() - waiting)))
                checkpoints.append({"monotonic_elapsed_s": time.monotonic() - waiting,
                                    "epoch_elapsed_s": time.time() - waiting_epoch,
                                    "pcm_bytes": len(old_audio)})
                assert old_audio == old_runtime.audio == b""
                assert asr_calls == initial_asr_calls and llm_calls == initial_llm_calls
            report["wait"] = {"monotonic_seconds": time.monotonic() - waiting,
                              "epoch_seconds": time.time() - waiting_epoch, "checkpoints": checkpoints}
            assert report["wait"]["monotonic_seconds"] >= 120
            assert await _rows(app, user_id, sid) == before
            available = await http.get(f"/api/v1/sessions/{sid}")
            assert available.status_code == 200 and available.json()["retry_kind"] == "refine"
            for field in ("result_id", "attempt_no", "attempt_kind", "raw_text", "refined_text",
                          "review_required", "review_reason", "text_hash", "dictionary_version", "stop_to_result_ms"):
                assert available.json()["result"][field] == initial[field], field
            retry_key = str(uuid.uuid4())
            retry_start_mono, retry_start_wall = time.monotonic(), datetime.now(timezone.utc)
            request = await http.post(f"/api/v1/sessions/{sid}/retry", headers={"Idempotency-Key": retry_key},
                                      json={"retry_kind": "refine"})
            assert request.status_code == 202
            async with asyncio.timeout(3):
                await entered.wait()
            try:
                active = await http.get(f"/api/v1/sessions/{sid}")
                assert active.status_code == 200
                active_body = active.json()
                assert active_body["session_id"] == sid and active_body["current_attempt_no"] == 2
                assert active_body["state"] == "refining" and active_body["result"] is None
                assert active_body["current_result_id"] is None
                during = await _rows(app, user_id, sid)
                assert during["results"] == before["results"]
                assert during["attempts"][0] == before["attempts"][0]
                assert len(during["attempts"]) == 2
                attempt = during["attempts"][1]
                assert (attempt["user_id"], attempt["session_id"], attempt["kind"], attempt["source_attempt_no"]) == (user_id, sid, "refine", 1)
                assert attempt["state"] == "running" and attempt["ended_at"] is None
                assert during["contributions"][0]["has_result"] is False
                assert during["contributions"][0]["outcome"] == "active"
                retry_runtime = manager.runtimes[sid]
                assert retry_runtime.audio == b"" and retry_runtime.asr is None and retry_runtime.raw_text == RAW
                assert retry_runtime.attempt_no == 2 and retry_runtime.device_id == old_runtime.device_id
                await asyncio.sleep(.25)  # Positive measured work interval; no production deadline extension.
                gate_released = time.monotonic()
            finally:
                release.set()
            observations = []
            async with asyncio.timeout(32):
                while True:
                    response = await http.get(f"/api/v1/sessions/{sid}")
                    assert response.status_code == 200
                    current = response.json()
                    observations.append({"state": current["state"], "attempt_no": current["current_attempt_no"],
                                         "current_result_id": current["current_result_id"]})
                    if current["state"] == "ready":
                        break
                    assert current["state"] == "refining" and current["result"] is None
                    await asyncio.sleep(.25)
            retry_end_mono, retry_end_wall = time.monotonic(), datetime.now(timezone.utc)
            result = current["result"]
            assert result["attempt_no"] == 2 and result["attempt_kind"] == "refine"
            assert result["result_id"] != initial["result_id"] == before["results"][0]["id"]
            assert current["current_result_id"] == result["result_id"]
            assert result["raw_text"] == RAW and result["refined_text"] == REFINED
            assert result["review_required"] is False and result["review_reason"] is None
            assert result["text_hash"] == hashlib.sha256(REFINED.encode()).hexdigest()
            assert result["dictionary_version"] == initial["dictionary_version"] == baseline_session["dictionary_version"]
            after = await _rows(app, user_id, sid)
            assert len(after["results"]) == len(after["attempts"]) == 2
            assert after["session"][0]["current_attempt_no"] == 2
            assert after["session"][0]["current_result_id"] == after["results"][1]["id"] == result["result_id"]
            assert (after["results"][1]["user_id"], after["results"][1]["session_id"], after["results"][1]["attempt_no"]) == (user_id, sid, 2)
            assert after["results"][0] == before["results"][0]
            assert after["attempts"][0] == before["attempts"][0]
            attempt = after["attempts"][1]
            assert attempt["state"] == "ready" and attempt["source_attempt_no"] == 1 and attempt["asr_ms"] == 0
            assert retry_start_wall <= attempt["started_at"] <= attempt["ended_at"] <= retry_end_wall
            measured_ms = (retry_end_mono - retry_start_mono) * 1000
            gate_ms = (gate_released - gate_started) * 1000
            elapsed = result["stop_to_result_ms"]
            assert int(gate_ms) <= elapsed <= measured_ms + 1
            assert 0 <= elapsed < 30000 < report["wait"]["monotonic_seconds"] * 1000
            assert attempt["stop_to_result_ms"] == elapsed
            assert after["results"][1]["stop_to_result_ms"] == elapsed
            for field in ("id", "user_id", "device_id", "created_login_sid", "client_request_id",
                          "request_hash", "created_at", "end_accepted_at", "first_terminal_at",
                          "received_samples", "audio_complete", "dictionary_version"):
                assert after["session"][0][field] == baseline_session[field], field
            assert asr_calls == initial_asr_calls
            assert llm_calls == initial_llm_calls + [(user_id, sid, 2, "refine")]
            assert old_audio == old_runtime.audio == retry_runtime.audio == b""
            contribution, day = after["contributions"][0], after["daily"][0]
            assert len(after["contributions"]) == len(after["daily"]) == 1
            assert contribution["business_date"] == before["contributions"][0]["business_date"] == day["date"]
            assert contribution["counted_session"] is True and contribution["has_result"] is True
            assert contribution["fallback"] is False and contribution["outcome"] == "ready"
            assert contribution["received_samples"] == contribution["successful_audio_samples"] == 8000
            chars = sum(character.isalnum() for character in REFINED)
            assert contribution["char_count"] == day["chars"] == chars
            assert day["sessions"] == day["result_sessions"] == 1
            assert day["received_audio_samples"] == day["successful_audio_samples"] == 8000
            assert day["fallback_sessions"] == day["failed_sessions"] == day["inserted_sessions"] == 0
            day_string = day["date"].isoformat()
            stats = await http.get("/api/v1/stats/me", params={"date_from": day_string, "date_to": day_string})
            assert stats.status_code == 200
            totals = stats.json()["totals"]
            assert totals["sessions"] == totals["result_sessions"] == 1
            assert totals["chars"] == chars and totals["received_audio_ms"] == 500
            calls_before_replay = list(llm_calls)
            replay = await http.post(f"/api/v1/sessions/{sid}/retry", headers={"Idempotency-Key": retry_key},
                                     json={"retry_kind": "refine"})
            assert replay.status_code == 200 and replay.json()["replayed"] is True
            assert replay.json()["current_attempt_no"] == 2 and replay.json()["result"]["result_id"] == result["result_id"]
            assert await _rows(app, user_id, sid) == after
            assert llm_calls == calls_before_replay and asr_calls == initial_asr_calls
            report.update(status="passed", new_result_id=result["result_id"],
                measured_retry_ms=measured_ms, provider_gate_ms=gate_ms, reported_processing_ms=elapsed,
                attempt_started_at=attempt["started_at"].isoformat(), attempt_ended_at=attempt["ended_at"].isoformat(),
                db_attempt_elapsed_ms=(attempt["ended_at"] - attempt["started_at"]).total_seconds() * 1000,
                observations=observations, statistics={key: day[key] for key in (
                    "sessions", "result_sessions", "chars", "received_audio_samples", "fallback_sessions")},
                asr_invocations=asr_calls, llm_invocations=llm_calls,
                note="All inference invocations are explicit mocks; no physical-model usage claim.")

    async def bounded():
        async with asyncio.timeout(180):  # Overall harness guard includes the real wait; product budgets unchanged.
            await exercise()

    try:
        app_client.portal.call(bounded)
        report["sources_after"] = _sources()
        assert report["sources_before"] == report["sources_after"], "source drift invalidates this run"
    except BaseException as exc:
        report["status"] = "failed"
        report["failure_type"] = type(exc).__name__  # No exception body, headers, credentials or DSN.
        raise
    finally:
        report.setdefault("sources_after", _sources())
        with evidence_file.open("x", encoding="utf-8") as output:
            json.dump(report, output, ensure_ascii=False, indent=2)
            output.write("\n")
