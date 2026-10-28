"""VOICE14 zero-sample parameter: real API/ASGI WS/PG, declared Mock providers.

This does NOT claim SILENCE5 real-ASR/no-hallucination or Desktop prompt evidence.
A normal nonzero session proves the call observers are installed and reachable.
"""
from __future__ import annotations

import asyncio
import json

import httpx
from sqlalchemy import text

from app.services.inference_context import current_scope
from tests.conftest import auth_headers, register_and_login
from tests.integration.test_normal_shutdown_contract import WsProbe
from tests.test_sessions import create_session


class ZeroSampleProbe(WsProbe):
    async def start(self):
        path = f"/ws/v1/sessions/{self.session_id}/audio"
        scope = {"type": "websocket", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1", "scheme": "ws", "root_path": "", "path": path,
            "raw_path": path.encode(), "query_string": b"", "state": {}, "subprotocols": [],
            "headers": [(b"host", b"testserver"), (b"authorization", self.headers["Authorization"].encode())],
            "client": ("127.0.0.1", 50000), "server": ("testserver", 80),
            "extensions": {"websocket.http.response": {}}}
        self.task = asyncio.create_task(self.app(scope, self.incoming.get, self.outgoing.put))
        await self.send({"type": "websocket.connect"})
        assert (await self.message())["type"] == "websocket.accept"
        assert (await self.event())["type"] == "ready"
        return self

    async def end(self):
        await self.send({"type": "websocket.receive", "text": json.dumps({
            "type": "audio.end", "last_seq": -1, "total_samples": 0})})
        ack = await self.event()
        assert (ack["type"], ack["last_seq"], ack["total_samples"]) == ("audio.end_ack", -1, 0)


async def read_zero_rows(app, owner, sid):
    async with app.state.db.session_factory() as db:
        await db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        parameters = {"owner": owner, "sid": sid}
        value = {}
        for table in ("sessions", "session_attempts", "results", "insertions", "learning_events", "model_calls", "session_contributions"):
            column = "id" if table == "sessions" else "session_id"
            value[table] = (await db.scalars(text(f"SELECT to_jsonb(t) FROM {table} t "
                f"WHERE user_id=:owner AND {column}=:sid ORDER BY to_jsonb(t)::text"), parameters)).all()
        value["model_usage"] = (await db.scalars(text(
            "SELECT to_jsonb(t) FROM model_usage_daily t WHERE user_id=:owner ORDER BY date,provider,model_id,purpose"), parameters)).all()
        value["active"] = await db.scalar(text("SELECT count(*) FROM sessions WHERE user_id=:owner "
            "AND state IN ('created','streaming','finalizing','refining')"), parameters)
        return value


