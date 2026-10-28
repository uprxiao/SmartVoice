"""Draft real-PostgreSQL projection regressions; author has NOT executed them.

Providers/classification/PCM are explicitly synthetic. These tests establish
transactional accounting, not real ASR/LLM accuracy or complete NFR-11 acceptance.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from app.models import User, VoiceSession
from app.services import learning_service, stats_service
from tests.cases.test_learn_cases import (
    prepare_inserted, set_classifier, set_text, submit, wait_event,
)
from tests.cases.test_stat_cases import add_contribution, uid
from tests.conftest import (
    SERVER_ROOT, TEST_URL, auth_headers, execute_sql, register_and_login, wait_for_readiness,
)
from tests.ids import fixture_uuid
from tests.integration.test_stats_pg_recovery import (
    PgFault, _assert_safe_failure, _owner, _wait_for_lifespan_readiness,
    stats_client,
)
from tests.test_history_stats import run_session
from tests.test_insertions_learning import claim, receipt

# Independent expected field list, not derived from implementation constants.
FIELDS = (
    "sessions", "received_audio_samples", "result_sessions", "failed_sessions",
    "empty_sessions", "cancelled_sessions", "chars", "successful_audio_samples",
    "fallback_sessions", "inserted_sessions", "learning_events",
)
DAY = dt.date(2026, 10, 16)
APPLIED = dt.datetime(2030, 1, 1, 16, 0, tzinfo=dt.timezone.utc)
APPLIED_DAY = dt.date(2030, 1, 2)  # Shanghai midnight, UTC day differs.
FSTAT = (3, 560000, 2, 0, 0, 1, 13, 480000, 0, 1, 0)


async def daily(app, user_id, day):
    async with app.state.db.session_factory() as db:
        row = await db.scalar(text(
            "SELECT to_jsonb(t) FROM usage_daily t WHERE user_id=:u AND date=:d"
        ), {"u": user_id, "d": day})
    if row is None:
        return None
    assert set(row) == {"user_id", "date", *FIELDS}
    return tuple(row[name] for name in FIELDS)


async def session_day(app, user_id, session_id):
    async with app.state.db.session_factory() as db:
        return await db.scalar(text(
            "SELECT business_date FROM session_contributions WHERE user_id=:u AND session_id=:s"
        ), {"u": user_id, "s": session_id})


async def sources(app, user_id):
    # Only synthetic fixture hashes/counts are emitted, never tokens or text.
    tables = {
        "session_contributions": "session_id", "learning_events": "id",
        "dictionary_terms": "id", "dictionary_state": "user_id",
        "sessions": "id", "results": "id", "session_attempts": "id",
        "insertions": "id", "model_calls": "id", "model_usage_daily": "date,provider,model_id,purpose",
        "operation_requests": "operation,request_key",
    }
    out = {}
    async with app.state.db.session_factory() as db:
        for table, order in tables.items():
            # Literal allowlist identifiers only.
            values = list((await db.scalars(text(
                f"SELECT to_jsonb(t) FROM {table} t WHERE user_id=:u ORDER BY {order}"
            ), {"u": user_id})).all())
            data = json.dumps(values, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            out[table] = (len(values), hashlib.sha256(data.encode()).hexdigest())
    return out


async def rebuild(app, user_id, day):
    async with app.state.db.session_factory() as db:
        result = await stats_service.rebuild_usage_daily(db, user_id, day)
        await db.commit()
        return tuple(result[name] for name in FIELDS)


async def transition(app, user_id, session_id, outcome, samples, chars=0, has_result=False, fallback=False):
    async with app.state.db.session_factory() as db:
        # Respect the production lock order before mutating any lower-level row.
        await db.scalar(select(User.id).where(User.id == user_id).with_for_update())
        session = await db.scalar(select(VoiceSession).where(
            VoiceSession.user_id == user_id, VoiceSession.id == session_id,
        ).with_for_update())
        assert session is not None
        session.received_samples = samples
        await stats_service.record_session(db, session, outcome, has_result=has_result,
                                           char_count=chars, fallback=fallback)
        await db.commit()


class ProjectionWriteFault(PgFault):
    """Execute an actual failed PG statement at the aggregate SQL boundary."""
    def before_cursor(self, connection, cursor, statement, parameters, context, executemany):
        normalized = " ".join(statement.lower().split())
        if (not self.fired and normalized.startswith("insert into usage_daily ")
                and self.user_id in self._compiled_values(context)):
            assert not executemany
            self.fired += 1
            self.targeted_statement_sha256 = hashlib.sha256(statement.encode()).hexdigest()
            return "SELECT 1 / 0", ()
        return statement, parameters


@contextmanager
def aggregate_fault(client, user_id):
    control = ProjectionWriteFault(client.app.state.db.engine.sync_engine, user_id, "daily_write")
    client.portal.call(control.install)
    try:
        yield control
    finally:
        client.portal.call(control.remove)


def test_real_api_persists_all_counters_before_stats_read_and_receipt_once(stats_client):
    client = stats_client
    tokens = register_and_login(client)
    headers, user_id = auth_headers(tokens), uid(tokens)
    set_text(client, "你好 API")
    session, result = run_session(client, headers, "daily-normal")
    assert result["refined_text"] == "你好 API"
    day = client.portal.call(session_day, client.app, user_id, session["session_id"])
    # No stats GET precedes this direct persistent projection assertion.
    expected = (1, 8000, 1, 0, 0, 0, 5, 8000, 0, 0, 0)
    assert client.portal.call(daily, client.app, user_id, day) == expected
    reserved = claim(client, headers, session["session_id"], result)
    assert reserved.status_code == 201
    insertion = reserved.json()["insertion_id"]
    first = receipt(client, headers, session["session_id"], insertion, "inserted", True)
    assert first.status_code == 200 and first.json()["replayed"] is False
    expected = expected[:9] + (1, 0)
    assert client.portal.call(daily, client.app, user_id, day) == expected
    stable = client.portal.call(sources, client.app, user_id)
    again = receipt(client, headers, session["session_id"], insertion, "inserted", True)
    assert again.status_code == 200 and again.json()["replayed"] is True
    assert client.portal.call(daily, client.app, user_id, day) == expected
    assert client.portal.call(sources, client.app, user_id) == stable


@pytest.mark.parametrize("outcome,samples,chars,has_result,fallback,expected", [
    ("active", 160000, 0, False, False, (1,160000,0,0,0,0,0,0,0,0,0)),
    ("ready", 320000, 8, True, False, (1,320000,1,0,0,0,8,320000,0,0,0)),
    ("ready", 160000, 5, True, True, (1,160000,1,0,0,0,5,160000,1,0,0)),
    ("failed", 160000, 0, False, False, (1,160000,0,1,0,0,0,0,0,0,0)),
    ("empty", 160000, 0, False, False, (1,160000,0,0,1,0,0,0,0,0,0)),
    ("cancelled", 160000, 0, False, False, (1,160000,0,0,0,1,0,0,0,0,0)),
])
def test_old_to_new_projection_handles_positive_and_negative_deltas(stats_client,
        outcome, samples, chars, has_result, fallback, expected):
    client = stats_client
    user_id = uid(register_and_login(client))
    session_id = fixture_uuid("daily-transition")
    add_contribution(user_id, session_id, DAY.isoformat(), 160000, chars=5)
    assert client.portal.call(daily, client.app, user_id, DAY) is None  # legacy fixture
    assert client.portal.call(rebuild, client.app, user_id, DAY) == (1,160000,1,0,0,0,5,160000,0,0,0)
    for _ in range(2):
        client.portal.call(transition, client.app, user_id, session_id, outcome,
                           samples, chars, has_result, fallback)
        assert client.portal.call(daily, client.app, user_id, DAY) == expected
        assert client.portal.call(session_day, client.app, user_id, session_id) == DAY


def test_inconsistent_negative_final_projection_rolls_back_business_and_daily(stats_client):
    client = stats_client
    user_id = uid(register_and_login(client))
    session_id = fixture_uuid("daily-negative")
    add_contribution(user_id, session_id, DAY.isoformat(), 160000, chars=5)
    client.portal.call(rebuild, client.app, user_id, DAY)
    execute_sql("UPDATE usage_daily SET result_sessions=0 WHERE user_id=$1 AND date=$2", user_id, DAY)
    before = client.portal.call(sources, client.app, user_id)
    broken = client.portal.call(daily, client.app, user_id, DAY)
    with pytest.raises(SQLAlchemyError, match="USAGE_PROJECTION_INCONSISTENT"):
        client.portal.call(transition, client.app, user_id, session_id, "active", 160000)
    assert client.portal.call(sources, client.app, user_id) == before
    assert client.portal.call(daily, client.app, user_id, DAY) == broken
    # Repair from permanent sources, then the same legitimate negative delta works.
    client.portal.call(rebuild, client.app, user_id, DAY)
    client.portal.call(transition, client.app, user_id, session_id, "active", 160000)
    assert client.portal.call(daily, client.app, user_id, DAY) == (1,160000,0,0,0,0,0,0,0,0,0)


@pytest.mark.parametrize("decision", ["term", "suggest"])
def test_learning_application_day_first_only_and_undo_survives_rebuild(stats_client, monkeypatch, decision):
    client = stats_client
    set_classifier(client, monkeypatch, decision=decision, ambiguous=decision == "suggest")
    headers, session, result, insertion = prepare_inserted(client, "请用 cloud 保存", "daily-learn")
    user_id = client.portal.call(_owner, client, session["session_id"])
    original_day = client.portal.call(session_day, client.app, user_id, session["session_id"])
    original = client.portal.call(daily, client.app, user_id, original_day)
    assert original is not None and original[-1] == 0
    # Deliberately synthetic application clock only; retention uses its real
    # clock. No timestamp editing after application and no production time change.
    monkeypatch.setattr(learning_service, "utcnow", lambda: APPLIED)
    correction_id = fixture_uuid("daily-learning-correction")
    response = submit(client, headers, {**session, "insertion_id": insertion}, result,
                      "cloud", "Claude", correction_id=correction_id)
    assert response.status_code == 202
    event_id = response.json()["event_id"]
    event = wait_event(client, headers, event_id)
    confirm_key = fixture_uuid("daily-learning-confirm")
    if decision == "suggest":
        assert event["status"] == "suggested"
        assert client.portal.call(daily, client.app, user_id, APPLIED_DAY) is None
        confirmed = client.post(f"/api/v1/learning-events/{event_id}/confirm",
            headers={**headers, "Idempotency-Key": confirm_key})
        assert confirmed.status_code == 200 and confirmed.json()["status"] == "learned"
    else:
        assert event["status"] == "learned"
    applied_counts = (0,0,0,0,0,0,0,0,0,0,1)
    assert client.portal.call(daily, client.app, user_id, APPLIED_DAY) == applied_counts
    assert client.portal.call(daily, client.app, user_id, APPLIED_DAY - dt.timedelta(days=1)) is None
    assert client.portal.call(daily, client.app, user_id, original_day) == original
    replay = submit(client, headers, {**session, "insertion_id": insertion}, result,
                    "cloud", "Claude", correction_id=correction_id)
    assert replay.status_code == 200 and replay.json()["event_id"] == event_id
    for operation_key in (confirm_key, confirm_key, fixture_uuid("daily-learning-another-confirm")):
        repeated = client.post(f"/api/v1/learning-events/{event_id}/confirm",
            headers={**headers, "Idempotency-Key": operation_key})
        assert repeated.status_code == 200 and repeated.json()["status"] == "learned"
        assert client.portal.call(daily, client.app, user_id, APPLIED_DAY) == applied_counts
    for _ in range(2):
        undone = client.post(f"/api/v1/learning-events/{event_id}/undo",
            headers={**headers, "Idempotency-Key": fixture_uuid("daily-learning-undo")})
        assert undone.status_code == 200 and undone.json()["undone"] is True
        assert client.portal.call(daily, client.app, user_id, APPLIED_DAY) == applied_counts
    # Cleanup-compatible applied_at is permanent; learned+undone still contributes.
    execute_sql("UPDATE learning_events SET before_text=NULL,after_text=NULL,left_context=NULL,"
                "right_context=NULL,evidence=NULL,term_before=NULL,content_cleared_at=now() WHERE id=$1", event_id)
    before = client.portal.call(sources, client.app, user_id)
    for _ in range(2):
        assert client.portal.call(rebuild, client.app, user_id, APPLIED_DAY) == applied_counts
    assert client.portal.call(sources, client.app, user_id) == before


def test_confirm_real_pg_aggregate_failure_rolls_back_dictionary_event_key_and_daily(stats_client, monkeypatch):
    client = stats_client
    set_classifier(client, monkeypatch, decision="suggest", ambiguous=True)
    headers, session, result, insertion = prepare_inserted(client, "请用 cloud 保存", "daily-confirm-fault")
    user_id = client.portal.call(_owner, client, session["session_id"])
    response = submit(client, headers, {**session, "insertion_id": insertion}, result, "cloud", "Claude")
    assert response.status_code == 202
    event_id = response.json()["event_id"]
    assert wait_event(client, headers, event_id)["status"] == "suggested"
    monkeypatch.setattr(learning_service, "utcnow", lambda: APPLIED)
    before = client.portal.call(sources, client.app, user_id)
    confirm_headers = {**headers, "Idempotency-Key": fixture_uuid("daily-confirm-fault-key"),
                       "X-Request-Id": fixture_uuid("daily-confirm-fault-request")}
    with aggregate_fault(client, user_id) as control:
        failed = client.post(f"/api/v1/learning-events/{event_id}/confirm", headers=confirm_headers)
        _assert_safe_failure(failed, confirm_headers["X-Request-Id"],
                             (result["raw_text"], result["refined_text"], headers["Authorization"]))
        assert control.fired == 1 and control.sqlstate == "22012"
        assert client.portal.call(sources, client.app, user_id) == before
        assert client.portal.call(daily, client.app, user_id, APPLIED_DAY) is None
        _wait_for_lifespan_readiness(client, control)
        for replayed in (False, True):
            fixed = client.post(f"/api/v1/learning-events/{event_id}/confirm", headers=confirm_headers)
            assert fixed.status_code == 200 and fixed.json()["status"] == "learned"
            assert fixed.json()["replayed"] is replayed
            assert client.portal.call(daily, client.app, user_id, APPLIED_DAY) == (0,0,0,0,0,0,0,0,0,0,1)
        assert control.fired == 1


def test_automatic_application_real_pg_failure_is_atomic_then_explicit_worker_reentry_once(stats_client, monkeypatch):
    client = stats_client
    set_classifier(client, monkeypatch, decision="term", ambiguous=False)
    headers, session, result, insertion = prepare_inserted(client, "请用 cloud 保存", "daily-auto-fault")
    user_id = client.portal.call(_owner, client, session["session_id"])
    queued = []
    def capture_scheduled(app, event_id, owner_id):
        assert app is client.app and owner_id == user_id
        queued.append(event_id)
    monkeypatch.setattr(learning_service, "schedule", capture_scheduled)
    response = submit(client, headers, {**session, "insertion_id": insertion}, result, "cloud", "Claude")
    assert response.status_code == 202
    event_id = response.json()["event_id"]
    assert queued == [event_id]
    monkeypatch.setattr(learning_service, "utcnow", lambda: APPLIED)
    before = client.portal.call(sources, client.app, user_id)
    with aggregate_fault(client, user_id) as control:
        with pytest.raises(DBAPIError):
            client.portal.call(learning_service.process_event, client.app, event_id)
        assert control.fired == 1 and control.sqlstate == "22012"
        assert client.portal.call(sources, client.app, user_id) == before
        assert client.portal.call(daily, client.app, user_id, APPLIED_DAY) is None
        # Explicit controlled worker reentry proves transaction idempotence; it
        # does NOT claim a production automatic database-retry scheduler exists.
        for _ in range(2):
            client.portal.call(learning_service.process_event, client.app, event_id)
            assert client.portal.call(daily, client.app, user_id, APPLIED_DAY) == (0,0,0,0,0,0,0,0,0,0,1)
        assert client.get(f"/api/v1/learning-events/{event_id}", headers=headers).json()["status"] == "learned"


def test_rebuild_from_permanent_sources_is_scoped_atomic_idempotent_and_content_independent(stats_client):
    client = stats_client
    user_id = uid(register_and_login(client))
    other = uid(register_and_login(client, email="daily-other@example.com"))
    add_contribution(user_id, fixture_uuid("daily-A"), DAY.isoformat(), 160000, chars=5, inserted=True)
    add_contribution(user_id, fixture_uuid("daily-B"), DAY.isoformat(), 320000, chars=8)
    add_contribution(user_id, fixture_uuid("daily-C"), DAY.isoformat(), 80000,
                     outcome="cancelled", has_result=False)
    add_contribution(other, fixture_uuid("daily-other"), DAY.isoformat(), 160000, chars=7)
    client.portal.call(rebuild, client.app, other, DAY)
    other_daily = client.portal.call(daily, client.app, other, DAY)
    other_day = DAY + dt.timedelta(days=1)
    add_contribution(user_id, fixture_uuid("daily-other-day"), other_day.isoformat(), 160000, chars=7)
    client.portal.call(rebuild, client.app, user_id, other_day)
    other_day_daily = client.portal.call(daily, client.app, user_id, other_day)
    # Simulate content clearing, preserving all permanent source counters.
    execute_sql("UPDATE results SET raw_text=NULL,refined_text=NULL,content_cleared_at=now() WHERE user_id=$1", user_id)
    execute_sql("UPDATE session_attempts SET raw_text=NULL WHERE user_id=$1", user_id)
    execute_sql("UPDATE sessions SET content_cleared_at=now() WHERE user_id=$1", user_id)
    before = client.portal.call(sources, client.app, user_id)
    assert client.portal.call(rebuild, client.app, user_id, DAY) == FSTAT
    execute_sql("UPDATE usage_daily SET sessions=999,chars=999 WHERE user_id=$1 AND date=$2", user_id, DAY)
    broken = client.portal.call(daily, client.app, user_id, DAY)
    with aggregate_fault(client, user_id) as control:
        with pytest.raises(DBAPIError):
            client.portal.call(rebuild, client.app, user_id, DAY)
        assert control.fired == 1 and control.sqlstate == "22012"
        assert client.portal.call(daily, client.app, user_id, DAY) == broken
        assert client.portal.call(sources, client.app, user_id) == before
        for _ in range(2):
            assert client.portal.call(rebuild, client.app, user_id, DAY) == FSTAT
            assert client.portal.call(daily, client.app, user_id, DAY) == FSTAT
        assert control.fired == 1
    assert client.portal.call(daily, client.app, other, DAY) == other_daily
    assert client.portal.call(daily, client.app, user_id, other_day) == other_day_daily
    assert client.portal.call(sources, client.app, user_id) == before


def test_rebuild_and_two_insertions_same_user_day_serialize_without_lost_update(stats_client):
    client = stats_client
    user_id = uid(register_and_login(client))
    session_ids = [fixture_uuid(f"daily-concurrent-{index}") for index in range(2)]
    for session_id in session_ids:
        add_contribution(user_id, session_id, DAY.isoformat(), 160000, chars=5)
    client.portal.call(rebuild, client.app, user_id, DAY)

    async def concurrent():
        gate = asyncio.Event()
        async def record(session_id):
            await gate.wait()
            async with client.app.state.db.session_factory() as db:
                await stats_service.record_inserted(db, user_id, session_id)
                await db.commit()
        async def rebuild_same_day():
            await gate.wait()
            await rebuild(client.app, user_id, DAY)
        tasks = [asyncio.create_task(record(item)) for item in session_ids]
        tasks.append(asyncio.create_task(rebuild_same_day()))
        gate.set()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)
    client.portal.call(concurrent)
    expected = (2,320000,2,0,0,0,10,320000,0,2,0)
    assert client.portal.call(daily, client.app, user_id, DAY) == expected
    assert client.portal.call(rebuild, client.app, user_id, DAY) == expected


def test_lifespan_initializes_legacy_daily_before_interrupted_session_recovery(clean_database, monkeypatch):
    monkeypatch.setenv("SVI_DATABASE_URL", TEST_URL)
    monkeypatch.setenv("SVI_CONFIG", str(SERVER_ROOT / "config" / "app.yaml"))
    monkeypatch.setenv("SVI_INSTANCE_LOCK", "0")
    monkeypatch.setenv("SVI_ENVIRONMENT", "development")
    monkeypatch.setenv("SVI_ASR_PROVIDER", "mock")
    monkeypatch.setenv("SVI_LLM_PROVIDER", "mock")
    from app.main import create_app

    with TestClient(create_app(str(SERVER_ROOT / "config" / "app.yaml"))) as first:
        wait_for_readiness(first)
        assert first.get("/health/ready").status_code == 200
        user_id = uid(register_and_login(first))
    # No live app/runtime while creating legacy persistent sources.
    ready_id, active_id = fixture_uuid("legacy-ready"), fixture_uuid("legacy-active")
    add_contribution(user_id, ready_id, DAY.isoformat(), 160000, chars=5)
    add_contribution(user_id, active_id, DAY.isoformat(), 80000, outcome="active", has_result=False)
    execute_sql("UPDATE users SET status='disabled' WHERE id=$1", user_id)
    # Stale day with no authoritative sources must become zero, not survive.
    stale = DAY - dt.timedelta(days=1)
    columns = ",".join(FIELDS)
    execute_sql(f"INSERT INTO usage_daily (user_id,date,{columns}) VALUES ($1,$2,{','.join(['9'] * 11)})", user_id, stale)
    expected = (2,240000,1,1,0,0,5,160000,0,0,0)
    observed = []
    production_record_session = stats_service.record_session

    async def observe_recovery(db, session, outcome, **kwargs):
        if session.id == active_id:
            projection = await db.scalar(text(
                "SELECT to_jsonb(t) FROM usage_daily t WHERE user_id=:u AND date=:d"
            ), {"u": user_id, "d": DAY})
            assert projection is not None
            # Observe ordering without replacing any arithmetic or DB writes.
            assert tuple(projection[name] for name in FIELDS) == (2,240000,1,0,0,0,5,160000,0,0,0)
            observed.append(session.id)
        return await production_record_session(db, session, outcome, **kwargs)

    monkeypatch.setattr(stats_service, "record_session", observe_recovery)
    for _ in range(2):
        with TestClient(create_app(str(SERVER_ROOT / "config" / "app.yaml"))) as restarted:
            wait_for_readiness(restarted)
            assert restarted.get("/health/ready").status_code == 200
            assert restarted.portal.call(daily, restarted.app, user_id, DAY) == expected
            assert restarted.portal.call(daily, restarted.app, user_id, stale) == (0,) * 11
            assert restarted.portal.call(rebuild, restarted.app, user_id, DAY) == expected
    assert observed == [active_id]
