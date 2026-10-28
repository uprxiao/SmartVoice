"""CAP14/NFR09: real TCP LLM adapters, scheduler and PG recorder.

Only the physical upstream HTTP service is scripted. Configured Mock ASR emits
its existing synthetic transcript; this is not microphone/model-quality or
ASR-performance evidence. Budgets, production methods and clocks are unchanged.
"""
from __future__ import annotations

import asyncio
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from types import SimpleNamespace
import uuid

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select
import yaml

from app.models import ModelCall, ModelUsageDaily
from app.services.inference_scheduler import SchedulerBusy
from tests.conftest import SERVER_ROOT, TEST_URL, auth_headers, register_and_login, wait_for_readiness
from tests.integration.test_capacity_normal_completion import AsgiPcmStream
from tests.test_insertions_learning import claim, receipt
from tests.test_sessions import create_session, frame


RAW = "嗯那个，帮我加一个按钮，不要修改数据库。"
CLEAN = "帮我加一个按钮，不要修改数据库。"
MODEL = "svi-isolated-http-fixture"
GOOD_REFINE = {"edits": [
    {"kind": "filler", "before": "嗯那个，", "after": ""},
]}
PASS_AUDIT = {"decision": "pass", "violations": []}
REJECT_AUDIT = {"decision": "reject", "violations": [
    {"category": "fact", "source_start": 4, "source_end": 5,
     "reason_code": "FIXTURE_REJECT"},
]}


@dataclass
class Reply:
    purpose: str
    document: dict | None = None
    http_status: int = 200
    finish: str = "stop"
    malformed: bool = False
    retry_after: str | None = None
    delay: float = 0
    hold: bool = False
    expect_timeout: bool = False
    expected_status: str = "succeeded"
    expected_error: str | None = None
    release: threading.Event = field(default_factory=threading.Event)


def refine(**kwargs):
    return Reply("refine", GOOD_REFINE, **kwargs)


def audit(**kwargs):
    return Reply("audit", PASS_AUDIT, **kwargs)


