"""ADMIN10 accounting only: real local HTTP adapters + PG + admin API.

Physical upstream replies are synthetic. This does not prove GPU semantics,
ASR inference, microphone/native insertion, or the ModelsView GUI.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
import uuid
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.models import ModelCall, ModelUsageDaily, SessionAttempt, SessionResult
from app.services import cleanup as cleanup_module
from tests.conftest import auth_headers
from tests.integration.test_llm_http_adapter_contract import (
    MODEL, RAW, ProtocolPeer, adapter_client, scenario,
)
from tests.test_admin import login_admin
from tests.test_sessions import create_session


TZ = ZoneInfo("Asia/Shanghai")


async def ledger_snapshot(app, owner):
    # Read authoritative real dispatch rows and persisted projections. Never
    # create calls/usage, call recorder.finished, or synthesize terminal values.
    async with app.state.db.session_factory() as db:
        calls = (await db.scalars(select(ModelCall).where(
            ModelCall.user_id == owner).order_by(ModelCall.id))).all()
        daily = (await db.scalars(select(ModelUsageDaily).where(
            ModelUsageDaily.user_id == owner).order_by(
                ModelUsageDaily.date, ModelUsageDaily.provider,
                ModelUsageDaily.model_id, ModelUsageDaily.purpose))).all()
        return {
            "calls": [{column.name: getattr(row, column.name)
                       for column in ModelCall.__table__.columns} for row in calls],
            "daily": [{column.name: getattr(row, column.name)
                       for column in ModelUsageDaily.__table__.columns} for row in daily],
        }


def expected_api(snapshot):
    # Aggregate physical-call facts independently of ModelUsageDaily and admin
    # SQL. h.ledger separately binds each of these facts to the actual HTTP
    # request/reply, including null for the 503's unknown token usage.
    grouped = {}
    for call in snapshot["calls"]:
        key = (call["started_at"].astimezone(TZ).date().isoformat(),
               call["provider"], call["model_id"], call["purpose"])
        counts = grouped.setdefault(key, Counter())
        counts.update(calls=1, successes=int(call["status"] == "succeeded"),
            failures=int(call["status"] != "succeeded"),
            unknown_calls=int(call["status"] == "unknown"),
            elapsed_ms=call["elapsed_ms"],
            processed_audio_ms=call["processed_audio_samples"] * 1000 // 16000,
            prompt_tokens_known=call["prompt_tokens"] if call["prompt_tokens"] is not None else 0,
            completion_tokens_known=call["completion_tokens"] if call["completion_tokens"] is not None else 0,
            prompt_tokens_unknown_calls=int(call["prompt_tokens"] is None),
            completion_tokens_unknown_calls=int(call["completion_tokens"] is None))
    return {"timezone": "Asia/Shanghai", "items": [
        dict(date=day, provider=provider, model_id=model, purpose=purpose, **dict(counts))
        for (day, provider, model, purpose), counts in sorted(grouped.items())
    ]}


async def content_state(app, owner, sid, result_id):
    async with app.state.db.session_factory() as db:
        attempt = await db.scalar(select(SessionAttempt).where(
            SessionAttempt.user_id == owner, SessionAttempt.session_id == sid,
            SessionAttempt.attempt_no == 1))
        result = await db.scalar(select(SessionResult).where(
            SessionResult.user_id == owner, SessionResult.session_id == sid,
            SessionResult.id == result_id))
        assert attempt is not None and result is not None
        return {"attempt_has_raw": attempt.raw_text is not None,
            "result_has_raw": result.raw_text is not None,
            "result_has_refined": result.refined_text is not None,
            "result_cleared": result.content_cleared_at is not None}


def test_tc_admin_10_actual_http_retry_usage_survives_business_replay_and_cleanup(
    adapter_client, monkeypatch, record_property,
):
    h = adapter_client
    actor = h.actor("admin10-physical")
    headers, owner = auth_headers(actor), actor["user"]["id"]
    admin = auth_headers(login_admin(h.client))
    settings = h.client.get("/api/v1/settings", headers=headers)
    assert settings.status_code == 200
    # Keep content permanent until the explicit shortening below; the periodic
    # cleanup cannot race ahead and turn the final cleanup proof into a no-op.
    permanent = h.client.patch("/api/v1/settings", headers=headers,
        json={"version": settings.json()["version"], "history_retention_days": None})
    assert permanent.status_code == 200

    replies, reason = scenario("503_retry")
    assert reason is None and len(replies) == 2
    h.upstream.install(*replies)
    request_key = str(uuid.uuid4())
    created = create_session(h.client, headers, request_key)
    assert created.status_code == 201
    sid = created.json()["session_id"]
    peer = ProtocolPeer(h.app, SimpleNamespace(headers=lambda: headers), sid, 1, RAW)
    h.peers.append(peer)
    h.client.portal.call(peer.start)
    h.end(peer)
    result = h.result(peer, actor)
    h.ledger(actor)  # actual HTTP dispatch -> ModelCall -> persisted daily
    assert len(h.upstream.records) == 2 and not h.upstream.pending and not h.upstream.errors
    assert [r["purpose"] for r in h.upstream.records] == ["refine", "refine"]
    before = h.client.portal.call(ledger_snapshot, h.app, owner)
    assert len(before["calls"]) == 2
    expected = expected_api(before)
    dates = [item["date"] for item in expected["items"]]
    params = {"date_from": min(dates), "date_to": max(dates), "user_id": owner}

    def admin_models():
        response = h.client.get("/api/admin/stats/models", params=params, headers=admin)
        assert response.status_code == 200
        assert response.json() == expected
        return response.json()

    initial_api = admin_models()
    assert sum(item["calls"] for item in initial_api["items"]) == 2
    assert sum(item["prompt_tokens_known"] for item in initial_api["items"]) == 12
    assert sum(item["completion_tokens_known"] for item in initial_api["items"]) == 4
    assert sum(item["prompt_tokens_unknown_calls"] for item in initial_api["items"]) == 1
    assert sum(item["completion_tokens_unknown_calls"] for item in initial_api["items"]) == 1
    assert sum(item["processed_audio_ms"] for item in initial_api["items"]) == 0
    assert all(item["model_id"] == MODEL and item["provider"] == h.upstream.adapter
               for item in initial_api["items"])

    for _ in range(10):
        replay = create_session(h.client, headers, request_key)
        assert replay.status_code == 200 and replay.json()["replayed"] is True
        assert replay.json()["session_id"] == sid
        assert replay.json()["current_attempt_no"] == 1
        assert replay.json()["result"]["result_id"] == result["result_id"]
        assert admin_models() == initial_api
    assert h.client.portal.call(ledger_snapshot, h.app, owner) == before
    assert len(h.upstream.records) == 2 and not h.upstream.pending and not h.upstream.errors
    assert h.client.portal.call(content_state, h.app, owner, sid, result["result_id"]) == {
        "attempt_has_raw": True, "result_has_raw": True,
        "result_has_refined": True, "result_cleared": False}

    # Only retention's wall-clock source moves; scheduler/provider deadlines,
    # model clocks, call dates and immutable ledger rows are not modified.
    cleanup_at = datetime.now(timezone.utc) + timedelta(days=8)
    with monkeypatch.context() as retention_clock:
        retention_clock.setattr(cleanup_module, "utcnow", lambda: cleanup_at)
        shortened = h.client.patch("/api/v1/settings", headers=headers,
            json={"version": permanent.json()["version"], "history_retention_days": 7})
    assert shortened.status_code == 200 and shortened.json()["history_retention_days"] == 7
    assert h.client.portal.call(content_state, h.app, owner, sid, result["result_id"]) == {
        "attempt_has_raw": False, "result_has_raw": False,
        "result_has_refined": False, "result_cleared": True}
    expired = h.client.get(f"/api/v1/sessions/{sid}", headers=headers)
    assert expired.status_code == 200 and expired.json()["content_expired"] is True
    assert expired.json()["result"] is None
    assert admin_models() == initial_api
    assert h.client.portal.call(ledger_snapshot, h.app, owner) == before
    h.ledger(actor)
    assert len(h.upstream.records) == 2 and not h.upstream.pending and not h.upstream.errors
    record_property("admin10_actual_http_usage", json.dumps({
        "adapter": h.upstream.adapter, "physical_requests": 2,
        "refine_dispatches": 2, "audit_dispatches": 0,
        "business_replays": 10, "known_prompt_tokens": 12,
        "known_completion_tokens": 4, "unknown_prompt_calls": 1,
        "unknown_completion_calls": 1, "content_before_present": True,
        "content_after_cleared": True, "immutable_calls_and_daily_unchanged": True,
        "formal_admin_api_unchanged": True, "scope": "synthetic-local-http-accounting-no-gui",
    }, sort_keys=True))
    h.evidence(record_property)