def test_tc_voice_14_zero_samples_never_dispatch_asr_finish_or_llm_and_release_owners(
    app_client, monkeypatch, record_property,
):
    tokens = register_and_login(app_client, email="voice14-zero@example.invalid")
    headers, owner = auth_headers(tokens), tokens["user"]["id"]
    app, calls, streams = app_client.app, [], {}
    original_open = app.state.asr.open
    original_llm = app.state.llm.generate
    original_inner = app.state.llm.inner.generate

    async def observe_open(options):
        stream = await original_open(options)
        calls.append((options.session_id, "asr_open"))
        streams[options.session_id] = stream
        for method in ("feed", "finish", "cancel"):
            original = getattr(stream, method)

            async def observe(*args, _method=method, _original=original, **kwargs):
                calls.append((options.session_id, "asr_" + _method))
                return await _original(*args, **kwargs)

            monkeypatch.setattr(stream, method, observe)
        return stream

    async def observe_llm(request, deadline):
        scope = current_scope.get()
        calls.append((scope.session_id if scope else None, "llm_scheduled_" + request.purpose))
        return await original_llm(request, deadline)

    async def observe_inner(request, deadline):
        scope = current_scope.get()
        calls.append((scope.session_id if scope else None, "llm_physical_mock_" + request.purpose))
        return await original_inner(request, deadline)

    monkeypatch.setattr(app.state.asr, "open", observe_open)
    monkeypatch.setattr(app.state.llm, "generate", observe_llm)
    monkeypatch.setattr(app.state.llm.inner, "generate", observe_inner)
    created = create_session(app_client, headers, "voice14-zero-samples")
    assert created.status_code == 201
    sid = created.json()["session_id"]

    async def scenario():
        assert app.state.config.asr.provider == app.state.config.llm.provider == "mock"
        runtime = app.state.sessions.runtimes[sid]
        audio, asr = runtime.audio, streams[sid]
        assert not audio and runtime.samples == asr.samples == 0
        probe = await ZeroSampleProbe(app, headers, sid).start()
        await probe.end()
        await probe.drain()
        assert probe.close_code == 1000
        assert [event["type"] for event in probe.events] == ["ready", "audio.end_ack", "session.empty"]
        assert runtime.state == "empty" and runtime.task.done()
        assert runtime.audio is audio and len(audio) == 0 and runtime.asr is None
        assert asr.cancelled is True and asr.samples == runtime.samples == 0
        assert sid not in app.state.sessions.runtimes
        assert [kind for call_sid, kind in calls if call_sid == sid] == ["asr_open", "asr_cancel"]
        before = await read_zero_rows(app, owner, sid)
        assert len(before["sessions"]) == len(before["session_attempts"]) == len(before["session_contributions"]) == 1
        session, attempt = before["sessions"][0], before["session_attempts"][0]
        assert (session["state"], session["received_samples"], session["audio_complete"]) == ("empty", 0, True)
        assert session["current_result_id"] is None and session["error_code"] is None
        assert session["retry_cache_status"] == "none" and session["first_terminal_at"] is not None
        assert (attempt["state"], attempt["raw_text"], attempt["error_code"]) == ("empty", None, None)
        assert attempt["ended_at"] is not None
        for name in ("results", "insertions", "learning_events", "model_calls", "model_usage"):
            assert before[name] == [], {"unexpected_table": name, "row_count": len(before[name])}
        assert before["active"] == 0
        contribution = before["session_contributions"][0]
        assert contribution["outcome"] == "empty" and contribution["counted_session"] is False
        assert contribution["has_result"] is False and contribution["received_samples"] == 0
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://testserver") as http:
            response = await http.get(f"/api/v1/sessions/{sid}", headers=headers)
            assert response.status_code == 200
            body = response.json()
            assert (body["state"], body["result"], body["retry_kind"], body["retry_unavailable_reason"]) == ("empty", None, "none", "empty")
            # Same real HTTP create/WS pipeline with nonzero PCM: the observers
            # must see actual Mock finish, Scheduled refine, without a second model audit.
            import uuid
            key = str(uuid.uuid4())
            positive = await http.post("/api/v1/sessions", headers=headers | {"Idempotency-Key": key},
                json={"client_request_id": key, "learning_enabled": True})
            assert positive.status_code == 201
            positive_sid = positive.json()["session_id"]
            normal = await WsProbe(app, headers, positive_sid).start()
            await normal.end()
            await normal.drain()
            assert normal.close_code == 1000 and normal.events[-1]["type"] == "result.ready"
            positive_calls = [kind for call_sid, kind in calls if call_sid == positive_sid]
            assert positive_calls == ["asr_open", "asr_feed", "asr_finish", "asr_cancel",
                "llm_scheduled_refine", "llm_physical_mock_refine"]
            after = await read_zero_rows(app, owner, sid)
            # The normal positive contributes its own model daily row. It must
            # not mutate any zero-session row or create zero-session model calls.
            assert {k: v for k, v in after.items() if k != "model_usage"} == {
                k: v for k, v in before.items() if k != "model_usage"}
            assert [kind for call_sid, kind in calls if call_sid == sid] == ["asr_open", "asr_cancel"]
        return {"scope": "zero-sample ASGI+PG; Mock positive, no SILENCE5 claim", "sample_count": 0,
            "terminal": "empty", "asr_finish_calls": 0, "llm_scheduled_calls": 0,
            "llm_physical_calls": 0, "result_rows": 0, "insertion_rows": 0, "learning_rows": 0,
            "retained_pcm_bytes": len(audio), "positive_asr_finish_calls": positive_calls.count("asr_finish"),
            "positive_llm_calls": sum(kind.startswith("llm_physical_mock_") for kind in positive_calls)}

    record_property("voice14_zero_samples", json.dumps(app_client.portal.call(scenario), sort_keys=True))
