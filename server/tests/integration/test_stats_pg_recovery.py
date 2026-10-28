"""NFR-11 statistics recovery candidates; author has not executed these tests.

Uses the existing process-owned PostgreSQL fixture and real API transactions.
PCM/providers are synthetic. This does not validate model quality or a missing
a complete model-ledger projection rebuild.
"""
from __future__ import annotations

import hashlib
import json
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date

import pytest
from sqlalchemy import event, inspect, text
from sqlalchemy.orm import Session as OrmSession

from app.models import SessionContribution
from tests.conftest import auth_headers, register_and_login
from tests.ids import fixture_uuid
from tests.test_history_stats import run_session, today
from tests.test_insertions_learning import claim, receipt, wait_event


@pytest.fixture
def stats_client(monkeypatch, request):
    # Select deterministic providers before the existing app_client is created.
    # Keep its owned CREATE DATABASE, migrations, lifecycle and readiness loop.
    monkeypatch.setenv("SVI_ASR_PROVIDER", "mock")
    monkeypatch.setenv("SVI_LLM_PROVIDER", "mock")
    return request.getfixturevalue("app_client")


def _stats(client, headers, day):
    return client.get("/api/v1/stats/me", headers=headers,
                      params={"date_from": day, "date_to": day})


async def _owner(client, session_id):
    async with client.app.state.db.session_factory() as db:
        value = await db.scalar(text("SELECT user_id FROM sessions WHERE id=:s"), {"s": session_id})
        assert value is not None
        return value


async def _source_fingerprint(app, user_id):
    """Only counts/SHA256 leave this function; no source text in test output."""
    tables = {
        "contributions": ("session_contributions", "session_id"),
        "learning": ("learning_events", "id"),
        "terms": ("dictionary_terms", "id"),
        "dictionary_state": ("dictionary_state", "user_id"),
        "usage_daily": ("usage_daily", "date"),
    }
    answer = {}
    async with app.state.db.session_factory() as db:
        for name, (table, order) in tables.items():
            # All identifiers are the literal allowlist above, never user input.
            rows = list((await db.scalars(text(
                f"SELECT to_jsonb(t) FROM {table} AS t WHERE user_id=:u ORDER BY {order}"
            ), {"u": user_id})).all())
            encoded = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            answer[name] = {"count": len(rows), "sha256": hashlib.sha256(encoded).hexdigest()}
    return answer


async def _usage_day(app, user_id, day):
    day = date.fromisoformat(day) if isinstance(day, str) else day
    async with app.state.db.session_factory() as db:
        return await db.scalar(text(
            "SELECT to_jsonb(t) FROM usage_daily t WHERE user_id=:u AND date=CAST(:day AS date)"
        ), {"u": user_id, "day": day})


async def _receipt_rows(app, user_id, session_id, insertion_id):
    async with app.state.db.session_factory() as db:
        insertion = (await db.execute(text(
            "SELECT status,observable,error_code,completed_at FROM insertions "
            "WHERE user_id=:u AND session_id=:s AND id=:i"
        ), {"u": user_id, "s": session_id, "i": insertion_id})).one()
        contribution = (await db.execute(text(
            "SELECT inserted,revision FROM session_contributions WHERE user_id=:u AND session_id=:s"
        ), {"u": user_id, "s": session_id})).one()
        return {"insertion": tuple(insertion), "contribution": tuple(contribution)}