class Upstream:
    """Bounded, test-owned loopback server; never logs headers or input text."""

    def __init__(self, adapter):
        self.adapter = adapter
        self.pending, self.records, self.errors = deque(), [], []
        self.all_replies, self.handlers = [], []
        self.lock = threading.Lock()
        self.on_dispatch = None
        self.generation_reply = {"done": True, "done_reason": "stop", "response": '{"ready":true}'}
        self.generation_requests = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def emit(self, status, body, headers=None, *, response_record=None):
                payload = json.dumps(body).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("Connection", "close")
                    for name, value in (headers or {}).items():
                        self.send_header(name, value)
                    # Timestamp immediately before end_headers flushes the
                    # buffered HTTP headers. This is a conservative wire-send
                    # lower bound, using the SAME monotonic source as incoming
                    # request timestamps in this test-owned HTTP server.
                    if response_record is not None:
                        response_record["retry_after_sent"] = (headers or {}).get("Retry-After")
                        response_record["response_headers_send_started_mono"] = time.monotonic()
                    self.end_headers()
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    # Expected when the REAL adapter has cancelled its socket.
                    pass
                self.close_connection = True

            def do_GET(self):
                if self.path == "/api/tags":
                    return self.emit(200, {"models": [{"name": MODEL}]})
                if self.path == "/models":
                    return self.emit(200, {"data": [{"id": MODEL}]})
                fixture.errors.append("unexpected_GET_path")
                self.emit(404, {})

            def do_POST(self):
                done = threading.Event()
                fixture.handlers.append(done)
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    assert 0 < length <= 131072
                    body = json.loads(self.rfile.read(length))
                    if self.path == "/api/show":
                        return self.emit(200, {"capabilities": []})
                    if self.path == "/api/generate":
                        assert "messages" not in body  # Startup synthetic warmup only.
                        assert body["prompt"].startswith("svi-llm-health-v1 synthetic:")
                        assert body["options"]["num_predict"] == 32
                        fixture.generation_requests.append(body)
                        return self.emit(200, fixture.generation_reply)
                    assert self.path == ("/api/chat" if fixture.adapter == "ollama" else "/chat/completions")
                    data = json.loads(body["messages"][1]["content"])
                    purpose = "refine" if "transcript" in data else "audit" if "candidate" in data else "classify"
                    assert body["model"] == MODEL
                    if fixture.adapter == "ollama":
                        assert body["stream"] is False and isinstance(body["format"], dict)
                    else:
                        assert body["response_format"]["type"] == "json_schema"
                        assert body["response_format"]["json_schema"]["name"] == purpose
                    wire_schema = (body["format"] if fixture.adapter == "ollama" else
                                   body["response_format"]["json_schema"]["schema"])
                    if purpose == "refine":
                        assert set(wire_schema["properties"]) == {"edits", "candidate_text"}
                        assert wire_schema["required"] == ["edits"]
                        assert wire_schema["additionalProperties"] is False
                        assert len(wire_schema["properties"]["edits"]["items"]["oneOf"]) == 5
                    with fixture.lock:
                        assert fixture.pending, "unexpected physical request"
                        spec = fixture.pending.popleft()
                        assert spec.purpose == purpose
                        n = len(fixture.records) + 1
                        record = {"n": n, "purpose": purpose, "spec": spec,
                                  "received_mono": time.monotonic(), "response_mono": None,
                                  "prompt_tokens": 10 + n, "completion_tokens": 2 + n}
                        fixture.records.append(record)
                    captured = threading.Event()
                    assert fixture.on_dispatch is not None
                    fixture.on_dispatch(record, captured)
                    assert captured.wait(1), "read-only deadline probe did not run"
                    if spec.hold:
                        assert spec.release.wait(35), "test fixture HTTP hold guard expired"
                    elif spec.delay:
                        spec.release.wait(spec.delay)
                    record["response_mono"] = time.monotonic()
                    if spec.http_status != 200:
                        headers = {"Retry-After": spec.retry_after} if spec.retry_after else {}
                        return self.emit(spec.http_status, {"error": "synthetic_upstream_failure"},
                                         headers, response_record=record)
                    content = "{" if spec.malformed else json.dumps(spec.document, ensure_ascii=False)
                    if fixture.adapter == "ollama":
                        result = {"message": {"content": content}, "done_reason": spec.finish,
                                  "prompt_eval_count": record["prompt_tokens"], "eval_count": record["completion_tokens"]}
                    else:
                        result = {"choices": [{"finish_reason": spec.finish, "message": {"content": content}}],
                                  "usage": {"prompt_tokens": record["prompt_tokens"], "completion_tokens": record["completion_tokens"]}}
                    self.emit(200, result)
                except Exception as exc:
                    fixture.errors.append(type(exc).__name__)  # Never exception message/body.
                    self.emit(500, {"error": "fixture_contract_error"})
                finally:
                    done.set()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def start(self):
        self.thread.start()

    def install(self, *replies):
        with self.lock:
            assert not self.pending
            self.pending.extend(replies)
            self.all_replies.extend(replies)

    def release_all(self):
        for spec in self.all_replies:
            spec.release.set()

    def close(self):
        self.release_all()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        assert not self.thread.is_alive()
        assert all(done.wait(2) for done in self.handlers), "owned HTTP handler did not exit"


