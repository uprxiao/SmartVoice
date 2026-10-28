"""Real PG + ASGI HTTP/WebSocket control-plane completion.

Synthetic legal PCM and per-user Mock ASR text do not prove TCP, microphone,
Desktop, model quality or model throughput. No production clock/deadline changes.
"""
from __future__ import annotations

import asyncio
import json
import struct

import httpx

from tests.integration.test_capacity_control_groups import (
    _active, _create, _error, _mock_expanded_deployment, _patch,
    _seed_actors, _simultaneous, _snapshot, _view,
)

# Importing this existing autouse fixture intentionally preserves the audited
# actor seed's validated deployment ceiling of 100. Saved admission is still
# exactly 20/10 or 2 here; this is not an N=100 processing claim.


class AsgiPcmStream:
    """One authenticated real ASGI WS, bounded transport, one unacked PCM frame."""

    def __init__(self, app, actor, session_id, pcm_value, expected_text):
        self.app, self.actor, self.session_id = app, actor, session_id
        self.expected_text = expected_text
        self.pcm = struct.pack("<h", pcm_value) * 4000
        self.incoming, self.outgoing = asyncio.Queue(maxsize=2), asyncio.Queue(maxsize=8)
        self.finish_requested, self.started = asyncio.Event(), asyncio.Event()
        self.total_samples, self.last_seq = 0, -1
        self.app_task = self.runner = None
        self.result = None

    async def start(self):
        scope = {
            "type": "websocket", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1", "scheme": "ws", "root_path": "",
            "path": f"/ws/v1/sessions/{self.session_id}/audio",
            "raw_path": f"/ws/v1/sessions/{self.session_id}/audio".encode(),
            "query_string": b"", "headers": [
                (b"host", b"testserver"),
                (b"authorization", self.actor.headers()["Authorization"].encode()),
            ],
            "client": ("127.0.0.1", 50000), "server": ("testserver", 80),
            "subprotocols": [], "extensions": {"websocket.http.response": {}},
            "state": {},
        }
        self.app_task = asyncio.create_task(self.app(scope, self.incoming.get, self.outgoing.put))
        self.runner = asyncio.create_task(self._run())
        # Startup is bounded inside the existing 10-second connect window.
        started_task = asyncio.create_task(self.started.wait())
        try:
            async with asyncio.timeout(4):
                done, _ = await asyncio.wait((started_task, self.runner), return_when=asyncio.FIRST_COMPLETED)
                if self.runner in done:
                    await self.runner  # preserve protocol/DB errors instead of a misleading timeout
                assert self.started.is_set()
        finally:
            started_task.cancel()
            await asyncio.gather(started_task, return_exceptions=True)
        return self

    async def _message(self, timeout=3):
        async with asyncio.timeout(timeout):
            return await self.outgoing.get()

    async def _event(self, timeout=3):
        message = await self._message(timeout)
        assert message["type"] == "websocket.send", {"unexpected_ws_type": message["type"], "close_code": message.get("code")}
        assert message.get("text") is not None
        return json.loads(message["text"])

    async def _send(self, message):
        async with asyncio.timeout(2):
            await self.incoming.put(message)

    async def _run(self):
        await self._send({"type": "websocket.connect"})
        accepted = await self._message()
        assert accepted["type"] == "websocket.accept", {"unexpected_ws_type": accepted["type"], "status": accepted.get("status")}
        ready = await self._event()
        assert ready["type"] == "ready" and ready["session_id"] == self.session_id
        assert ready["attempt_no"] == 1
        assert ready["audio"]["sample_rate_hz"] == 16000 and ready["audio"]["chunk_samples"] == 4000
        while True:
            assert self.total_samples + 4000 <= 2_880_000
            self.last_seq += 1
            await self._send({"type": "websocket.receive",
                "bytes": struct.pack("<II", self.last_seq, 4000) + self.pcm})
            ack = await self._event(timeout=2)
            self.total_samples += 4000
            assert ack["type"] == "audio.ack"
            assert ack["session_id"] == self.session_id
            assert ack["seq"] == self.last_seq and ack["total_samples"] == self.total_samples
            self.started.set()
            try:
                # Real 250 ms cadence, no synthetic heartbeat/clock manipulation.
                async with asyncio.timeout(.25):
                    await self.finish_requested.wait()
                break
            except TimeoutError:
                pass
        await self._send({"type": "websocket.receive", "text": json.dumps({
            "type": "audio.end", "last_seq": self.last_seq, "total_samples": self.total_samples,
        })})
        end_acked = False
        asr_final_text = None
        guard = self.app.state.config.pipeline.stop_to_result_budget_ms / 1000 + 2
        async with asyncio.timeout(guard):
            while True:
                event = await self._event(timeout=guard)
                assert event["session_id"] == self.session_id
                if event["type"] == "audio.end_ack":
                    assert event["last_seq"] == self.last_seq and event["total_samples"] == self.total_samples
                    assert event["attempt_no"] == 1
                    end_acked = True
                elif event["type"] == "asr.final":
                    assert end_acked, "ASR final arrived without audio.end acknowledgement"
                    assert asr_final_text is None, "duplicate ASR final"
                    assert event["attempt_no"] == 1
                    assert event["text"] == self.expected_text
                    asr_final_text = event["text"]
                elif event["type"] == "result.ready":
                    assert end_acked, "terminal output arrived without audio.end acknowledgement"
                    assert asr_final_text is not None, "speech result arrived without ASR final"
                    assert event["attempt_no"] == 1
                    self.result = event["result"]
                    assert self.result["raw_text"] == asr_final_text == self.expected_text
                    assert self.result["refined_text"] == self.expected_text
                    assert self.result["review_required"] is False
                    break
                else:
                    raise AssertionError({"unexpected_event": event["type"], "code": event.get("error", {}).get("code")})
        closed = await self._message()
        assert closed["type"] == "websocket.close" and closed["code"] == 1000
        async with asyncio.timeout(3):
            await self.app_task
        return self.result

    async def dispose(self):
        # Success already closed normally. On assertion failure only, stop local
        # harness tasks and let lifespan clean up; never count this as completion.
        if self.app_task is not None and not self.app_task.done():
            try:
                self.incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})
            except asyncio.QueueFull:
                pass
        tasks = [task for task in (self.runner, self.app_task) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            async with asyncio.timeout(4):
                await asyncio.gather(*tasks, return_exceptions=True)


def per_user_text(monkeypatch, app, actors):
    texts = {actor.user_id: f"账户{index:02d}的虚构口述文本。" for index, actor in enumerate(actors)}
    provider = app.state.sessions.asr_provider
    original_open = provider.open

    async def isolated_open(options):
        # This user_id is formal AsrOpenOptions metadata from authenticated
        # SessionManager, not a global mutable text setting or guessed active user.
        assert options.user_id in texts and options.attempt_no == 1
        stream = await original_open(options)
        assert stream.options.user_id == options.user_id
        stream.text = texts[options.user_id]  # only the deterministic Mock fixture
        return stream

    monkeypatch.setattr(provider, "open", isolated_open)
    return texts


async def admitted_stream(http, app, actor, streams, texts, pcm_value):
    response = await _create(http, actor)
    if response.status_code != 201:
        return response, None
    stream = AsgiPcmStream(app, actor, response.json()["session_id"], pcm_value, texts[actor.user_id])
    streams.append(stream)
    await stream.start()  # attach immediately, rather than waiting for the full burst
    return response, stream


async def complete_normally(http, streams):
    for stream in streams:
        assert stream.runner is not None and not stream.runner.done()
        stream.finish_requested.set()
    if streams:
        guard = streams[0].app.state.config.pipeline.stop_to_result_budget_ms / 1000 + 5
        async with asyncio.timeout(guard):
            await asyncio.gather(*(stream.runner for stream in streams))
    for stream in streams:
        response = await http.get(f"/api/v1/sessions/{stream.session_id}", headers=stream.actor.headers())
        assert response.status_code == 200, _error(response)
        body = response.json()
        assert body["state"] == "ready" and body["error_code"] is None
        assert body["audio_complete"] is True and body["received_samples"] == stream.total_samples
        assert body["current_attempt_no"] == 1
        assert body["result"]["result_id"] == stream.result["result_id"]
        assert body["result"]["raw_text"] == body["result"]["refined_text"] == stream.expected_text
        assert body["result"]["review_required"] is False


async def assert_population(app, active, ready, total):
    snapshot = await _snapshot(app)
    assert len(snapshot["rows"]) == snapshot["attempts"] == total
    assert len(_active(snapshot)) == active
    assert sum(row["state"] == "ready" for row in snapshot["rows"]) == ready
    assert all(row["state"] in ("streaming", "ready") and row["error_code"] is None
               and row["current_attempt_no"] == 1 for row in snapshot["rows"])
    return snapshot


def test_tc_cap_05_lowering_capacity_preserves_normal_completion_boundaries(app_client, monkeypatch):
    async def run():
        app, streams = app_client.app, []
        actors, admins = await _seed_actors(app, 21)
        texts = per_user_text(monkeypatch, app, actors)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as http:
                current = (await _view(http, admins[0]))["config"]
                assert current["max_active_sessions"] == 20
                outcomes = await _simultaneous([
                    lambda actor=actor, index=index: admitted_stream(http, app, actor, streams, texts, index + 1)
                    for index, actor in enumerate(actors[:20])
                ])
                assert all(response.status_code == 201 and stream is not None for response, stream in outcomes)
                ordered = [stream for _, stream in outcomes]
                await assert_population(app, active=20, ready=0, total=20)
                assert all(stream.started.is_set() and not stream.runner.done() for stream in ordered)
                lowered = await _patch(http, admins[0], current["version"], 10)
                assert lowered.status_code == 200, _error(lowered)
                assert lowered.json()["config"]["max_active_sessions"] == 10
                assert lowered.json()["config"]["version"] == current["version"] + 1
                await assert_population(app, active=20, ready=0, total=20)

                async def reject_at(active, ready):
                    await assert_population(app, active=active, ready=ready, total=20)
                    rejected = await _create(http, actors[20])
                    assert rejected.status_code == 429 and rejected.json()["error"]["code"] == "CAPACITY_EXCEEDED"
                    await assert_population(app, active=active, ready=ready, total=20)
                    capabilities = await http.get("/api/v1/capabilities", headers=actors[20].headers())
                    assert capabilities.status_code == 200 and capabilities.json()["limits"]["global_sessions"] == 10
                    assert (await _view(http, admins[0]))["config"] == lowered.json()["config"]

                await reject_at(active=20, ready=0)
                await complete_normally(http, ordered[:10])
                await reject_at(active=10, ready=10)
                await complete_normally(http, [ordered[10]])
                await assert_population(app, active=9, ready=11, total=20)
                # Same request ID/key as both 429s: no stale slot or key remains.
                accepted, replacement = await admitted_stream(http, app, actors[20], streams, texts, 21)
                assert accepted.status_code == 201 and accepted.json()["replayed"] is False
                assert replacement is not None
                await assert_population(app, active=10, ready=11, total=21)
                await complete_normally(http, ordered[11:] + [replacement])
                await assert_population(app, active=0, ready=21, total=21)
                assert len({stream.result["result_id"] for stream in streams}) == 21
                assert all(stream.app_task.done() and not stream.app_task.cancelled() for stream in streams)
        finally:
            await asyncio.gather(*(stream.dispose() for stream in streams))
    app_client.portal.call(run)


def test_tc_nfr_03_ten_authenticated_users_compete_for_two_slots_and_finish(app_client, monkeypatch):
    async def run():
        app, streams = app_client.app, []
        actors, admins = await _seed_actors(app, 10)
        texts = per_user_text(monkeypatch, app, actors)
        manager = app.state.sessions
        original_create = manager.create
        arrivals = 0
        barrier = asyncio.Barrier(10)

        async def authenticated_create(ctx, client_request_id, learning_enabled):
            nonlocal arrivals
            arrivals += 1
            await barrier.wait()
            return await original_create(ctx, client_request_id, learning_enabled)

        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as http:
                current = (await _view(http, admins[0]))["config"]
                saved = await _patch(http, admins[0], current["version"], 2)
                assert saved.status_code == 200 and saved.json()["config"]["max_active_sessions"] == 2
                # Barrier is after real Depends/authentication but before the
                # original capacity lock; no auth/admission function is bypassed.
                manager.create = authenticated_create
                try:
                    async with asyncio.timeout(8):
                        outcomes = await asyncio.gather(*(
                            admitted_stream(http, app, actor, streams, texts, index + 1)
                            for index, actor in enumerate(actors)
                        ))
                finally:
                    manager.create = original_create
                assert arrivals == 10
                assert [response.status_code for response, _ in outcomes].count(201) == 2
                assert [response.status_code for response, _ in outcomes].count(429) == 8
                assert all(response.status_code in (201, 429) for response, _ in outcomes)
                assert all(response.json()["error"]["code"] == "CAPACITY_EXCEEDED"
                           for response, _ in outcomes if response.status_code == 429)
                winners = [stream for _, stream in outcomes if stream is not None]
                assert len(winners) == 2 and len({stream.actor.user_id for stream in winners}) == 2
                assert all(stream.started.is_set() and not stream.finish_requested.is_set()
                           and not stream.runner.done() for stream in winners)
                before = await assert_population(app, active=2, ready=0, total=2)
                assert {row["user_id"] for row in before["rows"]} == {stream.actor.user_id for stream in winners}
                await complete_normally(http, winners)
                await assert_population(app, active=0, ready=2, total=2)
                assert winners[0].result["result_id"] != winners[1].result["result_id"]
                assert winners[0].result["raw_text"] != winners[1].result["raw_text"]
                for own, other in ((winners[0], winners[1]), (winners[1], winners[0])):
                    foreign = await http.get(f"/api/v1/sessions/{other.session_id}", headers=own.actor.headers())
                    assert foreign.status_code == 404 and foreign.json()["error"]["code"] == "NOT_FOUND"
                assert all(stream.app_task.done() and not stream.app_task.cancelled() for stream in winners)
        finally:
            manager.create = original_create
            await asyncio.gather(*(stream.dispose() for stream in streams))
    app_client.portal.call(run)