@dataclass
class PgFault:
    engine: object
    user_id: str
    target: str
    session_id: str | None = None
    insertion_id: str | None = None
    fired: int = 0
    sqlstate: str | None = None
    error_type: str | None = None
    readiness_select_completed: bool = False
    receipt_update_completed: bool = False
    dirty_contribution_observed: bool = False
    daily_update_completed: bool = False
    receipt_session: object | None = None
    targeted_statement_sha256: str | None = None
    listeners: list = field(default_factory=list)

    @staticmethod
    def _compiled_values(context):
        return [value for parameters in (getattr(context, "compiled_parameters", ()) or ())
                for value in parameters.values()]

    def before_cursor(self, connection, cursor, statement, parameters, context, executemany):
        normalized = " ".join(statement.lower().split())
        values = self._compiled_values(context)
        scoped = self.user_id in values
        contribution_range = ("from session_contributions" in normalized
                              and "session_contributions.business_date >=" in normalized
                              and "session_contributions.business_date <=" in normalized)
        applied_learning_range = ("count(learning_events.id)" in normalized
                                  and "learning_events.applied_at >=" in normalized
                                  and "learning_events.applied_at <" in normalized)
        target = ((self.target == "contribution_range" and contribution_range)
                  or (self.target == "applied_learning_range" and applied_learning_range))
        if not self.fired and scoped and target:
            assert not executemany
            self.fired += 1
            self.targeted_statement_sha256 = hashlib.sha256(statement.encode()).hexdigest()
            # The actual selected stats SQL reaches PostgreSQL as a real 22012,
            # rather than raising a fabricated Python exception or mocking stats.
            return "SELECT 1 / 0", ()
        return statement, parameters

    def after_cursor(self, connection, cursor, statement, parameters, context, executemany):
        normalized = " ".join(statement.lower().split())
        if self.sqlstate == "22012" and normalized == "select 1":
            # The unchanged lifespan readiness loop executes this real SELECT.
            self.readiness_select_completed = True
        values = self._compiled_values(context)
        if (self.target == "receipt_commit" and normalized.startswith("update insertions set ")
            and all(item in values for item in (self.user_id, self.session_id, self.insertion_id))):
            self.receipt_update_completed = True
        if (self.target == "receipt_commit" and normalized.startswith("insert into usage_daily ")
            and self.user_id in values):
            self.daily_update_completed = True

    def handle_error(self, context):
        if self.fired and context.statement == "SELECT 1 / 0":
            self.sqlstate = getattr(context.original_exception, "sqlstate", None)
            self.error_type = type(context.original_exception).__name__
        # Do not replace/suppress the real SQLAlchemy/driver exception.

    def before_flush(self, session, flush_context, instances):
        if self.target != "receipt_commit" or self.fired or session.get_bind() is not self.engine:
            return
        matches = [row for row in session.dirty if isinstance(row, SessionContribution)
                   and row.user_id == self.user_id and row.session_id == self.session_id
                   and tuple(inspect(row).attrs.inserted.history.added) == (True,)
                   and tuple(inspect(row).attrs.inserted.history.deleted) == (False,)]
        if matches:
            assert len(matches) == 1 and self.receipt_update_completed
            self.dirty_contribution_observed = True
            self.receipt_session = session

    def before_commit(self, session):
        if self.target != "receipt_commit" or self.fired or session.get_bind() is not self.engine:
            return
        # A projection upsert can autoflush the contribution before commit. Keep
        # the observed transition tied to this exact Session, not a later auth
        # transaction. Baseline's unflushed transition is observed here as well.
        if self.receipt_session is not session:
            self.before_flush(session, None, None)
        if self.receipt_session is not session:
            return
        self.fired += 1
        session.connection().exec_driver_sql("SELECT 1 / 0")
        raise AssertionError("PostgreSQL division by zero unexpectedly succeeded")

    def install(self):
        self.listeners = [
            (self.engine, "before_cursor_execute", self.before_cursor),
            (self.engine, "after_cursor_execute", self.after_cursor),
            (self.engine, "handle_error", self.handle_error),
            (OrmSession, "before_flush", self.before_flush),
            (OrmSession, "before_commit", self.before_commit),
        ]
        for target, name, listener in self.listeners:
            event.listen(target, name, listener, **({"retval": True} if name == "before_cursor_execute" else {}))

    def remove(self):
        for target, name, listener in reversed(self.listeners):
            event.remove(target, name, listener)
        self.listeners.clear()


@contextmanager
def _fault(client, user_id, target, **kwargs):
    control = PgFault(client.app.state.db.engine.sync_engine, user_id, target, **kwargs)
    # Register/remove on the application loop, never while its event dispatch
    # iterates on another thread. No readiness state or probe is replaced.
    client.portal.call(control.install)
    try:
        yield control
    finally:
        client.portal.call(control.remove)