class DeadlineProbe:
    """Read only at physical HTTP dispatch; no replacement production calls."""

    def __init__(self, app):
        self.app, self.loop = app, asyncio.get_running_loop()
        self.errors, self.handles = [], []

    def dispatch(self, record, captured):
        self.loop.call_soon_threadsafe(self.capture, record, captured)

    def capture(self, record, captured):
        try:
            jobs = list(self.app.state.llm.scheduler.running.values())
            assert len(jobs) == 1
            job = jobs[0]
            frame_ = job.task.get_coro().cr_frame
            assert frame_ is not None and frame_.f_code.co_name == "perform"
            data = frame_.f_locals
            scope = data["scope"]
            assert data["request"].purpose == record["purpose"]
            trace = {"call_id": data["call_id"], "user_id": scope.user_id,
                     "session_id": scope.session_id, "learning_event_id": scope.learning_event_id,
                     "started": data["started"], "call_deadline": data["call_deadline"],
                     "deadline": job.deadline, "wait_deadline": job.wait_deadline,
                     "received_loop": self.loop.time()}
            assert trace["call_id"]
            assert trace["call_deadline"] <= trace["started"] + 10
            assert trace["call_deadline"] <= trace["deadline"]
            if scope.learning_event_id is None:
                runtime = self.app.state.sessions.runtimes[scope.session_id]
                assert trace["deadline"] == runtime.deadline
                assert 0 < trace["deadline"] - trace["started"] <= 30
            else:
                assert 0 < trace["deadline"] - trace["started"] <= 10
            record["trace"] = trace
            if record["spec"].expect_timeout:
                def before():
                    try:
                        assert record["response_mono"] is None
                        assert job.task is not None and not job.task.done()
                        trace["pending_before_deadline"] = True
                    except Exception as exc:
                        self.errors.append(exc)

                def due():
                    trace["deadline_timer_lateness_ms"] = (self.loop.time() - trace["call_deadline"]) * 1000

                self.handles.append(self.loop.call_at(trace["call_deadline"] - .05, before))
                self.handles.append(self.loop.call_at(trace["call_deadline"], due))
        except Exception as exc:
            self.errors.append(exc)
        finally:
            captured.set()

    def close(self):
        for handle in self.handles:
            handle.cancel()


class ProtocolPeer(AsgiPcmStream):
    """Reuse bounded ASGI transport; accept genuine fallback result too."""

    async def _run(self):
        await self._send({"type": "websocket.connect"})
        assert (await self._message())["type"] == "websocket.accept"
        ready = await self._event()
        assert ready["type"] == "ready" and ready["session_id"] == self.session_id
        await self._send({"type": "websocket.receive", "bytes": frame(0)})
        ack = await self._event(timeout=2)
        assert ack["type"] == "audio.ack" and ack["seq"] == 0 and ack["total_samples"] == 4000
        self.started.set()
        await self.finish_requested.wait()
        self.end_clock = asyncio.get_running_loop().time()
        await self._send({"type": "websocket.receive", "text": json.dumps({
            "type": "audio.end", "last_seq": 0, "total_samples": 4000})})
        end_acked, final = False, None
        # Observation guard only; production stays 30 seconds.
        async with asyncio.timeout(35):
            while True:
                event = await self._event(timeout=35)
                assert event["session_id"] == self.session_id
                assert event["attempt_no"] == 1
                if event["type"] == "audio.end_ack":
                    assert not end_acked and event["last_seq"] == 0 and event["total_samples"] == 4000
                    end_acked = True
                elif event["type"] == "asr.final":
                    assert end_acked and final is None and event["text"] == RAW
                    final = event["text"]
                elif event["type"] == "result.ready":
                    assert end_acked and final == RAW
                    self.result = event["result"]
                    self.result_clock = asyncio.get_running_loop().time()
                    assert self.result["raw_text"] == RAW
                    break
                else:
                    raise AssertionError({"unexpected_event": event["type"], "code": event.get("error", {}).get("code")})
        closed = await self._message()
        assert closed["type"] == "websocket.close" and closed["code"] == 1000
        async with asyncio.timeout(3):
            await self.app_task
        return self.result


async def calls(app, user_id, session_id=None):
    async with app.state.db.session_factory() as db:
        query = select(ModelCall).where(ModelCall.user_id == user_id)
        if session_id is not None:
            query = query.where(ModelCall.session_id == session_id)
        rows = (await db.scalars(query.order_by(ModelCall.started_at, ModelCall.id))).all()
        return [{key: getattr(row, key) for key in (
            "id", "user_id", "session_id", "learning_event_id", "attempt_no", "purpose", "provider", "model_id",
            "call_no", "status", "error_code", "prompt_tokens", "completion_tokens", "elapsed_ms",
        )} for row in rows]


async def usage(app, user_id):
    names = ("calls", "successes", "failures", "unknown_calls", "prompt_tokens_known", "completion_tokens_known",
             "prompt_tokens_unknown_calls", "completion_tokens_unknown_calls")
    async with app.state.db.session_factory() as db:
        rows = (await db.scalars(select(ModelUsageDaily).where(ModelUsageDaily.user_id == user_id))).all()
        return {row.purpose: {name: getattr(row, name) for name in names} for row in rows}


