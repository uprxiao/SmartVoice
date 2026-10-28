"""CAP08: real PG/HTTP/WS admission atomicity, explicitly synthetic ASR/LLM.

No capacity/session state seed, clock override, weakened limit or online API.
Physical providers are Mock; these tests do not establish model throughput.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import hashlib
import json
import struct
from types import SimpleNamespace
import uuid

import httpx
import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.orm import Session as OrmSession

from app.models import OperationRequest, SessionAttempt, VoiceSession
from app.models.session import ACTIVE_STATES
from app.providers.asr.base import ProviderError
from tests.conftest import auth_headers, register_and_login
from tests.integration.test_capacity_normal_completion import AsgiPcmStream
from tests.integration.test_stats_pg_recovery import _wait_for_lifespan_readiness
from tests.test_admin import login_admin
from tests.test_retry import wait_state
from tests.test_sessions import create_session, dictation, frame


@pytest.fixture
def atomic_client(monkeypatch, request):
    monkeypatch.setenv("SVI_ASR_PROVIDER", "mock")
    monkeypatch.setenv("SVI_LLM_PROVIDER", "mock")
    return request.getfixturevalue("app_client")


def _limit_one(client):
    admin = auth_headers(login_admin(client))
    current = client.get("/api/admin/capacity", headers=admin)
    assert current.status_code == 200
    saved = client.patch("/api/admin/capacity", headers=admin,
        json={"version": current.json()["config"]["version"], "max_active_sessions": 1})
    assert saved.status_code == 200 and saved.json()["config"]["max_active_sessions"] == 1


async def _active(app):
    async with app.state.db.session_factory() as db:
        return int(await db.scalar(select(func.count()).select_from(VoiceSession)
                                  .where(VoiceSession.state.in_(ACTIVE_STATES))))


async def _fingerprint(app, owner):
    answer = {}
    tables = ("sessions", "session_attempts", "results", "operation_requests",
              "session_contributions", "usage_daily", "model_calls", "model_usage_daily",
              "dictionary_state", "dictionary_terms")
    async with app.state.db.session_factory() as db:
        for table in tables:
            # Literal identifiers only; only counts/SHA leave this function.
            rows = list((await db.scalars(text(
                f"SELECT to_jsonb(t) FROM {table} t WHERE user_id=:u ORDER BY to_jsonb(t)::text"
            ), {"u": owner})).all())
            encoded = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            answer[table] = {"count": len(rows), "sha256": hashlib.sha256(encoded).hexdigest()}
    return answer


class AudioFixture:
    def __init__(self, client, monkeypatch, texts):
        self.manager, self.texts = client.app.state.sessions, texts
        self.fail_initial, self.gate_candidates = set(), set()
        self.opened, self.feeds = [], {}
        self.entered = self.release = None
        provider = self.manager.asr_provider
        original_open = provider.open

        async def observed_open(options):
            self.opened.append((options.user_id, options.session_id, options.attempt_no))
            key = options.session_id, options.attempt_no
            self.feeds[key] = []
            stream = await original_open(options)
            stream.text = texts[options.user_id]  # Explicit Mock provider fixture.
            original_feed, original_finish = stream.feed, stream.finish

            async def feed(pcm, start_sample):
                self.feeds[key].append((start_sample, bytes(pcm)))
                await original_feed(pcm, start_sample)

            async def finish(total_samples, deadline):
                if options.session_id in self.fail_initial and options.attempt_no == 1:
                    raise ProviderError("ASR_FAILED", "CAP08 controlled complete-audio first failure")
                return await original_finish(total_samples, deadline)

            stream.feed, stream.finish = feed, finish
            if (options.user_id, options.attempt_no) in self.gate_candidates:
                self.entered.set()
                await self.release.wait()  # Only provider setup; no DB/lock is replaced.
            return stream

        monkeypatch.setattr(provider, "open", observed_open)


def _failed_audio(client, headers, control):
    created = create_session(client, headers, str(uuid.uuid4()))
    assert created.status_code == 201
    sid = created.json()["session_id"]
    runtime = control.manager.runtimes[sid]
    original_audio = runtime.audio
    control.fail_initial.add(sid)
    pcm = b"\x01\x00\xfe\xff" * 4000  # 8000 actual deterministic PCM samples.
    with client.websocket_connect(f"/ws/v1/sessions/{sid}/audio", headers=headers) as ws:
        assert ws.receive_json()["type"] == "ready"
        for seq in range(2):
            ws.send_bytes(struct.pack("<II", seq, 4000) + pcm[seq * 8000:(seq + 1) * 8000])
            ack = ws.receive_json()
            assert (ack["type"], ack["seq"], ack["total_samples"]) == ("audio.ack", seq, (seq + 1) * 4000)
        ws.send_json({"type": "audio.end", "last_seq": 1, "total_samples": 8000})
        assert ws.receive_json()["type"] == "audio.end_ack"
        error = ws.receive_json()
        assert error["type"] == "error" and error["error"]["code"] == "ASR_FAILED"
    detail = client.get(f"/api/v1/sessions/{sid}", headers=headers)
    assert detail.status_code == 200
    saved = detail.json()
    assert saved["state"] == "failed" and saved["audio_complete"] is True
    assert saved["received_samples"] == 8000 and saved["retry_kind"] == "asr"
    assert runtime.audio is original_audio and bytes(original_audio) == pcm
    assert runtime.retry_expires_at is not None
    return SimpleNamespace(sid=sid, runtime=runtime, audio=original_audio,
                           expiry=runtime.retry_expires_at, pcm=pcm)


class CommitFault:
    def __init__(self, app, owner, request_key, operation, session_id=None):
        self.app, self.owner, self.request_key = app, owner, request_key
        self.operation, self.session_id = operation, session_id
        self.engine = app.state.db.engine.sync_engine
        self.fired, self.sqlstate = 0, None
        self.readiness_select_completed = False
        self.precommit = None
        self.transient_runtime = None

    def before_commit(self, session):
        if self.fired or session.get_bind() is not self.engine:
            return
        target = next((row for row in (*session.new, *session.identity_map.values())
            if isinstance(row, VoiceSession) and row.user_id == self.owner
            and ((self.operation == "create" and row.client_request_id == self.request_key
                  and row.state == "created" and row.current_attempt_no == 1)
                 or (self.operation == "retry" and row.id == self.session_id
                     and row.state == "finalizing" and row.current_attempt_no == 2))), None)
        if target is None:
            return
        self.fired += 1
        session.flush()  # All writes reach real PG, still inside the uncommitted transaction.
        self.session_id = target.id
        connection = session.connection()
        self.precommit = {
            "active": int(connection.scalar(select(func.count()).select_from(VoiceSession)
                                             .where(VoiceSession.state.in_(ACTIVE_STATES)))),
            "attempts": int(connection.scalar(select(func.count()).select_from(SessionAttempt)
                                               .where(SessionAttempt.user_id == self.owner))),
            "retry_keys": int(connection.scalar(select(func.count()).select_from(OperationRequest)
                                                 .where(OperationRequest.user_id == self.owner,
                                                        OperationRequest.operation == "session.retry"))),
        }
        assert self.precommit == {"active": 1, "attempts": 1 if self.operation == "create" else 2,
                                  "retry_keys": 0 if self.operation == "create" else 1}
        self.transient_runtime = self.app.state.sessions.runtimes.get(target.id)
        # Actual PostgreSQL 22012 before COMMIT, not a fabricated Python error.
        connection.exec_driver_sql("SELECT 1 / 0")
        raise AssertionError("PostgreSQL fault did not occur")

    def error(self, context):
        if self.fired and context.statement == "SELECT 1 / 0":
            self.sqlstate = getattr(context.original_exception, "sqlstate", None)

    def after_cursor(self, connection, cursor, statement, parameters, context, many):
        if self.sqlstate == "22012" and " ".join(statement.lower().split()) == "select 1":
            self.readiness_select_completed = True

    def install(self):
        self.listeners = ((OrmSession, "before_commit", self.before_commit),
                          (self.engine, "handle_error", self.error),
                          (self.engine, "after_cursor_execute", self.after_cursor))
        for target, name, listener in self.listeners:
            event.listen(target, name, listener)

    def remove(self):
        for target, name, listener in reversed(self.listeners):
            event.remove(target, name, listener)


@contextmanager
def _fault(client, *args):
    injected = CommitFault(client.app, *args)
    client.portal.call(injected.install)
    try:
        yield injected
    finally:
        client.portal.call(injected.remove)


def _retry(client, headers, sid, key):
    return client.post(f"/api/v1/sessions/{sid}/retry", json={"retry_kind": "asr"},
                       headers={**headers, "Idempotency-Key": key})


def _cancel_twice_after_audio(client, headers, owner, manager):
    """Resource-release idempotence after a fresh, legitimately occupied slot."""
    created = create_session(client, headers, str(uuid.uuid4()))
    assert created.status_code == 201
    sid = created.json()["session_id"]
    runtime = manager.runtimes[sid]
    audio = runtime.audio
    with client.websocket_connect(f"/ws/v1/sessions/{sid}/audio", headers=headers) as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_bytes(frame(0))
        ack = ws.receive_json()
        assert (ack["type"], ack["seq"], ack["total_samples"]) == ("audio.ack", 0, 4000)
        assert len(audio) == 8000 and client.portal.call(_active, client.app) == 1
        first = client.post(f"/api/v1/sessions/{sid}/cancel", headers=headers)
        assert first.status_code == 200 and first.json()["state"] == "cancelled"
        assert ws.receive_json()["type"] == "cancelled"
    released = client.portal.call(_fingerprint, client.app, owner)
    second = client.post(f"/api/v1/sessions/{sid}/cancel", headers=headers)
    assert second.status_code == 200 and second.json()["state"] == "cancelled"
    assert audio == b"" and runtime.asr is None and runtime.retry_expires_at is None
    assert client.portal.call(_active, client.app) == 0
    assert client.portal.call(_fingerprint, client.app, owner) == released


@pytest.mark.parametrize("operation", ["create", "retry"])
def test_tc_cap_08_precommit_pg_failure_restores_admission_key_and_pcm(atomic_client, monkeypatch, operation, record_property):
    client = atomic_client
    _limit_one(client)
    actor = register_and_login(client, email="cap08-rollback@example.invalid")
    owner, headers = actor["user"]["id"], auth_headers(actor)
    control = AudioFixture(client, monkeypatch, {owner: "事务回滚的虚构口述。"})
    retained = _failed_audio(client, headers, control) if operation == "retry" else None
    request_key = str(uuid.uuid4())
    before = client.portal.call(_fingerprint, client.app, owner)
    opens_before = list(control.opened)
    with _fault(client, owner, request_key, operation, retained.sid if retained else None) as fault:
        failed = (create_session(client, headers, request_key) if operation == "create"
                  else _retry(client, headers, retained.sid, request_key))
        assert failed.status_code == 503 and failed.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
        assert fault.fired == 1 and fault.sqlstate == "22012"
        assert client.portal.call(_active, client.app) == 0
        assert client.portal.call(_fingerprint, client.app, owner) == before
        assert control.opened == opens_before, "provider started before admission committed"
        if retained is None:
            assert fault.transient_runtime is None
            assert fault.session_id not in control.manager.runtimes
        else:
            assert control.manager.runtimes[retained.sid] is retained.runtime
            assert retained.runtime.audio is retained.audio and bytes(retained.audio) == retained.pcm
            assert retained.runtime.retry_expires_at == retained.expiry
            assert fault.transient_runtime is not retained.runtime
            assert fault.transient_runtime.audio == b""
            assert fault.transient_runtime.task is None and fault.transient_runtime.asr is None
        _wait_for_lifespan_readiness(client, fault)  # Real unchanged readiness loop, no flag reset.
        accepted = (create_session(client, headers, request_key) if operation == "create"
                    else _retry(client, headers, retained.sid, request_key))
        assert accepted.status_code == (201 if operation == "create" else 202)
        sid = accepted.json()["session_id"]
        if operation == "create":
            assert dictation(client, headers, sid)[-1]["type"] == "result.ready"
        final = wait_state(client, headers, sid)
        assert final["state"] == "ready" and final["audio_complete"] is True
        assert final["result"]["review_required"] is False
        assert final["result"]["raw_text"] == control.texts[owner]
        assert final["current_attempt_no"] == (1 if operation == "create" else 2)
        if retained is not None:
            assert sid == retained.sid and retained.audio == b""
            assert control.feeds[(sid, 1)] == control.feeds[(sid, 2)]
        completed = client.portal.call(_fingerprint, client.app, owner)
        opens_completed = list(control.opened)
        replay = (create_session(client, headers, request_key) if operation == "create"
                  else _retry(client, headers, sid, request_key))
        assert replay.status_code == 200
        assert replay.json()["result"]["result_id"] == final["result"]["result_id"]
        assert client.portal.call(_fingerprint, client.app, owner) == completed
        assert control.opened == opens_completed and client.portal.call(_active, client.app) == 0
        if operation == "create":
            _cancel_twice_after_audio(client, headers, owner, control.manager)
        record_property("cap08_precommit_fault", json.dumps({"operation": operation,
            "sqlstate": fault.sqlstate, "precommit": fault.precommit, "restored": True,
            "cancel_release_idempotence": operation == "create"}, sort_keys=True))


async def _wait_ready(http, headers, sid):
    async with asyncio.timeout(10):  # Harness guard, not supplied to a product deadline.
        while True:
            response = await http.get(f"/api/v1/sessions/{sid}", headers=headers)
            assert response.status_code == 200
            body = response.json()
            if body["state"] in ("ready", "failed", "empty", "cancelled"):
                assert body["state"] == "ready" and body["result"]["review_required"] is False
                return body
            await asyncio.sleep(.2)


def test_tc_cap_08_create_and_audio_retry_compete_for_last_slot(atomic_client, monkeypatch, record_property):
    client = atomic_client
    _limit_one(client)
    a = register_and_login(client, email="cap08-create@example.invalid")
    b = register_and_login(client, email="cap08-retry@example.invalid")
    aid, bid, ah, bh = a["user"]["id"], b["user"]["id"], auth_headers(a), auth_headers(b)
    control = AudioFixture(client, monkeypatch, {aid: "创建账户的虚构口述。", bid: "重试账户的虚构口述。"})
    retained = _failed_audio(client, bh, control)
    create_key, retry_key = str(uuid.uuid4()), str(uuid.uuid4())

    async def run():
        app, tasks, streams = client.app, {}, []
        control.entered, control.release = asyncio.Event(), asyncio.Event()
        control.gate_candidates = {(aid, 1), (bid, 2)}
        before = {uid: await _fingerprint(app, uid) for uid in (aid, bid)}
        barrier = asyncio.Barrier(2)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as http:
            async def request(kind, synchronized=True):
                if synchronized:
                    await barrier.wait()
                if kind == "create":
                    return await http.post("/api/v1/sessions", headers={**ah, "Idempotency-Key": create_key},
                        json={"client_request_id": create_key, "learning_enabled": True})
                return await http.post(f"/api/v1/sessions/{retained.sid}/retry",
                    headers={**bh, "Idempotency-Key": retry_key}, json={"retry_kind": "asr"})

            async def finish(kind, response):
                assert response.status_code == (201 if kind == "create" else 202)
                sid = response.json()["session_id"]
                if kind == "create":
                    stream = AsgiPcmStream(app, SimpleNamespace(headers=lambda: ah), sid, 7, control.texts[aid])
                    streams.append(stream)
                    await stream.start()  # Connect immediately after successful HTTP create.
                    stream.finish_requested.set()
                    await stream.runner  # Normal audio.end/result, not cancellation/SQL slot release.
                result = await _wait_ready(http, ah if kind == "create" else bh, sid)
                assert result["result"]["raw_text"] == control.texts[aid if kind == "create" else bid]
                assert result["current_attempt_no"] == (1 if kind == "create" else 2)
                return result

            try:
                tasks = {kind: asyncio.create_task(request(kind)) for kind in ("create", "retry")}
                # A create winner is blocked before returning 201; a retry
                # winner may already return 202. Neither can finish before this
                # real persisted capacity snapshot and loser 429 are observed.
                async with asyncio.timeout(4):
                    await control.entered.wait()
                    while not any(task.done() and task.result().status_code == 429 for task in tasks.values()):
                        for task in tasks.values():
                            if task.done():
                                assert task.result().status_code in (201, 202, 429)
                        await asyncio.sleep(.01)
                losers = [kind for kind, task in tasks.items() if task.done() and task.result().status_code == 429]
                assert len(losers) == 1
                loser = losers[0]
                winner = "retry" if loser == "create" else "create"
                assert tasks[loser].result().json()["error"]["code"] == "CAPACITY_EXCEEDED"
                assert await _active(app) == 1
                loser_uid = aid if loser == "create" else bid
                assert await _fingerprint(app, loser_uid) == before[loser_uid]
                winner_facts = await _fingerprint(app, aid if winner == "create" else bid)
                assert winner_facts["sessions"]["count"] == 1
                assert winner_facts["session_attempts"]["count"] == (1 if winner == "create" else 2)
                assert winner_facts["operation_requests"]["count"] == (0 if winner == "create" else 1)
                if loser == "retry":
                    assert control.manager.runtimes[retained.sid] is retained.runtime
                    assert retained.runtime.audio is retained.audio and bytes(retained.audio) == retained.pcm
                else:
                    assert control.manager.runtimes[retained.sid].audio is retained.audio
                    assert retained.runtime.audio == b""
                assert control.manager.runtimes[retained.sid].retry_expires_at == retained.expiry
                control.release.set()
                responses = {kind: await task for kind, task in tasks.items()}
                first = await finish(winner, responses[winner])
                assert await _active(app) == 0
                second_response = await request(loser, synchronized=False)  # Exact failed key, no replacement.
                second = await finish(loser, second_response)
                assert first["session_id"] != second["session_id"]
                assert first["result"]["result_id"] != second["result"]["result_id"]
                assert await _active(app) == 0 and retained.audio == b""
                assert control.feeds[(retained.sid, 1)] == control.feeds[(retained.sid, 2)]
                assert sum(sid == retained.sid and attempt == 2 for _, sid, attempt in control.opened) == 1
                completed = {uid: await _fingerprint(app, uid) for uid in (aid, bid)}
                opens = list(control.opened)
                for kind in ("create", "retry"):
                    replay = await request(kind, synchronized=False)
                    assert replay.status_code == 200 and replay.json()["state"] == "ready"
                assert {uid: await _fingerprint(app, uid) for uid in (aid, bid)} == completed
                assert control.opened == opens and await _active(app) == 0
                record_property("cap08_last_slot", json.dumps({"winner": winner,
                    "loser": loser, "active_at_gate": 1, "active_after_both": 0,
                    "rejected_key_reused": True}, sort_keys=True))
            finally:
                control.release.set()
                control.gate_candidates.clear()
                for task in tasks.values():
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks.values(), return_exceptions=True)
                for stream in streams:
                    await stream.dispose()

    client.portal.call(run)
