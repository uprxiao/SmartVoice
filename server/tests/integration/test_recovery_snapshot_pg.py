"""Real PostgreSQL tests for the frozen startup recovery cohort.

Only tests/conftest's process-owned DB is used. Accounts/login/device parents
come from the real API; session/result/insertion/event rows and recorder calls
are declared synthetic accounting fixtures, NOT inference/native/ASR evidence.
No two-app ownership failure, heartbeat, runtime or production budget is mocked.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
import datetime as dt
import hashlib
import json
import uuid

import pytest
from sqlalchemy import select, text

from app import main
from app.models import Insertion, LearningEvent, LoginSession, SessionAttempt, SessionResult, User, VoiceSession
from app.services import stats_service
from app.services.cleanup import mark_restarted_pending
from app.services.inference_context import CallScope
from app.services.recovery_snapshot import (
    RecoveryIdentity, RecoveryOwnershipLost, RecoverySnapshot, capture_recovery_snapshot,
)
from tests.conftest import TEST_DB, register_and_login
from tests.integration.test_instance_recovery_loss import require_process_owned_database


RAW = "请用 SQL lite 保存配置。"
PROVIDER, MODEL = "fixture-recovery-ledger", "fixture-no-physical-model"
MODEL_FIELDS = (
    "calls", "successes", "failures", "unknown_calls", "elapsed_ms",
    "processed_audio_samples", "prompt_tokens_known", "completion_tokens_known",
    "prompt_tokens_unknown_calls", "completion_tokens_unknown_calls",
)
TABLES = (
    "sessions", "session_attempts", "results", "insertions", "learning_events",
    "session_contributions", "usage_daily", "model_calls", "model_usage_daily",
    "dictionary_state", "dictionary_terms",
)


@pytest.fixture(autouse=True)
def mock_prepare_only(monkeypatch):
    # Existing app_client still owns migrations/lifespan. Never let this DB-only
    # module choose an installed physical ASR/LLM from the caller's environment.
    monkeypatch.setenv("SVI_ASR_PROVIDER", "mock")
    monkeypatch.setenv("SVI_LLM_PROVIDER", "mock")


def uid():
    return str(uuid.uuid4())


def now():
    return dt.datetime.now(dt.timezone.utc)


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def request_hash():
    # Only synthetic fixture identities use this hash. No claim of API request
    # HMAC/idempotency validation is made by these direct-maintenance tests.
    return hashlib.sha256(uuid.uuid4().bytes).digest()


@dataclass(frozen=True)
class Actor:
    user_id: str
    device_id: str
    login_sid: str


async def actor_from_api(database, user_id):
    async with database.session_factory() as db:
        owner = await db.get(User, user_id)
        valid_hash = owner is not None and owner.password_hash.startswith("$argon2id$")
        assert valid_hash  # Do not render even a test password hash on failure.
        login = await db.scalar(select(LoginSession).where(
            LoginSession.user_id == user_id, LoginSession.client_type == "desktop",
            LoginSession.revoked_at.is_(None),
        ))
        assert login is not None and login.device_id is not None
        return Actor(user_id, login.device_id, login.id)


def register_actor(client, label):
    tokens = register_and_login(client, email=f"snapshot-{label}@example.invalid")
    # Tokens remain in memory and are never passed to assertions/evidence.
    return client.portal.call(actor_from_api, client.app.state.db, tokens["user"]["id"])


@asynccontextmanager
async def owned_connection(database, *, sign=1, acquire=True, allow_connection_loss=False):
    require_process_owned_database()
    key = sign * ((1 << 61) | (uuid.uuid4().int & ((1 << 60) - 1)))
    async with database.engine.connect() as connection:
        assert await connection.scalar(text("SELECT current_database()")) == TEST_DB
        pid = await connection.scalar(text("SELECT pg_backend_pid()"))
        acquired = False
        if acquire:
            acquired = await connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
            assert acquired is True
        await connection.commit()
        try:
            yield connection, pid, key
        finally:
            # No backend termination. Release only this connection's exact
            # randomly selected session lock; rollback an interrupted read first.
            if connection.in_transaction():
                await connection.rollback()
            if connection.closed or connection.invalidated:
                assert allow_connection_loss, "unexpected loss of this fixture's dedicated connection"
            elif acquired:
                unlocked = await connection.scalar(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                assert unlocked is True
                await connection.commit()


async def owned_locks(connection):
    rows = (await connection.execute(text("""SELECT database,classid,objid,objsubid,mode,granted
        FROM pg_locks WHERE pid=pg_backend_pid() AND locktype='advisory'
        ORDER BY database,classid,objid,objsubid,mode"""))).all()
    await connection.commit()
    return tuple(tuple(row) for row in rows)


async def rows(database, table, user_id, *, resource_id=None, session_id=None):
    assert table in TABLES
    clauses, parameters = ["user_id=:u"], {"u": user_id}
    if resource_id is not None:
        clauses.append("id=:r")
        parameters["r"] = resource_id
    if session_id is not None:
        clauses.append("session_id=:s")
        parameters["s"] = session_id
    async with database.session_factory() as db:
        values = list((await db.scalars(text(
            f"SELECT to_jsonb(t) FROM {table} t WHERE " + " AND ".join(clauses)
            + " ORDER BY to_jsonb(t)::text"
        ), parameters)).all())
    return values


async def one(database, table, actor, resource_id):
    result = await rows(database, table, actor.user_id, resource_id=resource_id)
    assert len(result) == 1
    return result[0]


async def session_bundle(database, actor, session_id):
    answer = {"sessions": await rows(database, "sessions", actor.user_id, resource_id=session_id)}
    for table in ("session_attempts", "results", "insertions", "learning_events", "session_contributions"):
        answer[table] = await rows(database, table, actor.user_id, session_id=session_id)
    return answer


async def fingerprints(database, actor, tables=TABLES):
    # Full JSON rows, including timestamps/revisions/nulls, are compared. Only
    # their digests are returned to the test; no credentials or body is logged.
    return {table: hashlib.sha256(json.dumps(await rows(database, table, actor.user_id),
        ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest() for table in tables}


async def seed_session(database, actor, *, state, cache="none", raw=None):
    session_id, attempt_id = uid(), uid()
    timestamp = now()
    active = state in ("created", "streaming", "finalizing", "refining")
    terminal = None if active else timestamp
    samples = 4000 if state in ("finalizing", "refining", "ready") or cache == "available" else 0
    async with database.session_factory() as db:
        await db.scalar(select(User.id).where(User.id == actor.user_id).with_for_update())
        session = VoiceSession(id=session_id, user_id=actor.user_id, device_id=actor.device_id,
            created_login_sid=actor.login_sid, client_request_id=uid(), request_hash=request_hash(),
            state=state, current_attempt_no=1, received_samples=samples,
            created_at=timestamp, audio_complete=bool(samples), end_accepted_at=timestamp if samples else None,
            first_terminal_at=terminal, first_asr_failed_at=terminal if cache == "available" else None,
            retry_cache_status=cache, error_code="ASR_FAILED" if cache == "available" else None,
            asr_hints=["fixture-hint"] if cache == "available" else None,
            llm_hints=[{"term": "SQLite"}] if state == "refining" else None)
        db.add(session)
        await db.flush()
        attempt = SessionAttempt(id=attempt_id, user_id=actor.user_id, session_id=session_id,
            attempt_no=1, kind="initial", state="running" if active else state,
            raw_text=raw, started_at=timestamp, ended_at=terminal,
            error_code="ASR_FAILED" if cache == "available" else None)
        db.add(attempt)
        await db.flush()
        char_count = sum(ch.isalnum() for ch in raw or "")
        if state == "ready":
            assert raw is not None
            result = SessionResult(id=uid(), user_id=actor.user_id, session_id=session_id,
                attempt_no=1, raw_text=raw, refined_text=raw, review_required=False,
                text_hash=digest(raw), dictionary_version=0, char_count=char_count, created_at=timestamp)
            db.add(result)
            await db.flush()
            session.current_result_id = result.id
        await stats_service.record_session(db, session, "active" if active else state,
            has_result=state == "ready", char_count=char_count if state == "ready" else 0)
        await db.commit()
    return session_id


async def seed_learning_parent(database, actor):
    session_id = await seed_session(database, actor, state="ready", raw=RAW)
    insertion_id = uid()
    async with database.session_factory() as db:
        await db.scalar(select(User.id).where(User.id == actor.user_id).with_for_update())
        session = await db.scalar(select(VoiceSession).where(
            VoiceSession.user_id == actor.user_id, VoiceSession.id == session_id).with_for_update())
        db.add(Insertion(id=insertion_id, user_id=actor.user_id, session_id=session_id,
            result_id=session.current_result_id, device_id=actor.device_id, text_hash=digest(RAW),
            status="inserted", observable=True, completed_at=now(), request_hash=request_hash()))
        await db.flush()
        await stats_service.record_inserted(db, actor.user_id, session_id)
        await db.commit()
    return session_id, insertion_id


async def seed_event(database, actor, parent):
    event_id = uid()
    offset = RAW.index("SQL lite")
    async with database.session_factory() as db:
        db.add(LearningEvent(id=event_id, user_id=actor.user_id, session_id=parent[0],
            insertion_id=parent[1], correction_id=uid(), request_hash=request_hash(),
            offset=offset, before_text="SQL lite", after_text="SQLite",
            left_context=RAW[:offset], right_context=RAW[offset + len("SQL lite"):], status="pending"))
        await db.commit()
    return event_id


async def seed_call(recorder, actor):
    # Production recorder, synthetic accounting input. No provider is invoked.
    return await recorder.started(CallScope(user_id=actor.user_id), PROVIDER, MODEL, "refine")


async def recover_all(app, snapshot):
    await mark_restarted_pending(app, identities=snapshot.learning)
    await main._recover_interrupted_sessions(app.state.db, identities=snapshot.sessions)
    await app.state.recorder.recover_incomplete(identities=snapshot.calls)


async def model_totals(database, actor):
    values = await rows(database, "model_usage_daily", actor.user_id)
    assert values and all(row["provider"] == PROVIDER and row["model_id"] == MODEL and row["purpose"] == "refine"
                          for row in values)
    # Sum actual day keys so an execution across Shanghai midnight stays valid.
    # The established dedicated date-boundary tests retain their exact-day oracle.
    return tuple(sum(row[field] for row in values) for field in MODEL_FIELDS)


@pytest.mark.parametrize("key_sign", [1, -1], ids=["positive-key", "negative-key"])
def test_pg_frozen_three_cohorts_preserve_same_tenant_new_ids_and_terminal_rows(app_client, key_sign, record_property):
    actor = register_actor(app_client, "cohorts")

    async def run():
        app, database = app_client.app, app_client.app.state.db
        recorder = app.state.recorder
        parent = await seed_learning_parent(database, actor)
        old_session = await seed_session(database, actor, state="failed", cache="available")
        pending, completed_event = await seed_event(database, actor, parent), await seed_event(database, actor, parent)
        old_call, completed_call = await seed_call(recorder, actor), await seed_call(recorder, actor)
        async with owned_connection(database, sign=key_sign) as (connection, pid, key):
            snapshot = await capture_recovery_snapshot(connection, backend_pid=pid, key=key)
            assert snapshot.ownership_checked is True
            assert set(snapshot.learning) == {RecoveryIdentity(actor.user_id, pending), RecoveryIdentity(actor.user_id, completed_event)}
            assert {(item.user_id, item.resource_id, item.attempt_no, item.state, item.retry_cache_status)
                    for item in snapshot.sessions} == {(actor.user_id, old_session, 1, "failed", "available")}
            assert set(snapshot.calls) == {RecoveryIdentity(actor.user_id, old_call), RecoveryIdentity(actor.user_id, completed_call)}
            async with database.session_factory() as db:
                await db.scalar(select(User.id).where(User.id == actor.user_id).with_for_update())
                event = await db.get(LearningEvent, completed_event)
                event.status, event.classification, event.ambiguous = "suggested", "suggest", True
                await db.commit()
            await recorder.finished(completed_call, "succeeded", None, 5, 2, 1)
            new_session = await seed_session(database, actor, state="refining", raw=RAW)
            new_event, new_call = await seed_event(database, actor, parent), await seed_call(recorder, actor)
            protected = {
                "session": await session_bundle(database, actor, new_session),
                "events": [await one(database, "learning_events", actor, item) for item in (completed_event, new_event)],
                "calls": [await one(database, "model_calls", actor, item) for item in (completed_call, new_call)],
                "dictionary_and_usage": await fingerprints(database, actor, ("dictionary_state", "dictionary_terms", "usage_daily")),
            }
            await recover_all(app, snapshot)
            assert await session_bundle(database, actor, new_session) == protected["session"]
            assert [await one(database, "learning_events", actor, item) for item in (completed_event, new_event)] == protected["events"]
            assert [await one(database, "model_calls", actor, item) for item in (completed_call, new_call)] == protected["calls"]
            assert await fingerprints(database, actor, ("dictionary_state", "dictionary_terms", "usage_daily")) == protected["dictionary_and_usage"]
            old_event = await one(database, "learning_events", actor, pending)
            assert (old_event["status"], old_event["reason"]) == ("failed", "SERVER_RESTARTED")
            cached = await one(database, "sessions", actor, old_session)
            assert cached["state"] == "failed" and cached["retry_cache_status"] == "server_restarted"
            assert cached["asr_hints"] is cached["llm_hints"] is None
            recovered = await one(database, "model_calls", actor, old_call)
            assert (recovered["status"], recovered["error_code"]) == ("unknown", "SERVER_RESTARTED")
            assert recovered["prompt_tokens"] is recovered["completion_tokens"] is None
            assert recovered["ended_at"] is not None and recovered["aggregated_at"] is not None
            assert await model_totals(database, actor) == (2, 1, 1, 1, 5, 0, 2, 1, 1, 1)
            unchanged = await fingerprints(database, actor)
            await recover_all(app, snapshot)
            assert await fingerprints(database, actor) == unchanged
            await recorder.finished(new_call, "succeeded", None, 13, 7, 3)
            new_done = await one(database, "model_calls", actor, new_call)
            assert (new_done["status"], new_done["error_code"], new_done["prompt_tokens"],
                    new_done["completion_tokens"]) == ("succeeded", None, 7, 3)
            assert await model_totals(database, actor) == (3, 2, 1, 1, 18, 0, 9, 4, 1, 1)
            completed = await fingerprints(database, actor)
            await recorder.finished(new_call, "succeeded", None, 13, 7, 3)
            await recorder.rebuild_usage_before_recovery()
            assert await fingerprints(database, actor) == completed
        return {"ownership_checked": True, "key_sign": key_sign,
                "new_ids_preserved": ["session", "learning", "call"], "repeat_idempotent": True,
                "data_kind": "synthetic_accounting_not_physical_model"}

    record_property("recovery_snapshot_pg", json.dumps(app_client.portal.call(run)))


def test_pg_captured_refining_session_recovers_raw_review_once(app_client):
    actor = register_actor(app_client, "refining")

    async def run():
        app, database = app_client.app, app_client.app.state.db
        session_id = await seed_session(database, actor, state="refining", raw=RAW)
        async with owned_connection(database) as (connection, pid, key):
            snapshot = await capture_recovery_snapshot(connection, backend_pid=pid, key=key)
            assert len(snapshot.sessions) == 1 and snapshot.learning == snapshot.calls == ()
            await recover_all(app, snapshot)
            bundle = await session_bundle(database, actor, session_id)
            assert len(bundle["results"]) == len(bundle["session_attempts"]) == len(bundle["session_contributions"]) == 1
            session, result = bundle["sessions"][0], bundle["results"][0]
            assert session["state"] == "ready" and session["current_result_id"] == result["id"]
            assert result["raw_text"] == result["refined_text"] == RAW
            assert result["text_hash"] == digest(RAW)
            assert result["review_required"] is True and result["review_reason"] == "server_restarted"
            attempt = bundle["session_attempts"][0]
            assert attempt["state"] == "ready" and attempt["error_code"] == "SERVER_RESTARTED"
            contribution = bundle["session_contributions"][0]
            assert contribution["outcome"] == "ready" and contribution["has_result"] is True and contribution["fallback"] is True
            daily = await rows(database, "usage_daily", actor.user_id)
            assert len(daily) == 1
            assert (daily[0]["sessions"], daily[0]["result_sessions"], daily[0]["fallback_sessions"],
                    daily[0]["failed_sessions"], daily[0]["successful_audio_samples"]) == (1, 1, 1, 0, 4000)
            before = await fingerprints(database, actor)
            await recover_all(app, snapshot)
            assert await fingerprints(database, actor) == before

    app_client.portal.call(run)


@pytest.mark.parametrize("changed", ["attempt", "state", "cache"])
def test_pg_session_recovery_cas_preserves_each_changed_dimension(app_client, changed):
    actor = register_actor(app_client, "session-cas")

    async def run():
        app, database = app_client.app, app_client.app.state.db
        session_id = await seed_session(database, actor,
            state="created" if changed == "state" else "failed",
            cache="none" if changed == "state" else "available")
        async with owned_connection(database) as (connection, pid, key):
            snapshot = await capture_recovery_snapshot(connection, backend_pid=pid, key=key)
            assert len(snapshot.sessions) == 1
            async with database.session_factory() as db:
                await db.scalar(select(User.id).where(User.id == actor.user_id).with_for_update())
                session = await db.scalar(select(VoiceSession).where(
                    VoiceSession.user_id == actor.user_id, VoiceSession.id == session_id).with_for_update())
                if changed == "attempt":
                    # Synthetic completed retry: same failed/available state,
                    # new attempt 2. Only the captured identity dimension changes.
                    timestamp = now()
                    db.add(SessionAttempt(id=uid(), user_id=actor.user_id, session_id=session_id,
                        attempt_no=2, kind="asr", source_attempt_no=1, state="failed",
                        error_code="ASR_FAILED", started_at=timestamp, ended_at=timestamp))
                    await db.flush()
                    session.current_attempt_no = 2
                elif changed == "state":
                    session.state = "streaming"  # Legal forward transition, same attempt/cache.
                else:
                    session.retry_cache_status = "expired"
                await db.commit()
            before = await fingerprints(database, actor)
            await main._recover_interrupted_sessions(database, identities=snapshot.sessions)
            assert await fingerprints(database, actor) == before
            current = await one(database, "sessions", actor, session_id)
            assert (current["current_attempt_no"], current["state"], current["retry_cache_status"]) == {
                "attempt": (2, "failed", "available"), "state": (1, "streaming", "none"),
                "cache": (1, "failed", "expired"),
            }[changed]

    app_client.portal.call(run)


def test_pg_empty_snapshot_does_not_reenumerate_later_three_kinds(app_client):
    actor = register_actor(app_client, "empty")

    async def run():
        app, database = app_client.app, app_client.app.state.db
        async with owned_connection(database) as (connection, pid, key):
            snapshot = await capture_recovery_snapshot(connection, backend_pid=pid, key=key)
            assert snapshot.learning == snapshot.sessions == snapshot.calls == ()
            parent = await seed_learning_parent(database, actor)
            event_id = await seed_event(database, actor, parent)
            session_id = await seed_session(database, actor, state="refining", raw=RAW)
            call_id = await seed_call(app.state.recorder, actor)
            before = await fingerprints(database, actor)
            await recover_all(app, snapshot)
            assert await fingerprints(database, actor) == before
            assert (await one(database, "learning_events", actor, event_id))["status"] == "pending"
            assert (await one(database, "sessions", actor, session_id))["state"] == "refining"
            assert (await one(database, "model_calls", actor, call_id))["status"] == "running"

    app_client.portal.call(run)


def test_pg_wrong_tenant_ids_cannot_recover_other_users_objects(app_client):
    owner, other = register_actor(app_client, "owner"), register_actor(app_client, "other")

    async def run():
        app, database = app_client.app, app_client.app.state.db
        parent = await seed_learning_parent(database, owner)
        await seed_event(database, owner, parent)
        await seed_session(database, owner, state="refining", raw=RAW)
        await seed_call(app.state.recorder, owner)
        async with owned_connection(database) as (connection, pid, key):
            snapshot = await capture_recovery_snapshot(connection, backend_pid=pid, key=key)
            assert len(snapshot.learning) == len(snapshot.sessions) == len(snapshot.calls) == 1
            mismatch = RecoverySnapshot(
                tuple(replace(identity, user_id=other.user_id) for identity in snapshot.learning),
                tuple(replace(identity, user_id=other.user_id) for identity in snapshot.sessions),
                tuple(replace(identity, user_id=other.user_id) for identity in snapshot.calls), True)
            before = [await fingerprints(database, actor) for actor in (owner, other)]
            await recover_all(app, mismatch)
            assert [await fingerprints(database, actor) for actor in (owner, other)] == before

    app_client.portal.call(run)


def test_pg_wrong_pid_key_and_unowned_connection_never_acquire_a_lock(app_client):
    async def run():
        database = app_client.app.state.db
        async with owned_connection(database) as (connection, pid, key):
            before = await owned_locks(connection)
            assert len(before) == 1 and before[0][-2:] == ("ExclusiveLock", True)
            for supplied_pid, supplied_key in ((pid + 1, key), (pid, key + 1)):
                with pytest.raises(RecoveryOwnershipLost):
                    await capture_recovery_snapshot(connection, backend_pid=supplied_pid, key=supplied_key)
                assert await owned_locks(connection) == before
                assert not connection.in_transaction()
            # Failed probes do not poison the original valid owner/connection.
            assert (await capture_recovery_snapshot(connection, backend_pid=pid, key=key)).ownership_checked is True
        async with owned_connection(database, acquire=False) as (connection, pid, key):
            assert await owned_locks(connection) == ()
            with pytest.raises(RecoveryOwnershipLost):
                await capture_recovery_snapshot(connection, backend_pid=pid, key=key)
            assert await owned_locks(connection) == ()

    app_client.portal.call(run)
