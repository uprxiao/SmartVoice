"""Real PG commit, then a controlled Python return error; no fake SQL outcome.

Formal ASGI HTTP/WS + synthetic PCM + Mock physical providers. This is not
COMMIT network-ACK loss, native input, real model throughput or a 600s run.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import uuid

import httpx
import pytest
from sqlalchemy import text

from tests.conftest import auth_headers, register_and_login
from tests.integration.test_capacity_admission_atomicity import (
    AudioFixture, _active, _failed_audio, atomic_client,
)
from tests.integration.test_normal_shutdown_contract import require_owned_database
from tests.integration.test_retry_postcommit_caller_cancel import (
    CommittedReturnGate, rows, wait_until,
)


class ConfirmedCommitErrorGate(CommittedReturnGate):
    def __init__(self, app, retained, inject_error):
        super().__init__(app, retained)
        self.inject_error, self.raised = inject_error, False

    @asynccontextmanager
    async def admission(self):
        # Reuse the original real flush/COMMIT, checkout/checkin and identity
        # probes. The outer wrapper raises only after that gate was released.
        first = not self.used
        async with super().admission() as db:
            if not first:
                yield db
                return
            gated_commit = db.commit

            async def commit():
                await gated_commit()
                if self.inject_error:
                    assert self.commit_completed and self.transaction_after_commit is False
                    self.raised = True
                    raise RuntimeError("controlled failure after confirmed commit")

            db.commit = commit
            try:
                yield db
            finally:
                db.commit = gated_commit


@pytest.mark.parametrize(
    "inject_error,force_stale_snapshot",
    [(False, False), (True, False), (False, True)],
    ids=["normal-return", "confirmed-return-error", "completed-after-active-read"],
)
def test_retry_confirmed_commit_never_restores_previous_attempt_pcm(
    atomic_client, monkeypatch, record_property, inject_error, force_stale_snapshot,
):
    require_owned_database()
    client = atomic_client
    actor = register_and_login(client, email="confirmed-retry-return@example.invalid")
    owner, headers = actor["user"]["id"], auth_headers(actor)
    physical = AudioFixture(client, monkeypatch, {owner: "这是提交确认后的虚构测试。"})
    retained = _failed_audio(client, headers, physical)

    async def exercise():
        app, manager = client.app, physical.manager
        assert manager.config.retry.audio_ttl_seconds == 600
        assert manager.config.asr.finish_timeout_ms == 10000
        assert manager.config.pipeline.stop_to_result_budget_ms == 30000
        await wait_until(lambda: retained.runtime.task is None or retained.runtime.task.done())
        assert not retained.runtime.connected
        gate = ConfirmedCommitErrorGate(app, retained, inject_error)
        if force_stale_snapshot:
            physical.gate_candidates.add((owner, 2))
            physical.entered, physical.release = asyncio.Event(), asyncio.Event()
        stale_probe_pending = force_stale_snapshot
        request = None
        key = str(uuid.uuid4())
        evidence = {"fault_requested": inject_error, "scope": "real PG/ASGI; Mock physical models"}
        gate.install()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://confirmed-retry-return") as http:
            try:
                before = await rows(app, owner, retained.sid, key)
                request = asyncio.create_task(http.post(f"/api/v1/sessions/{retained.sid}/retry",
                    headers={**headers, "Idempotency-Key": key}, json={"retry_kind": "asr"}))
                await wait_until(gate.entered.is_set)
                committed = await rows(app, owner, retained.sid, key)
                assert gate.commit_completed and gate.transaction_after_commit is False
                assert gate.commit_connection_returned
                assert committed["session"][:4] == ("finalizing", 2, 8000, True)
                assert committed["session"][4] == before["session"][4]
                assert committed["attempts"] == [*before["attempts"], (2, "running", None)]
                assert committed["keys"] == [("session", retained.sid)]
                runtime = gate.transferred
                lease = runtime.audio_lease
                original_end, original_budget = runtime.end_accepted_at, runtime.deadline
                assert original_budget - original_end == pytest.approx(30)
                assert runtime.retry_expires_at == lease.expires_at == retained.expiry
                assert runtime.task is None and len(physical.opened) == 1
                assert await _active(app) == 1
                evidence["durable_attempt2_verified_before_fault"] = True
                gate.release.set()
                await wait_until(request.done)
                outcome = (await asyncio.gather(request, return_exceptions=True))[0]
                evidence.update(outcome_type=type(outcome).__name__, fault_raised=gate.raised,
                    restores=list(gate.restore_observations), old_pcm_bytes=len(retained.runtime.audio),
                    current_attempt=None if manager.runtimes.get(retained.sid) is None
                    else manager.runtimes[retained.sid].attempt_no)
                if inject_error:
                    assert gate.raised and isinstance(outcome, RuntimeError)
                else:
                    assert not gate.raised and isinstance(outcome, httpx.Response)
                    assert outcome.status_code == 202
                # These must already hold before any cleanup cancellation.
                assert not gate.restore_observations, "committed retry republished old attempt"
                assert retained.runtime.audio == b"", "committed PCM returned to old attempt"
                current = manager.runtimes.get(retained.sid)
                assert current is None or current is runtime
                assert runtime.end_accepted_at == original_end and runtime.deadline == original_budget
                assert lease.expires_at == retained.expiry
                await wait_until(lambda: not manager.background_tasks)
                async with asyncio.timeout(8):
                    while True:
                        after = await rows(app, owner, retained.sid, key)
                        if stale_probe_pending:
                            # Freeze the retry during the active DB read, then
                            # complete it before checking the task owner. This
                            # is the real PG race that failed in Linux CI.
                            await asyncio.wait_for(physical.entered.wait(), 3)
                            assert after["session"][0] in ("created", "streaming", "finalizing", "refining")
                            physical.release.set()
                            await asyncio.wait_for(asyncio.shield(runtime.task), 8)
                            assert runtime.task.done()
                            stale_probe_pending = False
                            evidence["controlled_completion_after_active_read"] = True
                        if (after["session"][0] in ("created", "streaming", "finalizing", "refining")
                                and (runtime.task is None or runtime.task.done())):
                            # rows() performs several awaits: the saved session
                            # row can predate completion by the time it returns.
                            # Re-read before deciding an active retry is orphaned.
                            after = await rows(app, owner, retained.sid, key)
                        if after["session"][0] not in ("created", "streaming", "finalizing", "refining"):
                            break
                        assert runtime.task is not None and not runtime.task.done(), "active retry has no owner"
                        await asyncio.sleep(0.01)
                assert after["session"][1:] == committed["session"][1:]
                assert after["attempts"][:1] == before["attempts"]
                assert [item[0] for item in after["attempts"]] == [1, 2]
                assert after["keys"] == committed["keys"]
                detail = await http.get(f"/api/v1/sessions/{retained.sid}", headers=headers)
                assert detail.status_code == 200
                body = detail.json()
                if inject_error:
                    # Current candidate preserves the actual error and safely
                    # terminates attempt2; never label this a normal success.
                    assert after["session"][0] == after["attempts"][-1][1] == "failed"
                    assert after["attempts"][-1][2] and body["result"] is None
                    assert len(physical.opened) == 1
                else:
                    assert after["session"][0] == after["attempts"][-1][1] == "ready"
                    assert body["result"]["raw_text"] == physical.texts[owner]
                    assert body["result"]["review_required"] is False
                    assert physical.feeds[(retained.sid, 1)] == physical.feeds[(retained.sid, 2)]
                    assert len(physical.opened) == 2
                replay = await http.post(f"/api/v1/sessions/{retained.sid}/retry",
                    headers={**headers, "Idempotency-Key": key}, json={"retry_kind": "asr"})
                assert replay.status_code == 200 and replay.json()["replayed"] is True
                assert replay.json()["current_attempt_no"] == 2
                assert await rows(app, owner, retained.sid, key) == after
                assert await _active(app) == 0
                assert retained.audio == runtime.audio == b""
                assert runtime.asr is None and lease.owner is None
                assert not manager._operations
                async with app.state.db.session_factory() as db:
                    assert await db.scalar(text("SELECT 1")) == 1
                await wait_until(lambda: app.state.db.engine.pool.checkedout() == 0)
                evidence.update(terminal=after["session"][0], attempt_count=2,
                    old_attempt_unchanged=True, expiry_unchanged=True, active_slots=0,
                    original_pcm_bytes=len(retained.audio), pool_checkedout=0)
            finally:
                gate.release.set()
                if physical.release is not None:
                    physical.release.set()
                cleanup_errors = []
                try:
                    if request is not None:
                        done, pending = await asyncio.wait({request}, timeout=8)
                        if pending:
                            request.cancel()
                            done, pending = await asyncio.wait({request}, timeout=8)
                        if pending:
                            cleanup_errors.append("request_join_timeout")
                        else:
                            await asyncio.gather(request, return_exceptions=True)
                    async with asyncio.timeout(8):
                        cancelled = await http.post(f"/api/v1/sessions/{retained.sid}/cancel", headers=headers)
                    assert cancelled.status_code in (200, 409)
                except BaseException as exc:
                    cleanup_errors.append(type(exc).__name__)
                finally:
                    gate.remove()
                    evidence.update(restores=list(gate.restore_observations), cleanup_errors=cleanup_errors)
                    record_property("retry_confirmed_commit_error", json.dumps(evidence, sort_keys=True))
                assert not cleanup_errors, cleanup_errors

    client.portal.call(exercise)
