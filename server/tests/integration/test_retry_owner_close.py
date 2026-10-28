"""Manager close joins a committed retry despite repeated HTTP caller cancel.

Real isolated PG/API/WS; synthetic PCM and Mock physical providers. The gate
holds Python return after actual COMMIT. Not TCP ACK loss, OS shutdown or TTL.
Normal close drains the DB-accepted complete attempt; abort still fences it.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid

import httpx
import pytest
from sqlalchemy import text

from tests.conftest import auth_headers, register_and_login
from tests.integration.test_capacity_admission_atomicity import AudioFixture, _active, _failed_audio, atomic_client
from tests.integration.test_normal_shutdown_contract import require_owned_database
from tests.integration.test_retry_postcommit_caller_cancel import CommittedReturnGate, rows, wait_until


@pytest.mark.parametrize("graceful,code", [(True, None), (False, "INTERNAL_ERROR")],
                         ids=["normal-close", "abort-close"])
def test_retry_postcommit_caller_cancel_close_joins_operation(
    atomic_client, monkeypatch, record_property, graceful, code,
):
    require_owned_database()
    client = atomic_client
    actor = register_and_login(client, email=f"retry-close-{graceful}@example.invalid")
    owner, headers = actor["user"]["id"], auth_headers(actor)
    raw = "关闭交叉的虚构口述。"
    physical = AudioFixture(client, monkeypatch, {owner: raw})
    retained = _failed_audio(client, headers, physical)

    async def exercise():
        app, manager = client.app, physical.manager
        assert manager.config.retry.audio_ttl_seconds == 600
        assert manager.config.asr.finish_timeout_ms == 10000
        assert manager.config.pipeline.stop_to_result_budget_ms == 30000
        await wait_until(lambda: retained.runtime.task is None or retained.runtime.task.done())
        assert not retained.runtime.connected
        gate = CommittedReturnGate(app, retained)
        request = close_task = None
        key = str(uuid.uuid4())
        observed = {"scope": "manager close + real PG/API/WS + Mock; committed-return gate",
                    "graceful": graceful, "expected_code": code}
        gate.install()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://retry-close") as http:
            try:
                before = await rows(app, owner, retained.sid, key)
                async with app.state.db.session_factory() as db:
                    identity_sql = text("""SELECT id,user_id,device_id,created_login_sid,
                        client_request_id,request_hash,first_terminal_at
                        FROM sessions WHERE user_id=:u AND id=:s""")
                    original_identity = tuple((await db.execute(identity_sql,
                        {"u": owner, "s": retained.sid})).one())
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
                assert runtime.task is None  # Actual processing handoff is still held.
                observed["processing_accepted_after_commit"] = runtime.processing_accepted
                original_deadline, original_end = runtime.deadline, runtime.end_accepted_at
                assert original_deadline - original_end == pytest.approx(30)
                lease = runtime.audio_lease
                assert lease is not None and lease.owner is runtime and lease.status == "available"
                assert lease.expires_at == retained.expiry == before["session"][4].timestamp() + 600
                original_lease_deadline, original_timer = lease.deadline, lease.timer
                original_asr_deadline = manager._asr_deadline(runtime)
                assert original_asr_deadline == min(original_end + 10, original_lease_deadline)
                assert len(physical.opened) == 1 and await _active(app) == 1
                assert manager._operations

                assert request.cancel()
                await asyncio.sleep(0)
                # The owner must remain in the committed-return gate. A caller
                # response cancellation cannot be an implicit business cancel.
                observed["caller_done_after_first_cancel"] = request.done()
                observed["operation_pending_after_first_cancel"] = bool(manager._operations)
                assert not request.done() and manager._operations
                assert not gate.request_task.done()
                if not graceful:
                    # Same manager fence reason used by the existing abort path;
                    # no claim of actually losing the advisory lock in this test.
                    manager.begin_close(graceful=False, code="INTERNAL_ERROR")
                close_task = asyncio.create_task(manager.close(graceful=graceful))
                await wait_until(lambda: manager._closing and manager._close_task is not None)
                await asyncio.sleep(0)
                assert request.cancel()  # Repeated real caller cancellation while close waits.
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                observed.update(close_done_before_gate_release=close_task.done(),
                    caller_done_before_gate_release=request.done(),
                    operation_pending_before_gate_release=bool(manager._operations),
                    owner_done_before_gate_release=gate.request_task.done(),
                    runtime_state_at_close=runtime.state, runtime_code_at_close=runtime.error_code,
                    pcm_bytes_at_close=len(runtime.audio))
                assert not close_task.done() and not manager._close_task.done()
                assert not request.done() and manager._operations and not gate.request_task.done()
                # DB COMMIT already accepted attempt2. A Python return gate
                # cannot make normal shutdown discard that accepted work.
                if graceful:
                    assert runtime.state == "finalizing" and runtime.error_code is None
                    assert not runtime.cancelled and runtime.task is None
                    assert runtime.audio is retained.audio and bytes(runtime.audio) == retained.pcm
                    assert retained.runtime.audio == b""
                    assert runtime.audio_lease is lease and lease.owner is runtime
                    assert lease.status == "available" and lease.timer is original_timer
                    assert runtime.retry_expires_at == retained.expiry
                    assert manager._asr_deadline(runtime) == original_asr_deadline
                else:
                    assert runtime.state == "failed" and runtime.error_code == code
                    assert runtime.audio == b"" and retained.audio == b""
                assert lease.expires_at == retained.expiry and lease.deadline == original_lease_deadline
                assert runtime.deadline == original_deadline and runtime.end_accepted_at == original_end
                assert len(physical.opened) == 1

                gate.release.set()
                await wait_until(lambda: request.done() and close_task.done())
                outcome = (await asyncio.gather(request, return_exceptions=True))[0]
                assert isinstance(outcome, asyncio.CancelledError)
                await close_task  # Must succeed; do not swallow persistence/close failures.
                await asyncio.sleep(0)  # Existing done callbacks may discard finished auth tasks.
                assert gate.request_task.done() and not gate.request_task.cancelled()
                # The parent has already observed this exception. Reading its
                # type adds only safe lifecycle evidence, never exception text.
                owner_error = gate.request_task.exception()
                observed["owner_exception_type"] = None if owner_error is None else type(owner_error).__name__
                if graceful:
                    assert owner_error is None
                    accepted, created = gate.request_task.result()
                    assert created is True and accepted["session_id"] == retained.sid
                    assert accepted["current_attempt_no"] == 2
                    assert accepted["state"] in {"finalizing", "refining", "ready"}
                else:
                    assert type(owner_error).__name__ == "SessionError"
                    assert owner_error.status == 503 and owner_error.code == "SERVICE_UNAVAILABLE"
                assert runtime.deadline == original_deadline and runtime.end_accepted_at == original_end
                assert lease.expires_at == retained.expiry and lease.deadline == original_lease_deadline
                assert lease.owner is None and lease.timer is None
                after = await rows(app, owner, retained.sid, key)
                expected_state = "ready" if graceful else "failed"
                assert after["session"][:4] == (expected_state, 2, 8000, True)
                assert after["session"][4] == before["session"][4]
                assert after["attempts"] == [*before["attempts"], (2, expected_state, code)]
                assert after["keys"] == [("session", retained.sid)]
                assert await _active(app) == 0
                async with app.state.db.session_factory() as db:
                    persisted = (await db.execute(text("""SELECT error_code,asr_hints,llm_hints
                        FROM sessions WHERE user_id=:u AND id=:s"""),
                        {"u": owner, "s": retained.sid})).one()
                    assert tuple((await db.execute(identity_sql,
                        {"u": owner, "s": retained.sid})).one()) == original_identity
                    results = (await db.execute(text("""SELECT r.id,r.attempt_no,r.raw_text,
                        r.refined_text,r.text_hash,r.review_required,r.review_reason,r.stop_to_result_ms,
                        s.current_result_id,a.raw_text AS attempt_raw
                        FROM results r JOIN sessions s ON s.user_id=r.user_id AND s.id=r.session_id
                        JOIN session_attempts a ON a.user_id=r.user_id AND a.session_id=r.session_id
                          AND a.attempt_no=r.attempt_no
                        WHERE r.user_id=:u AND r.session_id=:s"""),
                        {"u": owner, "s": retained.sid})).all()
                    calls = (await db.execute(text("""SELECT attempt_no,purpose,status,
                        ended_at IS NOT NULL AS ended,aggregated_at IS NOT NULL AS aggregated
                        FROM model_calls WHERE user_id=:u AND session_id=:s ORDER BY started_at,id"""),
                        {"u": owner, "s": retained.sid})).all()
                    if graceful:
                        assert persisted.error_code is None and len(results) == 1
                        result = results[0]
                        assert result.attempt_no == 2 and result.id == result.current_result_id
                        assert result.raw_text == result.attempt_raw == result.refined_text == raw
                        assert result.text_hash == hashlib.sha256(raw.encode()).hexdigest()
                        assert not result.review_required and result.review_reason is None
                        assert 0 <= result.stop_to_result_ms <= 30000
                        # This unchanged fictional raw needs refine only. No
                        # filler/term/substantive change means no audit call.
                        assert [tuple(row) for row in calls] == [(2, "refine", "succeeded", True, True)]
                    else:
                        assert tuple(persisted) == (code, None, None)
                        assert results == [] and calls == []
                    observed.update(result_count=len(results), model_call_count=len(calls),
                        actual_refine_succeeded=bool(graceful and calls),
                        result_text_matches_fixture=bool(graceful and results))
                    assert await db.scalar(text("SELECT 1")) == 1
                assert manager.runtimes == {} and manager._closing_runtimes == {}
                assert not manager._operations and not manager._websockets and not manager.background_tasks
                assert not manager._auth_tasks
                assert not manager._processing_tasks
                assert retained.runtime.audio == b"" and retained.audio == b"" and runtime.audio == b""
                assert runtime.asr is None and runtime.retry_expires_at is None
                assert runtime.task is None or runtime.task.done()
                expected_opens = [(owner, retained.sid, 1)] + ([(owner, retained.sid, 2)] if graceful else [])
                assert physical.opened == expected_opens
                if graceful:
                    assert physical.feeds[(retained.sid, 2)] == [
                        (0, retained.pcm[:8000]), (4000, retained.pcm[8000:])]
                else:
                    assert (retained.sid, 2) not in physical.feeds  # No late abort dispatch/feed.
                assert not any(item["commit_completed"] and item["pcm_bytes"] > 0
                               for item in gate.restore_observations)
                # Lifespan health checks still run here; wait for a naturally
                # idle pool rather than mislabel a legitimate transient checkout.
                await wait_until(lambda: app.state.db.engine.pool.checkedout() == 0)
                observed.update(db_state=after["session"][0], current_attempt=after["session"][1],
                    attempt2_code=after["attempts"][-1][2], physical_asr_open_count=len(physical.opened),
                    operation_count=len(manager._operations), background_count=len(manager.background_tasks),
                    pool_checkedout_at_end=app.state.db.engine.pool.checkedout(),
                    caller_outcome=type(outcome).__name__, exact_deadline_unchanged=True,
                    close_succeeded=True, fresh_query_succeeded=True)
            finally:
                gate.release.set()
                cleanup_errors = []
                try:
                    if close_task is None:
                        close_task = asyncio.create_task(manager.close(graceful=False))
                    if request is not None and not request.done() and not request.cancelling():
                        request.cancel()
                    owned = [task for task in (request, close_task) if task is not None]
                    done, pending = await asyncio.wait(owned, timeout=8)
                    if pending:
                        cleanup_errors.append({"stage": "owned_join", "type": "TimeoutError", "count": len(pending)})
                    for task in done:
                        if task.cancelled():
                            continue
                        error = task.exception()
                        if error is not None:
                            cleanup_errors.append({"stage": "close" if task is close_task else "request",
                                                   "type": type(error).__name__})
                finally:
                    gate.remove()
                    observed.update(commit_completed=gate.commit_completed,
                        commit_connection_returned=gate.commit_connection_returned,
                        restore_observations=gate.restore_observations, cleanup_errors=cleanup_errors)
                    record_property("retry_owner_close", json.dumps(observed, sort_keys=True))
                assert not cleanup_errors, cleanup_errors

    client.portal.call(exercise)