class Harness:
    def __init__(self, client, upstream, probe):
        self.client, self.app, self.upstream, self.probe = client, client.app, upstream, probe
        self.peers, self.queue_probes = [], []

    def actor(self, suffix):
        return register_and_login(self.client, email=f"http-{suffix}@example.invalid")

    def start(self, actor):
        headers = auth_headers(actor)
        created = create_session(self.client, headers, str(uuid.uuid4()))
        assert created.status_code == 201
        sid = created.json()["session_id"]
        peer = ProtocolPeer(self.app, SimpleNamespace(headers=lambda: headers), sid, 1, RAW)
        self.peers.append(peer)
        self.client.portal.call(peer.start)
        return peer

    def end(self, peer):
        self.client.portal.call(peer.finish_requested.set)

    def result(self, peer, actor, reason=None):
        async def wait():
            async with asyncio.timeout(35):
                return await asyncio.shield(peer.runner)
        result = self.client.portal.call(wait)
        assert result["refined_text"] == (RAW if reason else CLEAN)
        assert result["review_required"] is (reason is not None)
        assert result["review_reason"] == reason
        response = self.client.get(f"/api/v1/sessions/{peer.session_id}", headers=auth_headers(actor))
        assert response.status_code == 200
        value = response.json()
        assert value["state"] == "ready" and value["audio_complete"] is True
        assert value["current_attempt_no"] == 1
        # HTTP/Pydantic emits UTC as Z; the WS JSON encoder emits +00:00.
        # Compare the same aware instant while retaining every result field.
        http_result, ws_result = dict(value["result"]), dict(result)
        for item in (http_result, ws_result):
            item["created_at"] = datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
            assert item["created_at"].tzinfo is not None
        assert http_result == ws_result
        return result

    def complete(self, actor, reason=None):
        peer = self.start(actor)
        self.end(peer)
        self.result(peer, actor, reason)
        return peer

    def correct(self, actor, peer):
        headers, result = auth_headers(actor), peer.result
        claimed = claim(self.client, headers, peer.session_id, result)
        assert claimed.status_code == 201
        insertion_id = claimed.json()["insertion_id"]
        assert receipt(self.client, headers, peer.session_id, insertion_id, "inserted", True).status_code == 200
        offset, before, after = CLEAN.index("按钮"), "按钮", "按纽"
        submitted = self.client.post("/api/v1/corrections", headers=headers, json={
            "session_id": peer.session_id, "insertion_id": insertion_id, "correction_id": str(uuid.uuid4()),
            "offset": offset, "before": before, "after": after, "left": CLEAN[:offset], "right": CLEAN[offset + len(before):],
        })
        assert submitted.status_code == 202 and submitted.json()["status"] == "pending"
        return submitted.json()["event_id"]

    def failed_event(self, actor, event_id):
        guard = time.monotonic() + 12  # Polling guard; not classification budget.
        reads = 0
        while time.monotonic() < guard:
            response = self.client.get(f"/api/v1/learning-events/{event_id}", headers=auth_headers(actor))
            reads += 1
            assert response.status_code == 200
            event = response.json()
            if event["status"] != "pending":
                assert event["status"] == "failed" and event["reason"] == "MODEL_BUSY"
                terms = self.client.get("/api/v1/dictionary/terms", headers=auth_headers(actor))
                assert terms.status_code == 200 and terms.json()["items"] == []
                return reads
            time.sleep(.2)  # At most60 reads, normal120/min user quota unchanged.
        raise AssertionError("classification observation guard expired")

    def ledger(self, actor, peer=None):
        owner = actor["user"]["id"]
        records = [record for record in self.upstream.records if record["trace"]["user_id"] == owner
                   and (peer is None or record["trace"]["session_id"] == peer.session_id)]
        rows = self.client.portal.call(calls, self.app, owner, peer.session_id if peer else None)
        assert len(rows) == len(records)
        by_id = {row["id"]: row for row in rows}
        numbers, totals = Counter(), {}
        for record in records:
            trace, spec = record["trace"], record["spec"]
            row = by_id[trace["call_id"]]
            assert (row["user_id"], row["session_id"], row["learning_event_id"], row["attempt_no"]) == (
                owner, trace["session_id"], trace["learning_event_id"], 1)
            assert (row["provider"], row["model_id"], row["purpose"]) == (self.upstream.adapter, MODEL, spec.purpose)
            assert (row["status"], row["error_code"]) == (spec.expected_status, spec.expected_error)
            known = spec.http_status == 200 and not spec.expect_timeout
            assert row["prompt_tokens"] == (record["prompt_tokens"] if known else None)
            assert row["completion_tokens"] == (record["completion_tokens"] if known else None)
            number_key = (row["session_id"], row["learning_event_id"], row["purpose"])
            numbers[number_key] += 1
            assert row["call_no"] == numbers[number_key]
            total = totals.setdefault(spec.purpose, Counter())
            total.update(calls=1, successes=int(spec.expected_status == "succeeded"),
                failures=int(spec.expected_status != "succeeded"), unknown_calls=int(spec.expected_status == "unknown"),
                prompt_tokens_known=record["prompt_tokens"] if known else 0,
                completion_tokens_known=record["completion_tokens"] if known else 0,
                prompt_tokens_unknown_calls=int(not known), completion_tokens_unknown_calls=int(not known))
        if peer is None:
            assert self.client.portal.call(usage, self.app, owner) == {purpose: dict(total) for purpose, total in totals.items()}
        return rows

    def evidence(self, record_property):
        assert not self.upstream.pending and not self.upstream.errors
        assert not self.probe.errors
        traces = []
        for record in self.upstream.records:
            trace = record["trace"]
            if record["spec"].expect_timeout:
                assert trace["pending_before_deadline"] is True
                assert "deadline_timer_lateness_ms" in trace
            traces.append({"n": record["n"], "purpose": record["purpose"], **trace})
        record_property("http_adapter_deadlines", json.dumps({"adapter": self.upstream.adapter, "calls": traces}))

    async def close(self):
        self.upstream.release_all()
        running = [peer.runner for peer in self.peers if peer.runner is not None and not peer.runner.done()]
        if running:
            await asyncio.wait(running, timeout=1)
        for peer in self.peers:
            await peer.dispose()
        for probe in self.queue_probes:
            await probe.close()
        self.probe.close()


