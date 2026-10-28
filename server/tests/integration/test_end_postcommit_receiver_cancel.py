"""Real PG/ASGI end COMMIT followed by controlled receiver cancellation.

Mock physical inference, valid synthetic PCM. No state/deadline writes or
manual after_commit hook. This is not a natural TCP disconnect or SIGTERM.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
import hashlib
import json
import sys

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import VoiceSession
from tests.conftest import auth_headers, register_and_login
from tests.integration.test_capacity_admission_atomicity import AudioFixture, atomic_client
from tests.integration.test_normal_shutdown_contract import WsProbe, create_session, require_owned_database
from tests.integration.test_retry_postcommit_caller_cancel import wait_until


async def read_state(app, owner, sid):
    async with app.state.db.session_factory() as db:
        return dict((await db.execute(text("""SELECT s.state,s.error_code,s.current_attempt_no,
            s.received_samples,s.audio_complete,s.end_accepted_at,s.current_result_id,
            a.state AS attempt_state,a.error_code AS attempt_error,
            (SELECT count(*) FROM results r WHERE r.user_id=s.user_id AND r.session_id=s.id) AS results,
            (SELECT count(*) FROM model_calls c WHERE c.user_id=s.user_id AND c.session_id=s.id) AS calls
            FROM sessions s JOIN session_attempts a ON a.user_id=s.user_id AND a.session_id=s.id
              AND a.attempt_no=s.current_attempt_no WHERE s.user_id=:u AND s.id=:s"""),
            {"u": owner, "s": sid})).mappings().one())


@pytest.mark.parametrize("mode", ["receiver-cancel", "business-cancel", "abort-fence"])
def test_end_committed_receiver_cancel_preserves_handoff_unless_explicitly_cancelled(
    atomic_client, monkeypatch, record_property, mode,
):
    require_owned_database()
    client = atomic_client
    actor = register_and_login(client, email=f"end-owner-{mode}@example.invalid")
    headers, owner = auth_headers(actor), actor["user"]["id"]
    raw = "结束提交的虚构口述。"
    physical = AudioFixture(client, monkeypatch, {owner: raw})

    async def scenario():
        app, manager = client.app, physical.manager
        assert manager.config.asr.finish_timeout_ms == 10000
        assert manager.config.pipeline.stop_to_result_budget_ms == 30000
        scope = ContextVar("controlled_end_commit", default=False)
        entered, release = asyncio.Event(), asyncio.Event()
        actual_commit, actual_end = AsyncSession.commit, manager._end
        wire = close_task = receiver = commit_owner = None
        used = False
        trace = {"mode": mode, "scope": "real PG/ASGI; explicit Task.cancel or production cancel/fence",
            "gate_cancelled": False, "finishes": [], "cleanup_errors": []}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://end-owner") as http:
            created = await create_session(http, headers)
            assert created.status_code == 201
            sid = created.json()["session_id"]
            runtime = manager.runtimes[sid]
            audio, stream = runtime.audio, runtime.asr
            actual_finish = stream.finish

            async def end(current, last_seq, total_samples):
                nonlocal receiver
                if current is not runtime:
                    return await actual_end(current, last_seq, total_samples)
                receiver = asyncio.current_task()  # Exact production WS receiver.
                trace["end_entry_loop"] = asyncio.get_running_loop().time()
                token = scope.set(True)  # A new owner task inherits this scoped identity.
                try:
                    return await actual_end(current, last_seq, total_samples)
                finally:
                    trace["end_caller_exception_type"] = None if sys.exc_info()[0] is None else sys.exc_info()[0].__name__
                    scope.reset(token)

            async def commit(db):
                nonlocal used, commit_owner
                if not scope.get() or used:
                    return await actual_commit(db)
                rows = [row for row in db.identity_map.values() if isinstance(row, VoiceSession)
                    and row.user_id == owner and row.id == sid]
                assert len(rows) == 1 and rows[0].state == "finalizing" and rows[0].audio_complete
                used = True
                commit_owner = asyncio.current_task()
                await actual_commit(db)  # Real PG COMMIT. Never simulate an acknowledgement.
                assert not db.in_transaction()
                trace.update(commit_completed=True, gate_entered_loop=asyncio.get_running_loop().time(),
                    commit_owner_is_receiver=commit_owner is receiver,
                    processing_accepted_after_commit=runtime.processing_accepted,
                    runtime_ended_after_commit=runtime.ended)
                entered.set()
                try:
                    async with asyncio.timeout(8):  # Control guard, unchanged product budgets.
                        await release.wait()
                except asyncio.CancelledError:
                    trace["gate_cancelled"] = True
                    raise
                except TimeoutError:
                    trace["control_failure"] = "COMMIT_RETURN_GATE_TIMEOUT"
                    raise

            async def finish(samples, deadline):
                entry = asyncio.get_running_loop().time()
                value = await actual_finish(samples, deadline)
                trace["finishes"].append({"samples": samples, "deadline_loop": deadline,
                    "entered_loop": entry, "returned_loop": asyncio.get_running_loop().time(),
                    "text_matches_fixture": value.text == raw})
                return value

            monkeypatch.setattr(manager, "_end", end)
            monkeypatch.setattr(AsyncSession, "commit", commit)
            monkeypatch.setattr(stream, "finish", finish)
            try:
                wire = WsProbe(app, headers, sid)
                await wire.start()  # Formal ready, seq0, actual feed and ACK4000.
                assert len(audio) == 8000 and runtime.samples == 4000
                wire.drain_task = asyncio.create_task(wire.drain())
                await wire.send({"type": "websocket.receive", "text": json.dumps({
                    "type": "audio.end", "last_seq": 0, "total_samples": 4000})})
                await wait_until(entered.is_set)
                before = await read_state(app, owner, sid)  # Independent PG checkout.
                assert before["state"] == "finalizing" and before["attempt_state"] == "running"
                assert before["audio_complete"] and before["received_samples"] == 4000
                assert before["end_accepted_at"] is not None and before["current_attempt_no"] == 1
                assert before["results"] == before["calls"] == 0 and runtime.task is None
                assert manager._operations and not receiver.done() and not commit_owner.done()
                trace["committed_snapshot"] = before
                if mode == "receiver-cancel":
                    assert receiver.cancel()
                    await asyncio.sleep(0)
                    await asyncio.sleep(0)
                    trace["second_receiver_cancel_accepted"] = receiver.cancel()
                    await asyncio.sleep(0)
                elif mode == "business-cancel":
                    cancelled = await http.post(f"/api/v1/sessions/{sid}/cancel", headers=headers)
                    assert cancelled.status_code == 200 and cancelled.json()["state"] == "cancelled"
                    cancelled_state = await read_state(app, owner, sid)
                    assert cancelled_state["state"] == cancelled_state["attempt_state"] == "cancelled"
                    assert cancelled_state["results"] == cancelled_state["calls"] == 0
                    assert runtime.cancelled and not audio and runtime.asr is None
                    trace["explicit_cancel_committed"] = True
                else:
                    # Existing production abort fence. No claim of actual DB lock loss.
                    manager.begin_close(graceful=False, code="INTERNAL_ERROR")
                    close_task = asyncio.create_task(manager.close(graceful=False))
                    await wait_until(lambda: manager._closing and manager._close_task is not None)
                    assert runtime.cancelled and runtime.error_code == "INTERNAL_ERROR" and not audio
                trace.update(receiver_done_before_release=receiver.done(),
                    operation_pending_before_release=bool(manager._operations),
                    commit_owner_done_before_release=commit_owner.done())
                release.set()
                await wire.drain_task  # Actual app task/terminal socket cleanup, not a fabricated ACK.

                async with asyncio.timeout(8):
                    while True:
                        after = await read_state(app, owner, sid)
                        if after["state"] in {"ready", "failed", "cancelled", "empty"}:
                            break
                        await asyncio.sleep(.01)
                trace.update(terminal_snapshot=after, events=[{"type": item["type"],
                    "code": item.get("error", {}).get("code")} for item in wire.events],
                    close_code=wire.close_code, receiver_cancelled=receiver.cancelled())
                assert "control_failure" not in trace
                assert after["audio_complete"] and after["received_samples"] == 4000
                assert after["current_attempt_no"] == 1 and after["end_accepted_at"] == before["end_accepted_at"]
                if mode == "receiver-cancel":
                    assert receiver.cancelled() and trace["end_caller_exception_type"] == "CancelledError"
                    assert trace["second_receiver_cancel_accepted"] and not trace["gate_cancelled"]
                    assert commit_owner is not receiver and commit_owner.done() and not commit_owner.cancelled()
                    assert commit_owner.exception() is None
                    assert after["state"] == after["attempt_state"] == "ready"
                    assert after["error_code"] is after["attempt_error"] is None
                    assert after["results"] == after["calls"] == 1
                    assert runtime.end_accepted_at >= trace["end_entry_loop"]
                    assert runtime.end_accepted_at <= trace["gate_entered_loop"]
                    assert runtime.deadline == runtime.end_accepted_at + 30
                    assert len(trace["finishes"]) == 1
                    finish_info = trace["finishes"][0]
                    assert finish_info["samples"] == 4000 and finish_info["text_matches_fixture"]
                    assert finish_info["returned_loop"] < finish_info["deadline_loop"] <= runtime.end_accepted_at + 10
                    detail = await http.get(f"/api/v1/sessions/{sid}", headers=headers)
                    assert detail.status_code == 200 and detail.json()["state"] == "ready"
                    result = detail.json()["result"]
                    assert result["attempt_no"] == 1 and result["attempt_kind"] == "initial"
                    assert result["raw_text"] == result["refined_text"] == raw
                    assert result["text_hash"] == hashlib.sha256(raw.encode()).hexdigest()
                    assert not result["review_required"] and result["review_reason"] is None
                    assert 0 <= result["stop_to_result_ms"] <= 30000
                    async with app.state.db.session_factory() as db:
                        calls = (await db.execute(text("""SELECT attempt_no,purpose,status,
                            ended_at IS NOT NULL,aggregated_at IS NOT NULL FROM model_calls
                            WHERE user_id=:u AND session_id=:s"""), {"u": owner, "s": sid})).all()
                        assert [tuple(row) for row in calls] == [(1, "refine", "succeeded", True, True)]
                else:
                    expected_state = "cancelled" if mode == "business-cancel" else "failed"
                    code = None if mode == "business-cancel" else "INTERNAL_ERROR"
                    assert after["state"] == after["attempt_state"] == expected_state
                    assert after["error_code"] == after["attempt_error"] == code
                    assert after["results"] == after["calls"] == 0 and after["current_result_id"] is None
                    assert trace["finishes"] == []  # No physical ASR finish/LLM after explicit termination.
                    assert not any(item["type"] == "result.ready" for item in wire.events)
                assert physical.opened == [(owner, sid, 1)]
                assert len(physical.feeds[(sid, 1)]) == 1
                assert runtime.audio == audio == b"" and runtime.asr is None
                assert runtime.retry_expires_at is None and (runtime.task is None or runtime.task.done())
                if close_task is None:
                    close_task = asyncio.create_task(manager.close(graceful=True))
                await close_task
                await asyncio.sleep(0)
                assert not manager.runtimes and not manager._closing_runtimes and not manager._processing_tasks
                assert not manager._operations and not manager._websockets and not manager.background_tasks and not manager._auth_tasks
                assert await read_state(app, owner, sid) == after
                await wait_until(lambda: app.state.db.engine.pool.checkedout() == 0)
                trace.update(clean_close=True, pcm_bytes=0, processing_count=0,
                    pool_checkedout=app.state.db.engine.pool.checkedout(), same_terminal_after_close=True)
            finally:
                release.set()
                if close_task is None:
                    close_task = asyncio.create_task(manager.close(graceful=False))
                owned = [task for task in (close_task, wire.drain_task if wire else None,
                    wire.task if wire else None) if task is not None]
                done, pending = await asyncio.wait(owned, timeout=8)
                if pending:
                    trace["cleanup_errors"].append({"stage": "owned_join", "type": "TimeoutError", "count": len(pending)})
                for task in done:
                    if not task.cancelled() and task.exception() is not None:
                        trace["cleanup_errors"].append({"stage": "owned_join", "type": type(task.exception()).__name__})
                record_property("end_postcommit_receiver_cancel", json.dumps(trace, default=str, sort_keys=True))
                assert not trace["cleanup_errors"], trace["cleanup_errors"]

    client.portal.call(scenario)
