"""P2/P3: actual PG COMMIT + repeated caller cancellation/refine fallback.

Formal HTTP/WS/PG and ScheduledLlmProvider/Refiner; physical providers are Mock.
No lease code, manual review/session UPDATE, deadline override or live model.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
import uuid

import httpx
import pytest
from sqlalchemy import text

from app.core.security import idempotency_hash
from app.providers.asr.base import ProviderError
from app.providers.llm.base import LlmResponse
from app.services.inference_context import current_scope
from tests.conftest import auth_headers, register_and_login
from tests.integration.test_capacity_admission_atomicity import AudioFixture, _failed_audio, atomic_client
from tests.integration.test_normal_shutdown_contract import require_owned_database
from tests.integration.test_retry_postcommit_caller_cancel import CommittedReturnGate, rows, wait_until
from tests.test_dictionary import create_term
from tests.test_sessions import create_session, dictation


class RetryReturnGate(CommittedReturnGate):
    def __init__(self, app, retained, kind, hold_first_cancel):
        super().__init__(app, retained)
        self.kind, self.hold_first_cancel = kind, hold_first_cancel
        self.first_cancel_seen = asyncio.Event()
        self.second_release = asyncio.Event()
        self.seen_cancel_count = 0
        self.original_deadline = self.original_end = None
        self.frozen_refine_terms = None
        self.refine_raw = None

    def register(self, runtime):
        answer = super().register(runtime)
        # Initial ready may already have retired its runtime. Refine retry can
        # construct another previous-attempt object; observe that identity too.
        if (runtime is not self.retained.runtime and runtime.session_id == self.retained.sid
                and runtime.user_id == self.retained.runtime.user_id and runtime.attempt_no == 1):
            self.restore_observations.append({"commit_completed": self.commit_completed,
                "attempt_no": runtime.attempt_no, "pcm_bytes": len(runtime.audio),
                "expiry_unchanged": runtime.retry_expires_at == self.retained.expiry})
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
                runtime = self.transferred = self.manager.runtimes[self.retained.sid]
                assert runtime is not self.retained.runtime and runtime.attempt_no == 2
                assert not runtime.processing_accepted
                if self.kind == "asr":
                    assert runtime.audio is self.retained.audio and bytes(runtime.audio) == self.retained.pcm
                    assert self.retained.runtime.audio == b""
                else:
                    assert runtime.audio == b"" and runtime.raw_text == self.retained.raw
                    assert runtime.refine_terms == self.retained.refine_terms
                    assert runtime.dictionary_version == self.retained.dictionary_version
                self.original_deadline, self.original_end = runtime.deadline, runtime.end_accepted_at
                self.refine_raw, self.frozen_refine_terms = runtime.raw_text, list(runtime.refine_terms)
                self.request_task = asyncio.current_task()
                await actual_commit()
                self.commit_completed = True
                self.transaction_after_commit = db.in_transaction()
                self.commit_connection_returned = any(identity == self.connection_id and action == "checkin"
                    for identity, action in self.pool_events[checkout_index + 1:])
                self.entered.set()
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    self.seen_cancel_count += 1
                    self.first_cancel_seen.set()
                    if self.hold_first_cancel:
                        # Only Python return handling is gated. PG already
                        # committed; do not fabricate a rollback or SQL outcome.
                        try:
                            await self.second_release.wait()
                        except asyncio.CancelledError:
                            self.seen_cancel_count += 1
                            raise
                    raise

            db.commit = commit
            try:
                yield db
            finally:
                db.commit = actual_commit


async def operation_identity(app, owner, sid, key):
    async with app.state.db.session_factory() as db:
        row = (await db.execute(text("""SELECT id,request_hash,target_type,target_id
            FROM operation_requests WHERE user_id=:u AND operation='session.retry'
            AND request_key=:k"""), {"u": owner, "k": key})).one()
        assert row[2:] == ("session", sid)
        return tuple(row)


def prepare_refine_fallback(client, monkeypatch, owner, headers, physical):
    scheduled = client.app.state.llm
    inner = scheduled.inner
    original_generate = inner.generate
    observed = []
    force_initial = True

    async def first_unavailable(request, deadline):
        scope = current_scope.get()
        assert scope is not None and scope.user_id == owner
        observed.append((scope.attempt_no, request.purpose))
        if force_initial and scope.attempt_no == 1 and request.purpose == "refine":
            return LlmResponse(parsed=None, provider=inner.provider_id, model_id=inner.model_id,
                error=ProviderError("unavailable", "controlled physical provider unavailable", retryable=False))
        return await original_generate(request, deadline)

    monkeypatch.setattr(inner, "generate", first_unavailable)
    created = create_session(client, headers, str(uuid.uuid4()))
    assert created.status_code == 201
    sid = created.json()["session_id"]
    runtime = physical.manager.runtimes[sid]
    events = dictation(client, headers, sid)
    assert events[-1]["type"] == "result.ready"
    result = events[-1]["result"]
    assert result["review_required"] is True and result["review_reason"] == "llm_unavailable"
    assert result["raw_text"] == result["refined_text"] == physical.texts[owner]
    detail = client.get(f"/api/v1/sessions/{sid}", headers=headers)
    assert detail.status_code == 200 and detail.json()["retry_kind"] == "refine"
    assert observed == [(1, "refine")]
    force_initial = False

    async def frozen():
        async with client.app.state.db.session_factory() as db:
            row = (await db.execute(text("""SELECT dictionary_version,llm_hints
                FROM sessions WHERE user_id=:u AND id=:s"""), {"u": owner, "s": sid})).one()
            calls = (await db.execute(text("""SELECT status,error_code FROM model_calls
                WHERE user_id=:u AND session_id=:s AND purpose='refine'
                ORDER BY started_at"""), {"u": owner, "s": sid})).all()
            assert [tuple(call) for call in calls] == [("failed", "unavailable")]
            return row[0], row[1]

    dictionary_version, refine_terms = client.portal.call(frozen)
    assert refine_terms and runtime.audio == b"" and runtime.retry_expires_at is None
    return SimpleNamespace(sid=sid, runtime=runtime, audio=runtime.audio, expiry=None, pcm=b"",
        raw=result["raw_text"], initial_result_id=result["result_id"],
        refine_terms=refine_terms, dictionary_version=dictionary_version, llm_calls=observed)


@pytest.mark.parametrize("kind,cancel_count", [("asr", 2), ("refine", 1)],
                         ids=["asr-double-cancel", "refine-real-fallback"])
def test_retry_commit_cancel_retains_identity_and_operation_owner(
    atomic_client, monkeypatch, record_property, kind, cancel_count,
):
    require_owned_database()
    client = atomic_client
    actor = register_and_login(client, email=f"retry-control-{kind}@example.invalid")
    owner, headers = actor["user"]["id"], auth_headers(actor)
    assert create_term(client, headers, "Kubernetes", ["k8s"]).status_code == 201
    physical = AudioFixture(client, monkeypatch, {owner: "请打开 Kubernetes 控制台。"})
    retained = (_failed_audio(client, headers, physical) if kind == "asr" else
                prepare_refine_fallback(client, monkeypatch, owner, headers, physical))

    async def exercise():
        app, manager = client.app, physical.manager
        assert manager.config.retry.audio_ttl_seconds == 600
        assert manager.config.asr.finish_timeout_ms == 10000
        assert manager.config.pipeline.stop_to_result_budget_ms == 30000
        await wait_until(lambda: retained.runtime.task is None or retained.runtime.task.done())
        assert not retained.runtime.connected
        gate = RetryReturnGate(app, retained, kind, cancel_count == 2)
        request = None
        key = str(uuid.uuid4())
        evidence = {"scope": "real PG/API/WS, synthetic PCM/physical Mock, commit-return gate",
            "kind": kind, "caller_cancels_requested": 0, "issues": []}
        gate.install()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://retry-controls") as http:
            try:
                before = await rows(app, owner, retained.sid, key)
                request = asyncio.create_task(http.post(f"/api/v1/sessions/{retained.sid}/retry",
                    headers={**headers, "Idempotency-Key": key}, json={"retry_kind": kind}))
                await wait_until(gate.entered.is_set)
                committed = await rows(app, owner, retained.sid, key)
                assert gate.commit_completed and gate.transaction_after_commit is False
                assert gate.commit_connection_returned
                assert committed["session"][:2] == ("finalizing" if kind == "asr" else "refining", 2)
                assert committed["session"][2:] == before["session"][2:]
                assert committed["attempts"] == [*before["attempts"], (2, "running", None)]
                identity = await operation_identity(app, owner, retained.sid, key)
                assert identity[1] == idempotency_hash(manager.config.idempotency_secret, "session.retry",
                    {"session_id": retained.sid, "retry_kind": kind})
                assert len(physical.opened) == 1
                assert manager._operations
                for number in range(cancel_count):
                    accepted = request.cancel()
                    evidence["caller_cancels_requested"] += int(accepted)
                    await asyncio.sleep(0)
                    if number == 0 and cancel_count == 2:
                        # Let the actual caller cancel reach either the commit
                        # return handler or a future operation join boundary.
                        await asyncio.sleep(0)
                evidence["operation_pending_before_gate_release"] = bool(manager._operations)
                evidence["caller_done_before_gate_release"] = request.done()
                assert evidence["caller_cancels_requested"] == cancel_count, "cancel control window was not exercised"
                if request.done() and not manager._operations:
                    # A correct implementation may finish a real terminal CAS
                    # before returning; do not force it to wait on an irrelevant
                    # gate. Active DB with no owner is checked below.
                    evidence["caller_left_operation_before_release"] = True
                gate.release.set()
                gate.second_release.set()
                await wait_until(request.done)
                outcome = (await asyncio.gather(request, return_exceptions=True))[0]
                evidence["caller_outcome"] = type(outcome).__name__
                if isinstance(outcome, httpx.Response):
                    assert outcome.status_code == 202
                else:
                    assert isinstance(outcome, asyncio.CancelledError)
                assert gate.transferred.end_accepted_at == gate.original_end
                assert gate.transferred.deadline == gate.original_deadline
                assert gate.original_deadline == gate.original_end + 30
                if kind == "asr":
                    if any(item["commit_completed"] and item["pcm_bytes"] > 0
                           for item in gate.restore_observations):
                        evidence["issues"].append("committed attempt2 returned PCM to previous attempt1")
                elif gate.restore_observations:
                    evidence["issues"].append("committed refine attempt2 republished previous ready runtime")
                replay = await http.post(f"/api/v1/sessions/{retained.sid}/retry",
                    headers={**headers, "Idempotency-Key": key}, json={"retry_kind": kind})
                assert replay.status_code == 200 and replay.json()["replayed"]
                assert replay.json()["current_attempt_no"] == 2
                assert await operation_identity(app, owner, retained.sid, key) == identity
                current = manager.runtimes.get(retained.sid)
                if current is not None and current.attempt_no != 2:
                    evidence["issues"].append("local current identity disagrees with committed attempt2")

                def live_owner():
                    return any(runtime is not None and runtime.attempt_no == 2
                        and runtime.task is not None and not runtime.task.done()
                        for runtime in (manager.runtimes.get(retained.sid), gate.transferred))

                latest = await rows(app, owner, retained.sid, key)
                active = latest["session"][0] in ("created", "streaming", "finalizing", "refining")
                if active and not live_owner():
                    latest = await rows(app, owner, retained.sid, key)
                    active = latest["session"][0] in ("created", "streaming", "finalizing", "refining")
                if active and not live_owner():
                    evidence["issues"].append("committed active attempt2 lost its processing owner")
                assert [item[0] for item in latest["attempts"]] == [1, 2]
                assert latest["session"][2:] == before["session"][2:]
                if kind == "refine":
                    assert len(physical.opened) == 1, "refine retry must not open ASR again"
                    assert gate.refine_raw == retained.raw and gate.frozen_refine_terms == retained.refine_terms
                    evidence["initial_fallback_from_real_pipeline"] = True
                evidence.update(db_state=latest["session"][0], live_attempt2_owner=live_owner(),
                    local_attempt=None if current is None else current.attempt_no,
                    original_deadline_unchanged=True, operation_identity_unchanged=True,
                    physical_asr_open_count=len(physical.opened))
                async with app.state.db.session_factory() as db:
                    assert await db.scalar(text("SELECT 1")) == 1
                assert not evidence["issues"], evidence["issues"]
            finally:
                gate.release.set()
                gate.second_release.set()
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
                    try:
                        async with asyncio.timeout(8):
                            response = await http.post(f"/api/v1/sessions/{retained.sid}/cancel", headers=headers)
                        assert response.status_code in (200, 409)
                    except BaseException as exc:
                        cleanup_errors.append({"stage": "business_cancel", "type": type(exc).__name__})
                finally:
                    gate.remove()
                    evidence.update(commit_completed=gate.commit_completed,
                        commit_connection_returned=gate.commit_connection_returned,
                        commit_return_cancel_count=gate.seen_cancel_count,
                        restore_observations=gate.restore_observations, cleanup_errors=cleanup_errors)
                    record_property("qa_retry_commit_controls", json.dumps(evidence, sort_keys=True))
                assert not cleanup_errors, cleanup_errors

    client.portal.call(exercise)