@pytest.fixture(params=["ollama", "openai_compatible"])
def adapter_client(request, clean_database, monkeypatch, tmp_path):
    upstream = Upstream(request.param)
    upstream.start()
    try:
        monkeypatch.setenv("SVI_DATABASE_URL", TEST_URL)
        monkeypatch.setenv("SVI_INSTANCE_LOCK", "0")
        monkeypatch.setenv("SVI_ENVIRONMENT", "development")
        monkeypatch.setenv("SVI_ASR_PROVIDER", "mock")
        monkeypatch.setenv("SVI_LLM_PROVIDER", request.param)
        monkeypatch.setenv("SVI_LLM_BASE_URL", upstream.url)
        monkeypatch.setenv("SVI_LLM_MODEL_ID", MODEL)
        payload = yaml.safe_load((SERVER_ROOT / "config" / "app.yaml").read_text())
        # This matrix exercises the retained JSON/edits protocol, including its
        # content guards. R2 plain-text/SSE is covered by its own HTTP matrix.
        payload["llm"].update(provider=request.param, model_id=MODEL, base_url=upstream.url,
            api_key_env=None, structured_output="json_schema", max_inflight=1,
            refine_protocol="legacy_edits")
        config_path = tmp_path / "http-adapter.yaml"
        config_path.write_text(yaml.safe_dump(payload))
        monkeypatch.setenv("SVI_CONFIG", str(config_path))
        from app.main import create_app
        app = create_app(str(config_path))
        with TestClient(app) as client:
            wait_for_readiness(client)
            config = app.state.config
            assert (config.asr.finish_timeout_ms, config.llm.call_timeout_ms,
                config.pipeline.stop_to_result_budget_ms, config.pipeline.classification_budget_ms,
                config.llm.foreground_wait_ms, config.llm.classify_wait_ms,
                config.llm.automatic_retries_per_attempt) == (10000, 10000, 30000, 10000, 2000, 1000, 1)
            assert type(app.state.llm).__name__ == "ScheduledLlmProvider"
            assert type(app.state.llm.inner).__name__ == ("OllamaProvider" if request.param == "ollama" else "OpenAICompatibleProvider")
            assert type(app.state.recorder).__name__ == "DatabaseCallRecorder"
            assert app.state.sessions.refiner.llm is app.state.classifier.llm is app.state.llm
            assert app.state.llm.recorder is app.state.recorder
            assert app.state.asr.text == RAW
            health = client.get("/healthz")
            assert health.status_code == 200 and health.json()["ready"] is True
            async def create_probe():
                return DeadlineProbe(app)
            probe = client.portal.call(create_probe)
            upstream.on_dispatch = probe.dispatch
            harness = Harness(client, upstream, probe)
            try:
                yield harness
            finally:
                client.portal.call(harness.close)
    finally:
        upstream.close()


