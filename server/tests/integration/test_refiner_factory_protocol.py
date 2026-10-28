"""R2 formal main factory / API / WS / PG projection, synthetic physical providers.

Only inner Mock response shape is fault-injected. No business/provider wrapper,
deadline, DB row or result is fabricated; this is not real model/audio quality.
Run tests-only first against old main: the actual dispatch protocol oracle must
fail, then rerun unchanged after the narrowly reviewed integration patch.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import hashlib

import pytest
from sqlalchemy import func, select

from app.core.clock import loop_time
from app.models import Insertion, LearningEvent, ModelCall, ModelUsageDaily, SessionContribution, SessionResult
from app.services.inference_context import current_scope
from tests.conftest import auth_headers, register_and_login
from tests.test_sessions import create_session, dictation

EXACT_ID = "refine-r2-text-v1"
R2_VERSION = "ds-r2-20261022"
R2_SHA = "a1efa73302779e50d7d80396e3020470c4f0b9c522f794e64cf78ef3fee386f8"
AUDIT_PROMPT_SHA = "1feedf6a8888df67428921c21f6366bba09b88f9ff25f2571a33d62cae520c90"
RAW = "嗯那个，帮我加一个按钮，不要修改数据库。"
CLEAN = "帮我加一个按钮，不要修改数据库。"


def result_at_same_instant(value):
    result = dict(value)
    instant = datetime.fromisoformat(result["created_at"].replace("Z", "+00:00"))
    assert instant.tzinfo is not None and instant.utcoffset() == timedelta(0)
    result["created_at"] = instant
    return result


async def ledger(app, owner, sid):
    async with app.state.db.session_factory() as db:
        calls = (await db.scalars(select(ModelCall).where(
            ModelCall.user_id == owner, ModelCall.session_id == sid,
            ModelCall.purpose.in_(("refine", "audit")),
        ).order_by(ModelCall.started_at, ModelCall.id))).all()
        daily = (await db.scalars(select(ModelUsageDaily).where(
            ModelUsageDaily.user_id == owner, ModelUsageDaily.purpose.in_(("refine", "audit")),
        ).order_by(ModelUsageDaily.date, ModelUsageDaily.purpose))).all()
        contribution = await db.scalar(select(SessionContribution).where(
            SessionContribution.user_id == owner, SessionContribution.session_id == sid))
        counts = {model.__tablename__: int(await db.scalar(select(func.count()).select_from(model)
            .where(model.user_id == owner, model.session_id == sid)))
            for model in (SessionResult, Insertion, LearningEvent)}
        return {
            "calls": [{column.name: getattr(row, column.name) for column in ModelCall.__table__.columns}
                      for row in calls],
            "daily": [{column.name: getattr(row, column.name) for column in ModelUsageDaily.__table__.columns}
                      for row in daily],
            "contribution": {column.name: getattr(contribution, column.name)
                             for column in SessionContribution.__table__.columns},
            "counts": counts,
        }


@pytest.mark.parametrize("mode", ["normal", "unchanged-no-audit", "refine-schema-retry", "shared-retry-exhausted"])
def test_main_factory_exact_wire_formal_result_and_real_ledger(app_client, monkeypatch, mode):
    app, state = app_client.app, app_client.app.state
    pair = register_and_login(app_client, email="refine-wire@example.invalid")
    headers, owner = auth_headers(pair), pair["user"]["id"]
    raw = CLEAN if mode == "unchanged-no-audit" else RAW
    monkeypatch.setattr(state.asr, "text", raw)
    calls = []
    original = state.llm.inner.generate
    target = {"sid": None}

    async def observed(request, deadline):
        scope = current_scope.get()
        assert scope is not None and not scope.synthetic
        assert (scope.user_id, scope.session_id, scope.attempt_no) == (owner, target["sid"], 1)
        runtime = state.sessions.runtimes[target["sid"]]
        assert request.call_id and runtime.audio == b"" and runtime.asr is None
        now = loop_time()
        assert now < deadline <= runtime.deadline
        assert deadline - now <= 10
        calls.append({
            "call_id": request.call_id, "purpose": request.purpose,
            "schema_id": request.output_schema_id, "version": request.system_prompt_version,
            "schema": deepcopy(request.schema), "system_prompt": request.system_prompt,
            "output_mode": request.output_mode, "input": deepcopy(request.input),
            "attempt_deadline": runtime.deadline, "physical_deadline": deadline,
            "request_tokens": request.max_output_tokens,
        })
        answer = await original(request, deadline)
        refine_number = sum(call["purpose"] == "refine" for call in calls)
        if mode in ("refine-schema-retry", "shared-retry-exhausted") and request.purpose == "refine" and refine_number == 1:
            answer.text = " "  # Actual text validation must spend the one allowance.
        if mode == "shared-retry-exhausted" and request.purpose == "refine" and refine_number == 2:
            answer.text = " "  # Must not receive a second allowance.
        return answer

    monkeypatch.setattr(state.llm.inner, "generate", observed)
    request_id = "main-exact-wire-" + mode
    created = create_session(app_client, headers, request_id)
    assert created.status_code == 201
    sid = target["sid"] = created.json()["session_id"]
    runtime = state.sessions.runtimes[sid]
    audio = runtime.audio
    events = dictation(app_client, headers, sid)
    assert events[-1]["type"] == "result.ready"
    result = events[-1]["result"]
    fallback = mode == "shared-retry-exhausted"
    expected = raw if fallback else CLEAN
    assert result["raw_text"] == raw and result["refined_text"] == expected
    assert result["review_required"] is fallback
    assert result["review_reason"] == ("llm_invalid_output" if fallback else None)
    assert result["text_hash"] == hashlib.sha256(expected.encode("utf-8")).hexdigest()
    assert result["attempt_no"] == 1 and result["attempt_kind"] == "initial"
    assert result["dictionary_version"] == 0
    assert audio == runtime.audio == b"" and runtime.asr is None

    purposes = (["refine", "refine"] if mode in ("refine-schema-retry", "shared-retry-exhausted")
                else ["refine"])
    assert [call["purpose"] for call in calls] == purposes
    # This is the actual formal main factory path, not a direct legacy service.
    # Check after a real API/WS result so tests-only on old main cannot pass by
    # merely exercising a separate opt-in service or substituting a stub schema.
    assert all(call["schema_id"] == EXACT_ID and call["version"] == R2_VERSION for call in calls if call["purpose"] == "refine")
    assert state.sessions.refiner.edits_only_candidate is False
    assert state.sessions.refiner.protocol == "ds_r2"
    assert state.sessions.refiner.llm is state.llm and state.llm.recorder is state.recorder
    assert len({call["attempt_deadline"] for call in calls}) == 1
    assert all(call["request_tokens"] <= state.config.llm.max_output_tokens == 8192 for call in calls)
    for call in calls:
        assert call["purpose"] == "refine" and call["output_mode"] == "text"
        assert call["schema"] == {"type": "string", "minLength": 1, "maxLength": 32768}
        assert hashlib.sha256(call["system_prompt"].encode()).hexdigest() == R2_SHA
        assert call["input"] == {"transcript": raw, "confirmed_terms": []}

    snapshot = app_client.portal.call(ledger, app, owner, sid)
    assert snapshot["counts"] == {"results": 1, "insertions": 0, "learning_events": 0}
    contribution = snapshot["contribution"]
    assert contribution["counted_session"] and contribution["has_result"] and contribution["outcome"] == "ready"
    assert contribution["received_samples"] == contribution["successful_audio_samples"] == 8000
    assert contribution["fallback"] is fallback and contribution["inserted"] is False
    by_id = {row["id"]: row for row in snapshot["calls"]}
    assert set(by_id) == {call["call_id"] for call in calls}
    for index, call in enumerate(calls):
        row = by_id[call["call_id"]]
        invalid = (mode in ("refine-schema-retry", "shared-retry-exhausted") and index == 0) or (
            mode == "shared-retry-exhausted" and index == 1)
        assert (row["user_id"], row["session_id"], row["attempt_no"], row["learning_event_id"]) == (owner, sid, 1, None)
        assert (row["provider"], row["model_id"], row["purpose"]) == ("mock", "mock-llm", call["purpose"])
        assert row["status"] == ("failed" if invalid else "succeeded")
        assert row["error_code"] == ("invalid_output" if invalid else None)
        assert row["ended_at"] is not None and row["aggregated_at"] is not None
        assert row["prompt_tokens"] is row["completion_tokens"] is None
        assert row["processed_audio_samples"] == 0
        assert row["call_no"] == sum(previous["purpose"] == call["purpose"] for previous in calls[:index + 1])
    assert sum(row["calls"] for row in snapshot["daily"]) == len(calls)
    assert sum(row["successes"] for row in snapshot["daily"]) == sum(row["status"] == "succeeded" for row in snapshot["calls"])
    assert sum(row["failures"] for row in snapshot["daily"]) == sum(row["status"] == "failed" for row in snapshot["calls"])
    assert sum(row["unknown_calls"] for row in snapshot["daily"]) == 0
    assert sum(row["prompt_tokens_unknown_calls"] for row in snapshot["daily"]) == len(calls)
    assert sum(row["completion_tokens_unknown_calls"] for row in snapshot["daily"]) == len(calls)
    assert sum(row["prompt_tokens_known"] + row["completion_tokens_known"] for row in snapshot["daily"]) == 0

    fetched = app_client.get(f"/api/v1/sessions/{sid}", headers=headers)
    assert fetched.status_code == 200
    assert result_at_same_instant(fetched.json()["result"]) == result_at_same_instant(result)
    replay = create_session(app_client, headers, request_id)
    assert replay.status_code == 200 and replay.json()["replayed"] is True
    assert replay.json()["session_id"] == sid and replay.json()["current_result_id"] == result["result_id"]
    assert app_client.portal.call(ledger, app, owner, sid) == snapshot
    assert len(calls) == len(purposes)  # Read/replay caused no new physical call.
