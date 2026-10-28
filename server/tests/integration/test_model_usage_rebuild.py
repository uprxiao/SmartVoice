"""Draft real-PG startup ledger rebuild tests; author has NOT run/collected them.

Call rows are explicitly synthetic accounting fixtures, never model executions
or throughput evidence. Existing conftest owns a unique test database. No new
database deletion/truncation path or network model request is introduced.
"""
from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select, text

from app.models import ModelCall, ModelUsageDaily, User
from app.services.model_call_recorder import DatabaseCallRecorder
from tests.conftest import SERVER_ROOT, TEST_URL, register_and_login, wait_for_readiness
from tests.ids import fixture_uuid

# Independent expected fields, not imported from the implementation.
FIELDS = (
    "calls", "successes", "failures", "unknown_calls", "elapsed_ms",
    "processed_audio_samples", "prompt_tokens_known", "completion_tokens_known",
    "prompt_tokens_unknown_calls", "completion_tokens_unknown_calls",
)
DAY = dt.date(2026, 10, 16)
NEXT = DAY + dt.timedelta(days=1)
BEFORE = dt.datetime(2026, 10, 16, 15, 59, 59, 999999, tzinfo=dt.timezone.utc)
BOUNDARY = dt.datetime(2026, 10, 16, 16, 0, tzinfo=dt.timezone.utc)
OLD_MARKER = dt.datetime(2026, 10, 17, 0, 0, tzinfo=dt.timezone.utc)
PROVIDER, MODEL = "fixture-ledger-provider", "fixture-ledger-model"
MAIN = (DAY.isoformat(), PROVIDER, MODEL, "refine")
RUNNING = (NEXT.isoformat(), PROVIDER, MODEL, "asr_decode")
STALE = ((DAY - dt.timedelta(days=1)).isoformat(), "fixture-stale", MODEL, "audit")
ZERO = (0,) * 10


@pytest.fixture
def startup_app(clean_database, monkeypatch):
    monkeypatch.setenv("SVI_DATABASE_URL", TEST_URL)
    monkeypatch.setenv("SVI_CONFIG", str(SERVER_ROOT / "config" / "app.yaml"))
    monkeypatch.setenv("SVI_INSTANCE_LOCK", "0")
    monkeypatch.setenv("SVI_ENVIRONMENT", "development")
    monkeypatch.setenv("SVI_ASR_PROVIDER", "mock")
    monkeypatch.setenv("SVI_LLM_PROVIDER", "mock")
    from app.main import create_app
    return lambda: create_app(str(SERVER_ROOT / "config" / "app.yaml"))


async def projection(database, user_id):
    async with database.session_factory() as db:
        rows = (await db.execute(text(
            "SELECT to_jsonb(t) FROM model_usage_daily t WHERE user_id=:u "
            "ORDER BY date,provider,model_id,purpose"
        ), {"u": user_id})).scalars().all()
    return {
        (row["date"], row["provider"], row["model_id"], row["purpose"]):
            tuple(row[name] for name in FIELDS)
        for row in rows
    }


async def source_rows(database, user_id):
    async with database.session_factory() as db:
        rows = (await db.execute(text(
            "SELECT to_jsonb(t) FROM model_calls t WHERE user_id=:u ORDER BY id"
        ), {"u": user_id})).scalars().all()
    return {row["id"]: row for row in rows}