def _assert_safe_failure(response, request_id, sensitive):
    assert response.status_code == 503
    assert response.headers["x-request-id"] == request_id
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"] == {"code": "SERVICE_UNAVAILABLE", "message": "数据服务暂时不可用",
                             "request_id": request_id, "retryable": True, "details": {}}
    for value in (*sensitive, "SELECT", "22012", "division by zero", "Traceback", "postgresql"):
        assert value not in response.text


def _wait_for_lifespan_readiness(client, control):
    # Harness anti-hang only: does not call _refresh_readiness or alter its
    # original 2-second cadence, dependency timeouts, providers or state flags.
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        response = client.get("/health/ready")
        if control.readiness_select_completed and response.status_code == 200:
            assert response.json()["dependencies"]["database"] == "ok"
            return
        time.sleep(0.05)
    raise AssertionError("formal lifespan readiness did not recover after the real SQL failure")


def _seed(client, *, learning):
    tokens = register_and_login(client, email="stats-recovery@example.com")
    headers = auth_headers(tokens)
    session, result = run_session(client, headers, "stats-recovery-session")
    assert result["review_required"] is False
    user_id = client.portal.call(_owner, client, session["session_id"])
    claimed = claim(client, headers, session["session_id"], result)
    assert claimed.status_code == 201 and claimed.json()["status"] == "reserved"
    insertion_id = claimed.json()["insertion_id"]
    if learning:
        done = receipt(client, headers, session["session_id"], insertion_id, "inserted", True)
        assert done.status_code == 200
        correction = client.post("/api/v1/corrections", headers=headers, json={
            "session_id": session["session_id"], "insertion_id": insertion_id,
            "correction_id": fixture_uuid("nfr11-stats-learning"),
            "offset": result["refined_text"].index("按钮"),
            "before": "按钮", "after": "按键", "left": "", "right": "",
        })
        assert correction.status_code == 202
        event_body = wait_event(client, headers, correction.json()["event_id"])
        assert event_body["status"] == "suggested"
        confirmed = client.post(f"/api/v1/learning-events/{event_body['event_id']}/confirm",
            headers={**headers, "Idempotency-Key": fixture_uuid("nfr11-stats-confirm")})
        assert confirmed.status_code == 200 and confirmed.json()["status"] == "learned"
    return tokens, headers, user_id, session, result, insertion_id


@pytest.mark.parametrize("target", ["contribution_range", "applied_learning_range"])
def test_stats_query_real_pg_failure_recomputes_same_day_without_source_changes(stats_client, target, caplog):
    client = stats_client
    tokens, headers, user_id, session, result, insertion_id = _seed(client, learning=True)
    day = today()
    before_response = _stats(client, headers, day)
    assert before_response.status_code == 200
    baseline = before_response.json()
    assert baseline["totals"]["sessions"] == baseline["totals"]["result_sessions"] == 1
    assert baseline["totals"]["inserted_sessions"] == baseline["totals"]["learning_events"] == 1
    assert baseline["totals"]["received_audio_ms"] == 500 and baseline["totals"]["chars"] > 0
    before = client.portal.call(_source_fingerprint, client.app, user_id)
    assert before["contributions"]["count"] == before["learning"]["count"] == 1
    request_id = fixture_uuid(f"nfr11-query-{target}")
    with _fault(client, user_id, target) as control:
        failed = _stats(client, {**headers, "X-Request-Id": request_id}, day)
        _assert_safe_failure(failed, request_id, (result["raw_text"], result["refined_text"],
                                                tokens["access_token"], tokens["refresh_token"]))
        assert control.fired == 1 and control.sqlstate == "22012"
        for secret in (result["raw_text"], result["refined_text"], tokens["access_token"], tokens["refresh_token"]):
            assert secret not in caplog.text
        assert control.targeted_statement_sha256 and control.error_type
        assert client.portal.call(_source_fingerprint, client.app, user_id) == before
        _wait_for_lifespan_readiness(client, control)
        for _ in range(2):
            recovered = _stats(client, headers, day)
            assert recovered.status_code == 200 and recovered.json() == baseline
        assert control.fired == 1
        assert client.portal.call(_source_fingerprint, client.app, user_id) == before