def scenario(name):
    invalid = dict(expected_status="failed", expected_error="invalid_output")
    if name == "503_retry":
        return [Reply("refine", http_status=503, expected_status="failed", expected_error="transport_error"), refine()], None
    if name == "bad_json_retry":
        return [Reply("refine", malformed=True, **invalid), refine()], None
    if name == "shared_retry_spent":
        return [Reply("refine", {}, **invalid), Reply("refine", {}, **invalid)], "llm_invalid_output"
    if name == "schema_retry_available":
        return [Reply("refine", {}, **invalid), refine()], None
    if name == "auth401_no_retry":
        return [Reply("refine", http_status=401, expected_status="failed", expected_error="transport_error")], "llm_unavailable"
    if name == "truncated_no_retry":
        return [refine(finish="length", expected_status="failed", expected_error="truncated")], "llm_truncated"
    if name == "protected_content_no_retry":
        return [Reply("refine", {"edits":[],"candidate_text":"帮我加一个按钮，修改数据库。"})], "protected_content_changed"
    raise AssertionError("unknown fixture scenario")


@pytest.mark.parametrize("name", ["503_retry", "bad_json_retry", "shared_retry_spent", "schema_retry_available",
    "auth401_no_retry", "truncated_no_retry", "protected_content_no_retry"])
def test_tc_cap_14_nfr09_real_http_retry_and_refusal_matrix(adapter_client, name, record_property):
    h = adapter_client
    replies, reason = scenario(name)
    h.upstream.install(*replies)
    actor = h.actor("matrix")
    peer = h.complete(actor, reason)
    h.ledger(actor)
    assert len(h.upstream.records) == len(replies)
    assert len({record["trace"]["deadline"] for record in h.upstream.records}) == 1
    record_property("stop_to_result_observed_ms", (peer.result_clock - peer.end_clock) * 1000)
    h.evidence(record_property)


def test_tc_cap_14_real_http_429_cooldown_never_dispatches_early(adapter_client, record_property):
    h, actor = adapter_client, adapter_client.actor("cooldown")
    h.upstream.install(Reply("refine", http_status=429, retry_after="5", expected_status="failed", expected_error="rate_limited"))
    h.complete(actor, "llm_unavailable")
    rate_limited = h.upstream.records[0]
    assert rate_limited["retry_after_sent"] == "5"
    wire_send_mono = rate_limited["response_headers_send_started_mono"]
    earliest_next_request_mono = wire_send_mono + 5.0
    async def cooldown():
        return h.app.state.llm.cooldown_until, asyncio.get_running_loop().time()
    until, now = h.client.portal.call(cooldown)
    assert until - now > 2, "fixture missed the >2s cooldown parameter"
    second = h.start(actor)
    until, now = h.client.portal.call(cooldown)
    assert until - now > 2, "fixture setup consumed the intended cooldown interval"
    h.end(second)
    h.result(second, actor, "llm_unavailable")
    assert h.client.portal.call(calls, h.app, actor["user"]["id"], second.session_id) == []
    assert len(h.upstream.records) == 1
    async def original_cooldown_ends():
        await asyncio.sleep(max(0, until - asyncio.get_running_loop().time()))
    h.client.portal.call(original_cooldown_ends)
    h.upstream.install(refine())
    h.complete(actor)
    assert h.upstream.records[1]["trace"]["received_loop"] >= until
    assert len(h.upstream.records) == 2
    # Do not infer Retry-After compliance from the provider's own cooldown
    # variable. Both wire timestamps below originate in time.monotonic(), not
    # in uvloop/asyncio clocks; no cross-clock subtraction is involved.
    later_requests = h.upstream.records[1:]
    next_request_mono = later_requests[0]["received_mono"]
    premature_requests = sum(record["received_mono"] < earliest_next_request_mono
                             for record in later_requests)
    record_property("retry_after_wire_observation", json.dumps({
        "adapter": h.upstream.adapter,
        "clock": "time.monotonic",
        "retry_after_header": rate_limited["retry_after_sent"],
        "retry_after_seconds": 5,
        "response_headers_send_started_mono": wire_send_mono,
        "earliest_next_request_mono": earliest_next_request_mono,
        "next_request_received_mono": next_request_mono,
        "observed_cooldown_ms": (next_request_mono - wire_send_mono) * 1000,
        "total_physical_requests": len(h.upstream.records),
        "requests_before_retry_after_elapsed": premature_requests,
    }))
    assert next_request_mono >= earliest_next_request_mono
    assert premature_requests == 0
    h.ledger(actor)
    h.evidence(record_property)


