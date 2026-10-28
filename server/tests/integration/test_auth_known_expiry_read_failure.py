"""Controlled PG auth-read timeout vs known signed JWT expiry.

Uses formal HTTP/ASGI WS, real isolated PostgreSQL and the production watchdog /
batched reader. The sole fault is pg_sleep(2) inside one target auth fetch, still
bounded by the original one-second read deadline. No clock, expiry, poll interval,
model budget or authentication result is mutated. Physical ASR is Mock; no end
is sent. This is not a diagnosis of the historical AUTH19 full-run failure.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
import json

import httpx
import pytest
from sqlalchemy import event, func, select, text

from app.core.security import create_access_token, decode_access_token, utcnow
from app.models import (
    Insertion, LearningEvent, ModelCall, SessionAttempt, SessionContribution,
    SessionResult, VoiceSession,
)
from tests.conftest import auth_headers, register_and_login
from tests.integration.test_normal_shutdown_contract import WsProbe, create_session, safe_status


@pytest.mark.parametrize("expiry_case", ["cross_exp", "still_valid"])
def test_auth_read_timeout_respects_known_jwt_expiry(
    app_client, monkeypatch, record_property, expiry_case,
):
    pair = register_and_login(app_client, f"known-exp-{expiry_case}@example.invalid")
    app = app_client.app
    config = app.state.config
    decoded = decode_access_token(config.auth, config.jwt_secret, pair["access_token"])
    facts = {"case": expiry_case, "fault": "one_target_pg_sleep_2_seconds",
             "auth_budget_seconds": 1, "close_budget_seconds": 6.5,
             "query_dispatched": False, "query_cancelled": False,
             "fetch_finished": False}

    async def run():
        manager = app.state.sessions
        assert config.asr.provider == config.llm.provider == "mock"
        assert config.asr.finish_timeout_ms == config.llm.call_timeout_ms == 10000
        assert config.pipeline.stop_to_result_budget_ms == 30000
        assert app.state.ready and not app.state.instance_fenced
        loop = asyncio.get_running_loop()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://testserver") as http:
            created = await create_session(http, auth_headers(pair))
            assert created.status_code == 201, safe_status(created)
            session_id = created.json()["session_id"]
            runtime = manager.runtimes[session_id]
            create_expiry = runtime.ctx.expires_at
            if expiry_case == "cross_exp":
                token, _ = create_access_token(config.auth, config.jwt_secret,
                    pair["user"]["id"], decoded["sid"], "user", ttl=timedelta(seconds=3))
            else:
                token = pair["access_token"]
            token_exp = decode_access_token(config.auth, config.jwt_secret, token)["exp"]
            wire = WsProbe(app, {"Authorization": "Bearer " + token}, session_id)
            original_fetch = manager._auth_reads._fetch
            fetch_finished = asyncio.Event()
            armed = False
            pcm_ref = asr_ref = None
            started = loop.time()
            sql = "SELECT pg_sleep(2)"

            def observe_dispatch(conn, cursor, statement, parameters, context, executemany):
                # No SQL/parameters/identity/credentials are copied into facts.
                if armed and statement == sql:
                    assert not facts["query_dispatched"]
                    facts["query_dispatched"] = True
                    facts["dispatch_remaining_ms"] = (token_exp - utcnow().timestamp()) * 1000

            async def one_slow_fetch(keys):
                nonlocal armed
                remaining = token_exp - utcnow().timestamp()
                eligible = (0 < remaining <= 1 if expiry_case == "cross_exp" else remaining > 5)
                if not armed and eligible and any(key.session_id == session_id for key in keys):
                    armed = True
                    facts["fetch_entry_remaining_ms"] = remaining * 1000
                    query_started = loop.time()
                    try:
                        # Real PG work, not a fabricated auth snapshot. Both
                        # checkout and cancellation/release retain the read budget.
                        async with manager.db.session_factory() as db:
                            await db.execute(text(sql))
                        facts["unexpected_query_returned"] = True
                    except asyncio.CancelledError:
                        facts["query_cancelled"] = True
                        facts["cancel_after_exp_ms"] = (utcnow().timestamp() - token_exp) * 1000
                        raise
                    finally:
                        facts["fetch_elapsed_ms"] = (loop.time() - query_started) * 1000
                        facts["fetch_finished"] = True
                        fetch_finished.set()
                return await original_fetch(keys)

            event.listen(manager.db.engine.sync_engine, "before_cursor_execute", observe_dispatch)
            try:
                async with asyncio.timeout_at(started + 6.5):
                    await wire.start()  # Actual authenticate replaces create's context.
                    assert runtime.ctx.sid == decoded["sid"]
                    assert runtime.ctx.expires_at.timestamp() == token_exp
                    if expiry_case == "cross_exp":
                        assert runtime.ctx.expires_at < create_expiry
                    pcm_ref, asr_ref = runtime.audio, runtime.asr
                    assert len(pcm_ref) == 8000 and asr_ref is not None
                    monkeypatch.setattr(manager._auth_reads, "_fetch", one_slow_fetch)
                    while True:
                        message = await wire.outgoing.get()
                        if message["type"] == "websocket.close":
                            wire.close_code = message["code"]
                            break
                        assert message["type"] == "websocket.send"
                        payload = json.loads(message["text"])
                        assert payload["session_id"] == session_id
                        wire.events.append(payload)
                    facts["close_elapsed_ms"] = (loop.time() - started) * 1000
                    facts["close_after_exp_ms"] = (utcnow().timestamp() - token_exp) * 1000
                    await wire.task
                # Separate observation/cleanup guard, never additional time for
                # the above unchanged 6.5-second AUTH19 close assertion.
                async with asyncio.timeout(5):
                    await fetch_finished.wait()
                    while manager.background_tasks:
                        await asyncio.gather(*(asyncio.shield(task)
                                               for task in tuple(manager.background_tasks)))
                    async with manager.db.session_factory() as db:
                        row = (await db.execute(select(VoiceSession).where(
                            VoiceSession.user_id == pair["user"]["id"],
                            VoiceSession.id == session_id))).scalar_one()
                        attempt = (await db.execute(select(SessionAttempt).where(
                            SessionAttempt.user_id == row.user_id,
                            SessionAttempt.session_id == session_id,
                            SessionAttempt.attempt_no == 1))).scalar_one()
                        contribution = (await db.execute(select(SessionContribution).where(
                            SessionContribution.user_id == row.user_id,
                            SessionContribution.session_id == session_id))).scalar_one()
                        counts = {}
                        for model in (SessionResult, Insertion, LearningEvent, ModelCall):
                            counts[model.__tablename__] = await db.scalar(select(func.count()).select_from(model).where(
                                model.user_id == row.user_id, model.session_id == session_id))
                        facts["persisted"] = {"state": row.state, "error_code": row.error_code,
                            "received_samples": row.received_samples, "audio_complete": row.audio_complete,
                            "has_result": row.current_result_id is not None,
                            "attempt_state": attempt.state, "attempt_error_code": attempt.error_code,
                            "attempt_raw_is_none": attempt.raw_text is None,
                            "contribution_outcome": contribution.outcome,
                            "successful_samples": contribution.successful_audio_samples,
                            "side_effect_counts": counts}
                facts.update({"close_code": wire.close_code,
                    "error_codes": [item["error"]["code"] for item in wire.events if item["type"] == "error"],
                    "result_event_count": sum(item["type"] == "result.ready" for item in wire.events),
                    "runtime_state": runtime.state, "pcm_bytes": len(pcm_ref),
                    "asr_cancelled": asr_ref.cancelled, "background_failed": manager._background_failed})
            finally:
                monkeypatch.setattr(manager._auth_reads, "_fetch", original_fetch)
                event.remove(manager.db.engine.sync_engine, "before_cursor_execute", observe_dispatch)
                if wire.task is not None and not wire.task.done():
                    await wire.send({"type": "websocket.disconnect", "code": 1000})
                    async with asyncio.timeout(5):
                        await asyncio.shield(wire.task)

    try:
        app_client.portal.call(run)
    finally:
        record_property("known_expiry_auth_read", json.dumps(facts, sort_keys=True))

    # Establish the fault really ran before asserting its classification.
    assert facts["query_dispatched"] and facts["query_cancelled"] and facts["fetch_finished"]
    assert not facts.get("unexpected_query_returned", False)
    if expiry_case == "cross_exp":
        assert 0 < facts["dispatch_remaining_ms"] <= 1000
        assert facts["cancel_after_exp_ms"] >= 0 and facts["close_after_exp_ms"] >= 0
        expected_code, expected_close = "AUTH_INVALID", 4401
    else:
        assert facts["dispatch_remaining_ms"] > 5000 and facts["close_after_exp_ms"] < 0
        expected_code, expected_close = "INTERNAL_ERROR", 1011
    assert facts["close_elapsed_ms"] <= 6500
    assert facts["close_code"] == expected_close, facts
    assert facts["error_codes"] and set(facts["error_codes"]) == {expected_code}, facts
    assert facts["persisted"] == {
        "state": "failed", "error_code": expected_code, "received_samples": 4000,
        "audio_complete": False, "has_result": False, "attempt_state": "failed",
        "attempt_error_code": expected_code, "attempt_raw_is_none": True,
        "contribution_outcome": "failed", "successful_samples": 0,
        "side_effect_counts": {"results": 0, "insertions": 0, "learning_events": 0, "model_calls": 0},
    }, facts
    assert facts["runtime_state"] == "failed" and facts["pcm_bytes"] == 0
    assert facts["asr_cancelled"] and not facts["background_failed"]
    assert facts["result_event_count"] == 0