def test_receipt_contribution_commit_real_pg_failure_rolls_back_then_replays_once(stats_client, caplog):
    client = stats_client
    tokens, headers, user_id, session, result, insertion_id = _seed(client, learning=False)
    session_id, day = session["session_id"], today()
    before_response = _stats(client, headers, day)
    assert before_response.status_code == 200
    baseline = before_response.json()
    assert baseline["totals"]["result_sessions"] == 1 and baseline["totals"]["inserted_sessions"] == 0
    before = client.portal.call(_source_fingerprint, client.app, user_id)
    rows_before = client.portal.call(_receipt_rows, client.app, user_id, session_id, insertion_id)
    assert rows_before["insertion"] == ("reserved", False, None, None)
    assert rows_before["contribution"][0] is False
    daily_before = client.portal.call(_usage_day, client.app, user_id, day)
    assert daily_before is not None and daily_before["inserted_sessions"] == 0
    request_id = fixture_uuid("nfr11-receipt-failed-commit")
    with _fault(client, user_id, "receipt_commit", session_id=session_id, insertion_id=insertion_id) as control:
        failed = receipt(client, {**headers, "X-Request-Id": request_id}, session_id, insertion_id, "inserted", True)
        _assert_safe_failure(failed, request_id, (result["raw_text"], result["refined_text"],
                                                tokens["access_token"], tokens["refresh_token"]))
        assert control.fired == 1 and control.sqlstate == "22012"
        for secret in (result["raw_text"], result["refined_text"], tokens["access_token"], tokens["refresh_token"]):
            assert secret not in caplog.text
        assert control.receipt_update_completed and control.dirty_contribution_observed
        assert control.daily_update_completed
        assert client.portal.call(_receipt_rows, client.app, user_id, session_id, insertion_id) == rows_before
        assert client.portal.call(_source_fingerprint, client.app, user_id) == before
        _wait_for_lifespan_readiness(client, control)
        before_retry = _stats(client, headers, day)
        assert before_retry.status_code == 200 and before_retry.json() == baseline
        reserved = client.get(f"/api/v1/sessions/{session_id}/insertions/{insertion_id}", headers=headers)
        assert reserved.status_code == 200 and reserved.json()["status"] == "reserved"

        accepted = receipt(client, headers, session_id, insertion_id, "inserted", True)
        assert accepted.status_code == 200 and accepted.json()["status"] == "inserted"
        assert accepted.json()["replayed"] is False and accepted.json()["completed_at"]
        rows_after = client.portal.call(_receipt_rows, client.app, user_id, session_id, insertion_id)
        assert rows_after["insertion"][:3] == ("inserted", True, None)
        assert rows_after["contribution"] == (True, rows_before["contribution"][1] + 1)
        after_response = _stats(client, headers, day)
        assert after_response.status_code == 200
        after = after_response.json()
        expected = json.loads(json.dumps(baseline))
        expected["totals"].update(inserted_sessions=1, insertion_success_rate=1.0)
        assert after == expected
        sources_after = client.portal.call(_source_fingerprint, client.app, user_id)
        assert sources_after["contributions"] != before["contributions"]
        assert sources_after["usage_daily"] != before["usage_daily"]
        assert all(sources_after[name] == before[name] for name in before
                   if name not in ("contributions", "usage_daily"))
        expected_daily = {**daily_before, "inserted_sessions": 1}
        assert client.portal.call(_usage_day, client.app, user_id, day) == expected_daily

        replayed = receipt(client, headers, session_id, insertion_id, "inserted", True)
        assert replayed.status_code == 200 and replayed.json()["replayed"] is True
        assert replayed.json()["completed_at"] == accepted.json()["completed_at"]
        assert client.portal.call(_receipt_rows, client.app, user_id, session_id, insertion_id) == rows_after
        again = _stats(client, headers, day)
        assert again.status_code == 200 and again.json() == after
        assert client.portal.call(_source_fingerprint, client.app, user_id) == sources_after
        assert control.fired == 1