class QueueProbe:
    def __init__(self, llm, owner, priority, budget_ms):
        self.llm, self.owner, self.priority, self.budget_ms = llm, owner, priority, budget_ms
        self.loop = asyncio.get_running_loop()
        self.before, self.due = self.loop.create_future(), self.loop.create_future()
        self.handles = []
        self.monitor = asyncio.create_task(self.watch())

    async def watch(self):
        async with asyncio.timeout(4):
            while self.owner not in self.llm.scheduler.queues[self.priority]:
                await asyncio.sleep(.005)
        self.job = self.llm.scheduler.queues[self.priority][self.owner]
        # The real generate coroutine is waiting on the scheduler. Read its
        # already-computed admission time; do not wrap or replace generate.
        original_now = None
        for task in asyncio.all_tasks():
            coro = task.get_coro()
            while coro is not None:
                frame_ = getattr(coro, "cr_frame", None)
                if frame_ is not None:
                    values = frame_.f_locals
                    if values.get("self") is self.llm and values.get("owner") == self.owner:
                        original_now = values["now"]
                        break
                coro = getattr(coro, "cr_await", None)
            if original_now is not None:
                break
        assert original_now is not None
        assert self.job.wait_deadline == min(self.job.deadline, original_now + self.budget_ms / 1000)
        assert self.job.wait_deadline < self.job.deadline
        self.original_now = original_now
        assert self.loop.time() < self.job.wait_deadline - .05
        self.handles.append(self.loop.call_at(self.job.wait_deadline - .05, self.check, False))
        self.handles.append(self.loop.call_at(self.job.wait_deadline, lambda: self.handles.append(self.loop.call_soon(self.check, True))))

    def check(self, expired):
        future = self.due if expired else self.before
        if future.done():
            return
        try:
            job = self.job
            assert job.started is False
            if expired:
                assert self.owner not in self.llm.scheduler.queues[self.priority]
                assert job.future.done() and isinstance(job.future.exception(), SchedulerBusy)
                assert job.done.is_set() and job.operation is None
            else:
                assert self.llm.scheduler.queues[self.priority].get(self.owner) is job
                assert not job.future.done() and job.operation is not None
            future.set_result({"elapsed_ms": (self.loop.time() - self.original_now) * 1000,
                "timer_lateness_ms": (self.loop.time() - self.job.wait_deadline) * 1000 if expired else None})
        except Exception as exc:
            future.set_exception(exc)

    async def evidence(self):
        async with asyncio.timeout(5):
            await self.monitor
            before, due = await asyncio.gather(self.before, self.due)
        return {"budget_ms": self.budget_ms, "deadline": self.job.deadline,
                "wait_deadline": self.job.wait_deadline, "before": before, "expiry": due}

    async def close(self):
        for handle in self.handles:
            handle.cancel()
        for item in (self.monitor, self.before, self.due):
            if not item.done():
                item.cancel()
        await asyncio.gather(self.monitor, self.before, self.due, return_exceptions=True)