async def seed(database, first_user, second_user):
    # Optional session/attempt/event identities are deliberately null, permitted
    # by the established ledger schema. Users were registered by the actual API,
    # so password hashes and their tenant parents are real/valid. No invented
    # completed dictation or model execution is claimed by these fixture rows.
    facts = (
        ("known", "succeeded", BEFORE, "refine", 10, 0, 100, 20, OLD_MARKER, PROVIDER, MODEL),
        ("failed", "failed", BEFORE, "refine", 20, 0, None, None, None, PROVIDER, MODEL),
        ("cancelled", "cancelled", BEFORE, "refine", 30, 0, 0, None, None, PROVIDER, MODEL),
        ("unknown", "unknown", BEFORE, "refine", 40, 0, None, 0, None, PROVIDER, MODEL),
        ("zero", "succeeded", BOUNDARY, "refine", 7, 0, 0, 0, None, PROVIDER, MODEL),
        ("audio", "succeeded", BOUNDARY, "asr_finish", 8, 48000, None, None, None, PROVIDER, MODEL),
        ("running", "running", BOUNDARY, "asr_decode", 0, 16000, None, None, None, PROVIDER, MODEL),
        ("provider", "succeeded", BEFORE, "refine", 2, 0, 3, 1, None, PROVIDER + "-other", MODEL),
        ("model", "succeeded", BEFORE, "refine", 3, 0, 4, 2, None, PROVIDER, MODEL + "-other"),
    )
    ids = {}
    async with database.session_factory() as db:
        for label, status, started, purpose, elapsed, samples, prompt, completion, marker, provider, model in facts:
            call_id = fixture_uuid("model-rebuild-" + label)
            ids[label] = call_id
            db.add(ModelCall(id=call_id, user_id=first_user, provider=provider,
                model_id=model, purpose=purpose, call_no=1, started_at=started,
                # End deliberately crosses Shanghai midnight: grouping must use
                # started_at rather than ended_at or the rebuild's current date.
                ended_at=None if status == "running" else BOUNDARY + dt.timedelta(seconds=1),
                status=status, error_code=None if status in {"succeeded", "running"} else "FIXTURE_FAILURE",
                elapsed_ms=elapsed, processed_audio_samples=samples,
                prompt_tokens=prompt, completion_tokens=completion, aggregated_at=marker))
        db.add(ModelCall(id=fixture_uuid("model-rebuild-other-user"), user_id=second_user,
            provider=PROVIDER, model_id=MODEL, purpose="refine", call_no=1,
            started_at=BEFORE, ended_at=BOUNDARY, status="succeeded", elapsed_ms=5,
            processed_audio_samples=0, prompt_tokens=9, completion_tokens=2,
            aggregated_at=None))
        # Exercise missing, corrupt and stale projection keys. A running-only
        # key must be zero before recovery, then receive exactly one unknown.
        for user_id, key in ((first_user, MAIN), (first_user, STALE),
                             (first_user, RUNNING), (second_user, MAIN)):
            day, provider, model, purpose = key
            db.add(ModelUsageDaily(user_id=user_id, date=dt.date.fromisoformat(day),
                provider=provider, model_id=model, purpose=purpose,
                **dict.fromkeys(FIELDS, 999)))
        owner = await db.get(User, first_user)
        assert owner is not None
        owner.status = "disabled"  # Disabled tenants' permanent accounting remains.
        await db.commit()
    return ids


def prepared_database(startup_app):
    # Initial healthy lifespan ends before any maintenance/restart experiment.
    with TestClient(startup_app()) as client:
        wait_for_readiness(client)
        assert client.get("/health/ready").status_code == 200
        first = register_and_login(client, "ledger-first@example.com")["user"]["id"]
        second = register_and_login(client, "ledger-second@example.com")["user"]["id"]
        ids = client.portal.call(seed, client.app.state.db, first, second)
        before = {user_id: client.portal.call(source_rows, client.app.state.db, user_id)
                  for user_id in (first, second)}
        before_projection = {user_id: client.portal.call(projection, client.app.state.db, user_id)
                             for user_id in (first, second)}
    return first, second, ids, before, before_projection


def expected_first(*, recovered):
    return {
        MAIN: (4,1,3,1,100,0,100,20,2,2),
        (NEXT.isoformat(), PROVIDER, MODEL, "refine"): (1,1,0,0,7,0,0,0,0,0),
        (NEXT.isoformat(), PROVIDER, MODEL, "asr_finish"): (1,1,0,0,8,48000,0,0,1,1),
        (DAY.isoformat(), PROVIDER + "-other", MODEL, "refine"): (1,1,0,0,2,0,3,1,0,0),
        (DAY.isoformat(), PROVIDER, MODEL + "-other", "refine"): (1,1,0,0,3,0,4,2,0,0),
        RUNNING: (1,0,1,1,0,16000,0,0,1,1) if recovered else ZERO,
        STALE: ZERO,
    }


