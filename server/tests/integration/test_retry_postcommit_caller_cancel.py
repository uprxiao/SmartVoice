"""Actual PG COMMIT followed by cancellation at its Python return boundary.

Uses current QA without lease implementation/registry. Real HTTP/WS/PG;
synthetic PCM and existing Mock providers. The gate holds only the return
after real COMMIT. This is not a lost network ACK or 600-second acceptance.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import uuid

import httpx
from sqlalchemy import event, text

from tests.conftest import auth_headers, register_and_login
from tests.integration.test_capacity_admission_atomicity import (
    AudioFixture, _active, _failed_audio, atomic_client,
)
from tests.integration.test_normal_shutdown_contract import require_owned_database


async def wait_until(predicate):
    # Harness observation guard only; production deadlines/config unchanged.
    async with asyncio.timeout(8):
        while not predicate():
            await asyncio.sleep(0.01)


async def rows(app, owner, sid, key):
    async with app.state.db.session_factory() as db:
        args = {"u": owner, "s": sid, "k": key}
        session = (await db.execute(text("""SELECT state,current_attempt_no,
            received_samples,audio_complete,first_asr_failed_at
            FROM sessions WHERE user_id=:u AND id=:s"""), args)).one()
        attempts = (await db.execute(text("""SELECT attempt_no,state,error_code
            FROM session_attempts WHERE user_id=:u AND session_id=:s
            ORDER BY attempt_no"""), args)).all()
        keys = (await db.execute(text("""SELECT target_type,target_id
            FROM operation_requests WHERE user_id=:u
            AND operation='session.retry' AND request_key=:k"""), args)).all()
        return {"session": tuple(session), "attempts": [tuple(row) for row in attempts],
                "keys": [tuple(row) for row in keys]}


class CommittedReturnGate:
    def __init__(self, app, retained):
        self.app, self.retained = app, retained
        self.manager = app.state.sessions
        self.actual_admission = self.manager._admission_session
        self.actual_register = self.manager._register_runtime
        self.engine = app.state.db.engine.sync_engine
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.used = self.commit_completed = False
        self.transferred = self.request_task = self.connection_id = None
        self.transaction_after_commit = None
        self.commit_connection_returned = False
        self.pool_events, self.restore_observations = [], []

    def checkout(self, connection, record, proxy):
        self.pool_events.append((id(connection), "checkout"))

    def checkin(self, connection, record):
        self.pool_events.append((id(connection), "checkin"))

    def register(self, runtime):
        answer = self.actual_register(runtime)
        if runtime is self.retained.runtime:
            self.restore_observations.append({
                "commit_completed": self.commit_completed,
                "attempt_no": runtime.attempt_no,
                "pcm_bytes": len(runtime.audio),
                "expiry_unchanged": runtime.retry_expires_at == self.retained.expiry,
            })
        return answer

    @asynccontextmanager
    async def admission(self):
        async with self.actual_admission() as db:
            if self.used:
                yield db
                return
            self.used = True
            actual_commit = db.commit

            async def commit():
                await db.flush()
                conn = await db.connection()
                self.connection_id = id(conn.sync_connection.connection.dbapi_connection)
                checkout_index = max(index for index, (identity, action) in enumerate(self.pool_events)
                    if identity == self.connection_id and action == "checkout")
                self.transferred = self.manager.runtimes[self.retained.sid]
                assert self.transferred is not self.retained.runtime
                assert self.transferred.attempt_no == 2
                assert self.transferred.audio is self.retained.audio
                assert bytes(self.transferred.audio) == self.retained.pcm
                assert self.retained.runtime.audio == b""
                assert not self.transferred.processing_accepted
                self.request_task = asyncio.current_task()
                await actual_commit()  # The actual PG transaction, not a fake success.
                self.commit_completed = True
                self.transaction_after_commit = db.in_transaction()
                self.commit_connection_returned = any(identity == self.connection_id and action == "checkin"
                    for identity, action in self.pool_events[checkout_index + 1:])
                self.entered.set()
                await self.release.wait()  # No DB transaction/row lock remains here.

            db.commit = commit
            try:
                yield db
            finally:
                db.commit = actual_commit

    def install(self):
        self.manager._admission_session = self.admission
        self.manager._register_runtime = self.register
        event.listen(self.engine, "checkout", self.checkout)
        event.listen(self.engine, "checkin", self.checkin)

    def remove(self):
        event.remove(self.engine, "checkin", self.checkin)
        event.remove(self.engine, "checkout", self.checkout)
        self.manager._register_runtime = self.actual_register
        self.manager._admission_session = self.actual_admission


def test_retry_durable_commit_caller_cancel_keeps_current_attempt_owner(
    atomic_client, monkeypatch, record_property,
):
    require_owned_database()
    client = atomic_client
    actor = register_and_login(client, email="qa-postcommit-return@example.invalid")
    owner, headers = actor["user"]["id"], auth_headers(actor)
    physical = AudioFixture(client, monkeypatch, {owner: "提交之后的虚构口述。"})
    retained = _failed_audio(client, headers, physical)

    async def exercise():
        app, manager = client.app, physical.manager
        assert manager.config.retry.audio_ttl_seconds == 600
        assert manager.config.asr.finish_timeout_ms == 10000
        assert manager.config.pipeline.stop_to_result_budget_ms == 30000
        await wait_until(lambda: retained.runtime.task is None or retained.runtime.task.done())
        assert not retained.runtime.connected
        gate = CommittedReturnGate(app, retained)
        request = None
        key = str(uuid.uuid4())
        evidence = {"scope": "real PG COMMIT + HTTP/WS; return Event; synthetic PCM/Mock",
                    "issues": []}
        gate.install()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://qa-postcommit-test") as http:
            try:
                before = await rows(app, owner, retained.sid, key)
                request = asyncio.create_task(http.post(f"/api/v1/sessions/{retained.sid}/retry",
                    headers={**headers, "Idempotency-Key": key}, json={"retry_kind": "asr"}))
                await wait_until(gate.entered.is_set)
                committed = await rows(app, owner, retained.sid, key)  # Independent PG read.
                assert gate.commit_completed and gate.transaction_after_commit is False
                assert gate.commit_connection_returned
                assert committed["session"][:4] == ("finalizing", 2, 8000, True)
                assert committed["session"][4] == before["session"][4]
                assert committed["attempts"] == [*before["attempts"], (2, "running", None)]
                assert committed["keys"] == [("session", retained.sid)]
                assert await _active(app) == 1 and len(physical.opened) == 1
                assert gate.transferred.retry_expires_at == retained.expiry
                evidence["durable_commit_verified_before_cancel"] = True
                evidence["commit_owner_is_http_task"] = gate.request_task is request
                request.cancel()  # Real HTTP caller; no direct runtime/provider mutation.
                await asyncio.sleep(0)
                gate.release.set()  # Also permits a future shielded owner to finish.
                await wait_until(request.done)
                outcome = (await asyncio.gather(request, return_exceptions=True))[0]
                evidence["caller_outcome"] = type(outcome).__name__
                if isinstance(outcome, httpx.Response):
                    assert outcome.status_code == 202
                else:
                    assert isinstance(outcome, asyncio.CancelledError)
                after = await rows(app, owner, retained.sid, key)
                assert after["session"][1] == 2
                assert [row[0] for row in after["attempts"]] == [1, 2]
                assert after["keys"] == [("session", retained.sid)]
                assert after["session"][4] == before["session"][4]
                replay = await http.post(f"/api/v1/sessions/{retained.sid}/retry",
                    headers={**headers, "Idempotency-Key": key}, json={"retry_kind": "asr"})
                assert replay.status_code == 200 and replay.json()["replayed"]
                assert replay.json()["current_attempt_no"] == 2
                if any(row["commit_completed"] and row["attempt_no"] == 1 and row["pcm_bytes"] > 0
                       for row in gate.restore_observations):
                    evidence["issues"].append("committed attempt2 republished previous live PCM owner")
                current = manager.runtimes.get(retained.sid)
                evidence["current_runtime_attempt"] = None if current is None else current.attempt_no
                if current is not None and current.attempt_no != 2:
                    evidence["issues"].append("local current identity differs from durable attempt2")

                def live_owner():
                    # Current QA owns processing through RuntimeSession.task.
                    # Inspect the exact transferred object too; no lease/registry dependency.
                    candidates = (manager.runtimes.get(retained.sid), gate.transferred)
                    return any(runtime is not None and runtime.user_id == owner
                        and runtime.session_id == retained.sid and runtime.attempt_no == 2
                        and runtime.task is not None and not runtime.task.done()
                        for runtime in candidates)

                latest = await rows(app, owner, retained.sid, key)
                active = latest["session"][0] in ("created", "streaming", "finalizing", "refining")
                owned = live_owner()
                if active and not owned:
                    latest = await rows(app, owner, retained.sid, key)  # Exclude a concurrent terminal commit.
                    active = latest["session"][0] in ("created", "streaming", "finalizing", "refining")
                    owned = live_owner()
                evidence.update(db_state=latest["session"][0], active_slot=active,
                    live_attempt2_owner=owned, physical_retry_open_count=sum(
                        attempt == 2 for _, _, attempt in physical.opened))
                if active and not owned:
                    evidence["issues"].append("durable active attempt2 has no processing task after caller exits and replay")
                # That transaction's checkout had a matching return before the
                # gate. Later health checks may legally reuse the same connection.
                evidence["commit_connection_returned_before_gate"] = gate.commit_connection_returned
                async with app.state.db.session_factory() as db:
                    assert await db.scalar(text("SELECT 1")) == 1
                evidence["fresh_query_succeeded"] = True
                assert not evidence["issues"], evidence["issues"]
            finally:
                gate.release.set()
                cleanup_errors = []
                try:
                    if request is not None:
                        if not request.done() and not request.cancelling():
                            request.cancel()
                        done, _ = await asyncio.wait({request}, timeout=8)
                        if request not in done:
                            cleanup_errors.append({"stage": "request_join", "type": "TimeoutError"})
                        else:
                            await asyncio.gather(request, return_exceptions=True)
                    # Cleanup only AFTER evidence; never counted as a recovered owner.
                    try:
                        async with asyncio.timeout(8):
                            cancelled = await http.post(f"/api/v1/sessions/{retained.sid}/cancel", headers=headers)
                        assert cancelled.status_code in (200, 409)
                        evidence["cleanup_cancel_status"] = cancelled.status_code
                    except BaseException as exc:
                        cleanup_errors.append({"stage": "business_cancel", "type": type(exc).__name__})
                finally:
                    gate.remove()
                    evidence.update(commit_completed=gate.commit_completed,
                        transaction_after_commit=gate.transaction_after_commit,
                        restore_observations=gate.restore_observations, cleanup_errors=cleanup_errors)
                    record_property("qa_postcommit_caller_cancel", json.dumps(evidence, sort_keys=True))
                assert not cleanup_errors, cleanup_errors

    client.portal.call(exercise)
