"""Real lifespan/PG cleanup recovery, with synthetic PCM/providers only.

Does not prove model quality, Desktop insertion, or persistent daily-table rebuild.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.conftest import SERVER_ROOT, TEST_URL, auth_headers, execute_sql, wait_for_readiness
from tests.cases.test_dict_cases import key, new_user
from tests.cases.test_learn_cases import set_classifier, set_text, submit
from tests.cases.test_stat_cases import claim_and_receipt, uid
from tests.test_sessions import create_session, dictation


async def wait_signal(signal, seconds=6):
    async with asyncio.timeout(seconds):
        await signal.wait()


async def control_for(app):
    async with asyncio.timeout(2):
        while not hasattr(app.state, "test_cleanup_control"):
            await asyncio.sleep(0.001)
    return app.state.test_cleanup_control


async def snapshot(app, user_id):
    """Only hashes, metadata and nullness; never return fixture body text."""
    queries = {
        "contributions": "SELECT md5(COALESCE(jsonb_agg(to_jsonb(t) ORDER BY t.session_id)::text,'[]')) FROM session_contributions t WHERE user_id=:u",
        "terms": "SELECT md5(COALESCE(jsonb_agg(to_jsonb(t) ORDER BY t.id)::text,'[]')) FROM dictionary_terms t WHERE user_id=:u",
        "dictionary_state": "SELECT md5(COALESCE(jsonb_agg(to_jsonb(t))::text,'[]')) FROM dictionary_state t WHERE user_id=:u",
        "session_metadata": "SELECT md5(COALESCE(jsonb_agg(to_jsonb(t)-'asr_hints'-'llm_hints'-'content_cleared_at' ORDER BY t.id)::text,'[]')) FROM sessions t WHERE user_id=:u",
        "attempt_metadata": "SELECT md5(COALESCE(jsonb_agg(to_jsonb(t)-'raw_text' ORDER BY t.id)::text,'[]')) FROM session_attempts t WHERE user_id=:u",
        "result_metadata": "SELECT md5(COALESCE(jsonb_agg(to_jsonb(t)-'raw_text'-'refined_text'-'content_cleared_at' ORDER BY t.id)::text,'[]')) FROM results t WHERE user_id=:u",
        "learning_metadata": "SELECT md5(COALESCE(jsonb_agg(to_jsonb(t)-'before_text'-'after_text'-'left_context'-'right_context'-'evidence'-'term_before'-'content_cleared_at'-'status'-'reason' ORDER BY t.id)::text,'[]')) FROM learning_events t WHERE user_id=:u",
        "insertions": "SELECT md5(COALESCE(jsonb_agg(to_jsonb(t) ORDER BY t.id)::text,'[]')) FROM insertions t WHERE user_id=:u",
        "operations": "SELECT md5(COALESCE(jsonb_agg(to_jsonb(t) ORDER BY t.id)::text,'[]')) FROM operation_requests t WHERE user_id=:u",
    }
    counts = {
        "session_count": "SELECT count(*) FROM sessions WHERE user_id=:u",
        "session_uncleared": "SELECT count(*) FROM sessions WHERE user_id=:u AND content_cleared_at IS NULL",
        "session_hints": "SELECT count(*) FROM sessions WHERE user_id=:u AND (asr_hints IS NOT NULL OR llm_hints IS NOT NULL)",
        "attempt_text": "SELECT count(*) FROM session_attempts WHERE user_id=:u AND raw_text IS NOT NULL",
        "result_text": "SELECT count(*) FROM results WHERE user_id=:u AND (raw_text IS NOT NULL OR refined_text IS NOT NULL)",
        "result_uncleared": "SELECT count(*) FROM results WHERE user_id=:u AND content_cleared_at IS NULL",
        "learning_count": "SELECT count(*) FROM learning_events WHERE user_id=:u",
        "learning_uncleared": "SELECT count(*) FROM learning_events WHERE user_id=:u AND content_cleared_at IS NULL",
        "learning_text": "SELECT count(*) FROM learning_events WHERE user_id=:u AND (before_text IS NOT NULL OR after_text IS NOT NULL OR left_context IS NOT NULL OR right_context IS NOT NULL)",
        "learning_evidence": "SELECT count(*) FROM learning_events WHERE user_id=:u AND evidence IS NOT NULL AND evidence::text <> 'null'",
        "learning_undo_snapshot": "SELECT count(*) FROM learning_events WHERE user_id=:u AND term_before IS NOT NULL AND term_before::text <> 'null'",
    }
    async with app.state.db.session_factory() as db:
        answer = {name: await db.scalar(text(sql), {"u": user_id}) for name, sql in queries.items()}
        answer.update({name: await db.scalar(text(sql), {"u": user_id}) for name, sql in counts.items()})
        answer["learning_states"] = [tuple(row) for row in (await db.execute(text(
            "SELECT id,status,reason FROM learning_events WHERE user_id=:u ORDER BY id"
        ), {"u": user_id})).all()]
        return answer


def stats(client, headers, day):
    response = client.get("/api/v1/stats/me", headers=headers,
                          params={"date_from": day, "date_to": day})
    assert response.status_code == 200
    return response.json()


def finished_learning(client, headers, event_id):
    # Poll status internally at 200 ms, then do one API read; no GET-rate storm.
    async def wait():
        from app.models import LearningEvent
        async with asyncio.timeout(6):
            while True:
                async with client.app.state.db.session_factory() as db:
                    row = await db.get(LearningEvent, event_id)
                    if row is not None and row.status != "pending":
                        break
                await asyncio.sleep(.2)
    client.portal.call(wait)
    response = client.get(f"/api/v1/learning-events/{event_id}", headers=headers)
    assert response.status_code == 200
    return response.json()


def seed_event(client, monkeypatch, headers, *, label, raw, before, after, suggested):
    set_classifier(client, monkeypatch,
                   decision="suggest" if suggested else "term", ambiguous=suggested)
    set_text(client, raw)
    created = create_session(client, headers, label)
    assert created.status_code == 201
    session = created.json()
    events = dictation(client, headers, session["session_id"])
    assert events[-1]["type"] == "result.ready"
    result = events[-1]["result"]
    insertion_id = claim_and_receipt(client, headers, session, result)
    response = submit(client, headers, {**session, "insertion_id": insertion_id},
                      result, before, after)
    assert response.status_code == 202
    learned = finished_learning(client, headers, response.json()["event_id"])
    assert learned["status"] == ("suggested" if suggested else "learned")
    return SimpleNamespace(session=session, result=result, event=learned,
                           insertion_id=insertion_id, raw=raw)


def assert_barriers(client, headers, learned, suggested):
    page = client.get("/api/v1/history", headers=headers)
    assert page.status_code == 200 and page.json()["items"] == []
    for item in (learned, suggested):
        session_id = item.session["session_id"]
        detail = client.get(f"/api/v1/sessions/{session_id}", headers=headers)
        assert detail.status_code == 200
        assert detail.json()["content_expired"] is True
        assert detail.json()["result"] is None
        assert item.raw not in detail.text
        claim = client.post(f"/api/v1/sessions/{session_id}/insertions/claim", headers=headers,
            json={"result_id": item.result["result_id"], "insertion_id": key(),
                  "text_hash": item.result["text_hash"], "review_confirmed": True})
        assert claim.status_code == 410 and claim.json()["error"]["code"] == "CONTENT_EXPIRED"
        retry = client.post(f"/api/v1/sessions/{session_id}/retry",
            headers={**headers, "Idempotency-Key": key()}, json={"retry_kind": "refine"})
        assert retry.status_code == 410 and retry.json()["error"]["code"] == "CONTENT_EXPIRED"
    undo = client.post(f"/api/v1/learning-events/{learned.event['event_id']}/undo",
                      headers={**headers, "Idempotency-Key": key()})
    assert undo.status_code == 410 and undo.json()["error"]["code"] == "CONTENT_EXPIRED"
    confirm = client.post(f"/api/v1/learning-events/{suggested.event['event_id']}/confirm",
                         headers={**headers, "Idempotency-Key": key()})
    assert confirm.status_code == 409 and confirm.json()["error"]["code"] == "LEARNING_CONFLICT"
    assert confirm.json()["error"]["details"]["reason"] == "CONTENT_EXPIRED"


def test_lifespan_cleanup_recovers_real_pg_failure_and_preserves_expired_barriers(
    clean_database, monkeypatch, caplog,
):
    for name, value in {
        "SVI_DATABASE_URL": TEST_URL, "SVI_CONFIG": str(SERVER_ROOT / "config/app.yaml"),
        "SVI_INSTANCE_LOCK": "0", "SVI_ENVIRONMENT": "development",
        "SVI_ASR_PROVIDER": "mock", "SVI_LLM_PROVIDER": "mock",
    }.items():
        monkeypatch.setenv(name, value)
    from app import main as main_module
    from app.services import cleanup as cleanup_module

    base = main_module.load_config(str(SERVER_ROOT / "config/app.yaml"))
    config = base.model_copy(update={"cleanup": base.cleanup.model_copy(update={
        "history_interval_seconds": 5,
    })})
    monkeypatch.setattr(main_module, "load_config", lambda *args, **kwargs: config)
    real_loop, real_history = cleanup_module.cleanup_loop, cleanup_module.cleanup_history
    controllers = []

    async def lifespan_loop(app):
        control = SimpleNamespace(task=asyncio.current_task(), start=asyncio.Event(),
            failed=asyncio.Event(), retry_entered=asyncio.Event(), allow_recovery=asyncio.Event(),
            recovered=asyncio.Event(), repeated=asyncio.Event(), calls=0, cleared=[],
            sqlstate=None, error_type=None, cancelled=False, stopped=False)
        controllers.append(control)
        app.state.test_cleanup_control = control
        try:
            await control.start.wait()
            await real_loop(app)
        except asyncio.CancelledError:
            control.cancelled = True
            raise
        finally:
            control.stopped = True

    async def fault_then_history(app):
        control = app.state.test_cleanup_control
        control.calls += 1
        if control.calls == 1:
            try:
                async with app.state.db.session_factory() as db:
                    await db.execute(text("SELECT 1 / 0"))
            except DBAPIError as error:
                # Capture only SQLSTATE; no SQL error text, body or DSN.
                control.sqlstate = getattr(error.orig, "sqlstate", None)
                control.error_type = type(error).__name__
                control.failed.set()
                raise
            raise AssertionError("PostgreSQL division by zero unexpectedly succeeded")
        if control.calls == 2:
            control.retry_entered.set()
            await control.allow_recovery.wait()
        cleared = await real_history(app)
        control.cleared.append(cleared)
        (control.recovered if control.calls == 2 else control.repeated).set()
        return cleared

    monkeypatch.setattr(main_module, "cleanup_loop", lifespan_loop)
    monkeypatch.setattr(cleanup_module, "cleanup_history", fault_then_history)
    caplog.set_level(logging.WARNING, logger="smartvoiceinput.cleanup")
    with TestClient(main_module.create_app()) as client:
        wait_for_readiness(client)
        assert client.get("/health/ready").status_code == 200
        control = client.portal.call(control_for, client.app)
        tokens = new_user(client, "cleanup-worker@example.com", "cleanup-worker-device")
        headers, user_id = auth_headers(tokens), uid(tokens)
        # Existing correct term makes the learned event carry a real undo snapshot.
        term = client.post("/api/v1/dictionary/terms", headers={**headers, "Idempotency-Key": key()},
                           json={"term": "Claude", "aliases": [], "scope": "default"})
        assert term.status_code == 201
        learned = seed_event(client, monkeypatch, headers, label="cleanup-worker-learned",
            raw="请用 cloud 保存", before="cloud", after="Claude", suggested=False)
        suggested = seed_event(client, monkeypatch, headers, label="cleanup-worker-suggested",
            raw="请使用 SQL lite 数据库", before="SQL lite", after="SQLite", suggested=True)
        settings = client.get("/api/v1/settings", headers=headers).json()
        changed = client.patch("/api/v1/settings", headers=headers,
                               json={"version": settings["version"], "history_retention_days": 7})
        assert changed.status_code == 200
        execute_sql("UPDATE sessions SET first_terminal_at=now()-interval '8 days' WHERE user_id=$1", user_id)
        day = dt.datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        before = client.portal.call(snapshot, client.app, user_id)
        original_stats = stats(client, headers, day)
        assert before["session_count"] == before["session_uncleared"] == 2
        assert before["session_hints"] == before["attempt_text"] == before["result_text"] == 2
        assert before["learning_count"] == before["learning_text"] == before["learning_evidence"] == 2
        assert before["learning_undo_snapshot"] == 1
        assert original_stats["totals"]["learning_events"] == 1

        client.portal.call(control.start.set)
        client.portal.call(wait_signal, control.failed)
        # Wait until the actual loop's next tick entered retry. No manual cleanup call.
        client.portal.call(wait_signal, control.retry_entered)
        assert control.sqlstate == "22012" and not control.task.done()
        assert control.calls == 2 and control.cleared == []
        assert_barriers(client, headers, learned, suggested)
        assert client.portal.call(snapshot, client.app, user_id) == before
        assert stats(client, headers, day) == original_stats

        client.portal.call(control.allow_recovery.set)
        client.portal.call(wait_signal, control.recovered)
        after = client.portal.call(snapshot, client.app, user_id)
        for name in ("session_uncleared", "session_hints", "attempt_text", "result_text",
                     "result_uncleared", "learning_uncleared", "learning_text", "learning_evidence",
                     "learning_undo_snapshot"):
            assert after[name] == 0, name
        for name in ("contributions", "terms", "dictionary_state", "session_metadata", "attempt_metadata",
                     "result_metadata", "learning_metadata", "insertions", "operations"):
            assert after[name] == before[name], name
        assert after["session_count"] == after["learning_count"] == 2
        states = {row[0]: row[1:] for row in after["learning_states"]}
        assert states[learned.event["event_id"]] == ("learned", None)
        assert states[suggested.event["event_id"]] == ("failed", "CONTENT_EXPIRED")
        assert stats(client, headers, day) == original_stats

        # The real loop, after its normal five-second success interval, repeats.
        client.portal.call(wait_signal, control.repeated, 8)
        assert control.cleared[:2] == [2, 0]
        assert client.portal.call(snapshot, client.app, user_id) == after
        assert stats(client, headers, day) == original_stats
        failures = [record for record in caplog.records if record.name == "smartvoiceinput.cleanup"]
        assert len(failures) == 1
        failure = failures[0]
        assert failure.code == "CLEANUP_JOB_FAILED" and failure.phase == "history"
        assert failure.exception_type == control.error_type and failure.exc_info is None
        assert "SELECT" not in failure.getMessage() and "postgresql" not in failure.getMessage()
        assert learned.raw not in caplog.text and suggested.raw not in caplog.text
    assert control.stopped and control.cancelled and control.task.cancelled()

    # Same isolated DB, new formal lifespan: already-cleared content stays absent.
    with TestClient(main_module.create_app()) as restarted:
        wait_for_readiness(restarted)
        assert restarted.get("/health/ready").status_code == 200
        restarted_control = restarted.portal.call(control_for, restarted.app)
        assert restarted.portal.call(snapshot, restarted.app, user_id) == after
        assert stats(restarted, headers, day) == original_stats
        page = restarted.get("/api/v1/history", headers=headers)
        assert page.status_code == 200 and page.json()["items"] == []
        for item in (learned, suggested):
            result = restarted.get(f"/api/v1/sessions/{item.session['session_id']}", headers=headers)
            assert result.status_code == 200 and result.json()["result"] is None
    assert restarted_control.stopped and restarted_control.cancelled and restarted_control.task.cancelled()
    assert len(controllers) == 2