def test_terminal_rebuild_precedes_original_recovery_and_two_startups_do_not_double(startup_app, monkeypatch):
    first, second, ids, before, _ = prepared_database(startup_app)
    production_recovery = DatabaseCallRecorder.recover_incomplete
    recovered_counts = []
    observed_before_recovery = []

    async def observe_then_recover(recorder, *, identities=None):
        assert identities is not None, "startup must pass a frozen cohort, including the empty second startup"
        # Read-only observation at the real startup boundary; no arithmetic or
        # recovery implementation is mocked. The first pre-recovery running call
        # must not already be counted by the rebuild.
        first_projection = await projection(recorder.db, first)
        second_projection = await projection(recorder.db, second)
        observed_before_recovery.append((first_projection, second_projection))
        assert first_projection == expected_first(recovered=bool(recovered_counts))
        assert second_projection == {MAIN: (1,1,0,0,5,0,9,2,0,0)}
        current = await source_rows(recorder.db, first)
        if not recovered_counts:
            assert current[ids["running"]] == before[first][ids["running"]]
        result = await production_recovery(recorder, identities=identities)
        recovered_counts.append(result)
        return result

    monkeypatch.setattr(DatabaseCallRecorder, "recover_incomplete", observe_then_recover)
    stable_sources = None
    rebuild_started = dt.datetime.now(dt.timezone.utc)
    for _ in range(2):
        with TestClient(startup_app()) as client:
            wait_for_readiness(client)
            assert client.get("/health/ready").status_code == 200, observed_before_recovery
            database = client.app.state.db
            assert client.portal.call(projection, database, first) == expected_first(recovered=True)
            assert client.portal.call(projection, database, second) == {MAIN: (1,1,0,0,5,0,9,2,0,0)}
            current = {user_id: client.portal.call(source_rows, database, user_id)
                       for user_id in (first, second)}
            if stable_sources is not None:
                assert current == stable_sources  # Existing aggregation timestamps stay fixed.
            for user_id in (first, second):
                for call_id, original in before[user_id].items():
                    row = current[user_id][call_id]
                    if call_id == ids["running"]:
                        assert row["status"] == "unknown" and row["error_code"] == "SERVER_RESTARTED"
                        assert row["prompt_tokens"] is row["completion_tokens"] is None
                        assert row["started_at"] == original["started_at"]
                        assert row["processed_audio_samples"] == original["processed_audio_samples"]
                        assert row["ended_at"] is not None and row["aggregated_at"] is not None
                    else:
                        assert {k:v for k,v in row.items() if k != "aggregated_at"} == {
                            k:v for k,v in original.items() if k != "aggregated_at"}
                        assert row["aggregated_at"] is not None
                        if original["aggregated_at"] is not None:
                            assert row["aggregated_at"] == original["aggregated_at"]
                        else:
                            marked = dt.datetime.fromisoformat(row["aggregated_at"])
                            assert rebuild_started <= marked <= dt.datetime.now(dt.timezone.utc)
            stable_sources = current
            # Late/replayed completion cannot change an already unknown outcome
            # to invented success or add tokens/counts after startup recovery.
            client.portal.call(client.app.state.recorder.finished, ids["running"],
                               "succeeded", None, 999, 777, 888)
            assert client.portal.call(source_rows, database, first) == current[first]
            assert client.portal.call(projection, database, first) == expected_first(recovered=True)
    assert recovered_counts == [1, 0]