def test_tc_cap_14_real_http_occupied_slot_respects_original_queue_deadlines(adapter_client, record_property):
    h = adapter_client
    learner, blocker, foreground = [h.actor(name) for name in ("learner", "blocker", "foreground")]
    h.upstream.install(refine())
    seeded = h.complete(learner)
    held = refine(hold=True)
    h.upstream.install(held)
    blocking = h.start(blocker)
    h.end(blocking)
    async def wait_physical():
        async with asyncio.timeout(3):
            while len(h.upstream.records) < 2 or "trace" not in h.upstream.records[1]:
                await asyncio.sleep(.005)
    h.client.portal.call(wait_physical)
    queued = h.start(foreground)
    async def probe_queues():
        probes = [QueueProbe(h.app.state.llm, (foreground["user"]["id"], queued.session_id, 1), "high", 2000),
                  QueueProbe(h.app.state.llm, (learner["user"]["id"], "classify"), "low", 1000)]
        h.queue_probes.extend(probes)
        return probes
    front_probe, learn_probe = h.client.portal.call(probe_queues)
    h.end(queued)
    event_id = h.correct(learner, seeded)
    h.result(queued, foreground, "llm_unavailable")
    reads = h.failed_event(learner, event_id)
    for probe in (front_probe, learn_probe):
        record_property(f"queue_{probe.priority}", json.dumps(h.client.portal.call(probe.evidence)))
    assert len(h.upstream.records) == 2
    assert h.client.portal.call(calls, h.app, foreground["user"]["id"]) == []
    learner_calls = h.client.portal.call(calls, h.app, learner["user"]["id"])
    assert len(learner_calls) == 1 and all(row["learning_event_id"] is None for row in learner_calls)
    held.release.set()
    h.result(blocking, blocker)
    for actor in (learner, blocker, foreground):
        h.ledger(actor)
    record_property("learning_poll_count", reads)
    h.evidence(record_property)


@pytest.mark.parametrize("purpose", ["refine", "classify"])
def test_tc_nfr09_real_http_original_absolute_deadline(adapter_client, purpose, record_property):
    h, actor = adapter_client, adapter_client.actor("deadline")
    if purpose == "refine":
        h.upstream.install(refine(hold=True, expect_timeout=True, expected_status="unknown", expected_error="timeout"))
        peer = h.complete(actor, "llm_timeout")
        trace = h.upstream.records[0]["trace"]
        assert peer.result_clock >= trace["call_deadline"]
        record_property("stop_to_result_observed_ms", (peer.result_clock - peer.end_clock) * 1000)
    else:
        h.upstream.install(refine())
        peer = h.complete(actor)
        h.upstream.install(
            Reply("classify", {}, delay=.35, expected_status="failed", expected_error="invalid_output"),
            Reply("classify", {}, hold=True, expect_timeout=True, expected_status="unknown", expected_error="timeout"),
        )
        event_id = h.correct(actor, peer)
        reads = h.failed_event(actor, event_id)
        first, second = [record["trace"] for record in h.upstream.records if record["purpose"] == "classify"]
        assert first["learning_event_id"] == second["learning_event_id"] == event_id
        assert first["deadline"] == second["deadline"]
        # The total ten seconds includes terminal accounting. A physical call
        # may end earlier to reserve that work; it must not get a fresh deadline.
        assert second["call_deadline"] <= first["deadline"]
        assert 0 < second["call_deadline"] - second["started"] < 9.8
        record_property("learning_poll_count", reads)
    async def read_completed_accounting():
        rows = await calls(h.app, actor["user"]["id"])
        return rows, asyncio.get_running_loop().time()
    observed, ledger_readback_clock = h.client.portal.call(read_completed_accounting)
    total_deadline = trace["deadline"] if purpose == "refine" else first["deadline"]
    record_property("ledger_readback_before_original_deadline", json.dumps({
        "observed_loop": ledger_readback_clock, "original_deadline_loop": total_deadline,
        "remaining_ms": (total_deadline - ledger_readback_clock) * 1000,
    }))
    record_property("observed_model_call_outcomes", json.dumps(observed))
    # Read an already COMMITTED terminal row inside the SAME original budget.
    # No extra polling window, manual finished(), or background repair is used.
    assert ledger_readback_clock <= total_deadline
    assert observed and all(row["status"] != "running" for row in observed)
    h.evidence(record_property)
    # This is deliberately strict. A permanently running row is not unknown
    # usage and cannot be hidden by manually calling recorder.finished().
    h.ledger(actor)