class FailAfterMarkerWrite:
    """Real 22012 after aggregate upserts AND marker update, before commit."""
    def __init__(self, engine, user_id):
        self.engine, self.user_id = engine, user_id
        self.upserts = self.fired = 0
        self.marker_written = False
        self.sqlstate = None

    def after_cursor(self, connection, cursor, statement, parameters, context, executemany):
        normalized = " ".join(statement.lower().split())
        values = [value for params in (getattr(context, "compiled_parameters", ()) or ())
                  for value in params.values()]
        if self.user_id not in values:
            return
        if normalized.startswith("insert into model_usage_daily "):
            self.upserts += 1
        if not self.fired and normalized.startswith("update model_calls set aggregated_at="):
            assert self.upserts > 0 and cursor.rowcount > 0
            self.marker_written = True
            self.fired += 1
            # Executed on the same real connection/transaction. No fabricated
            # Python DB exception, and no token/prompt/DSN in observations.
            connection.exec_driver_sql("SELECT 1 / 0")
            raise AssertionError("real PostgreSQL 22012 did not occur")

    def handle_error(self, context):
        if self.fired and context.statement == "SELECT 1 / 0":
            self.sqlstate = getattr(context.original_exception, "sqlstate", None)

    def install(self):
        event.listen(self.engine, "after_cursor_execute", self.after_cursor)
        event.listen(self.engine, "handle_error", self.handle_error)

    def remove(self):
        event.remove(self.engine, "handle_error", self.handle_error)
        event.remove(self.engine, "after_cursor_execute", self.after_cursor)


def test_real_pg_failure_rolls_back_one_users_projection_and_markers_then_restart_repairs(startup_app, monkeypatch):
    first, second, ids, before, before_projection = prepared_database(startup_app)
    production_rebuild = DatabaseCallRecorder.rebuild_usage_before_recovery
    # Fail the lexically first user so no unrelated earlier per-user transaction
    # has committed. The contract is per-user atomicity, not a giant global txn.
    target = min(first, second)
    observed = []

    async def fail_once(recorder):
        fault = FailAfterMarkerWrite(recorder.db.engine.sync_engine, target)
        observed.append(fault)
        fault.install()
        try:
            return await production_rebuild(recorder)
        finally:
            fault.remove()

    monkeypatch.setattr(DatabaseCallRecorder, "rebuild_usage_before_recovery", fail_once)
    with TestClient(startup_app()) as failed:
        assert failed.get("/health/ready").status_code == 503
        assert not failed.app.state.ready
        assert failed.app.state.unavailable_reason == "STARTUP_DEPENDENCY_UNAVAILABLE"
        assert len(observed) == 1
        fault = observed[0]
        assert fault.fired == 1 and fault.marker_written and fault.upserts > 0
        assert fault.sqlstate == "22012"
        for user_id in (first, second):
            assert failed.portal.call(source_rows, failed.app.state.db, user_id) == before[user_id]
            assert failed.portal.call(projection, failed.app.state.db, user_id) == before_projection[user_id]
    monkeypatch.setattr(DatabaseCallRecorder, "rebuild_usage_before_recovery", production_rebuild)
    with TestClient(startup_app()) as repaired:
        wait_for_readiness(repaired)
        assert repaired.get("/health/ready").status_code == 200
        assert repaired.portal.call(projection, repaired.app.state.db, first) == expected_first(recovered=True)
        assert repaired.portal.call(projection, repaired.app.state.db, second) == {MAIN: (1,1,0,0,5,0,9,2,0,0)}
        rows = repaired.portal.call(source_rows, repaired.app.state.db, first)
        assert rows[ids["running"]]["status"] == "unknown"


def test_empty_database_repeated_startup_does_not_manufacture_calls_or_usage(startup_app):
    async def empty(database):
        async with database.session_factory() as db:
            assert (await db.scalars(select(ModelCall.id))).all() == []
            assert (await db.scalars(select(ModelUsageDaily.user_id))).all() == []
    for _ in range(2):
        with TestClient(startup_app()) as client:
            wait_for_readiness(client)
            assert client.get("/health/ready").status_code == 200
            client.portal.call(empty, client.app.state.db)
