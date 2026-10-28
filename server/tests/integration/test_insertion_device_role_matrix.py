"""Real API/PG control matrix; synthetic PCM/providers, no native insertion claim.

Prepared offline. Root must compile and execute with the existing process-owned
PostgreSQL fixture. No session/result/insertion/learning terminal row is seeded.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

from tests.conftest import auth_headers, execute_sql
from tests.cases.test_dict_cases import PASSWORD, fetch_one, key, login, new_user
from tests.cases.test_learn_cases import set_classifier, submit, wait_event
from tests.test_data_integrity_regressions import concurrent
from tests.test_insertions_learning import claim, receipt
from tests.test_sessions import create_session, dictation


TEXT = "请使用 SQL lite 数据库。"
BUSINESS_TABLES = (
    "sessions", "session_attempts", "results", "insertions",
    "session_contributions", "usage_daily", "dictionary_state",
    "dictionary_terms", "learning_events", "model_calls",
    "model_usage_daily", "operation_requests",
)
# One PG statement/snapshot; auth/session last_used_at is intentionally excluded.
# Table identifiers are the fixed tuple above, never request-controlled text.
SNAPSHOT_SQL = "SELECT jsonb_build_object(" + ",".join(
    f"'{table}', (SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text), "
    f"'[]'::jsonb) FROM {table} t WHERE t.user_id=ANY($1::varchar[]))"
    for table in BUSINESS_TABLES
) + ")::text"


def business_snapshot(*user_ids):
    return json.loads(fetch_one(SNAPSHOT_SQL, list(user_ids)))


def stats(client, headers, session_id):
    day = fetch_one(
        "SELECT business_date FROM session_contributions WHERE session_id=$1",
        session_id,
    )
    assert day is not None
    response = client.get("/api/v1/stats/me", headers=headers, params={
        "date_from": (day - dt.timedelta(days=1)).isoformat(),
        "date_to": (day + dt.timedelta(days=1)).isoformat(),
    })
    assert response.status_code == 200, response.text
    return response.json()["totals"]


def ready(client, headers):
    client.app.state.sessions.asr_provider.text = TEXT
    created = create_session(client, headers, key())
    assert created.status_code == 201, created.text
    session = created.json()
    events = dictation(client, headers, session["session_id"])
    assert events[-1]["type"] == "result.ready", events[-1]
    result = events[-1]["result"]
    assert result["raw_text"] == TEXT and result["refined_text"] == TEXT
    assert result["review_required"] is False
    fetched = client.get(f"/api/v1/sessions/{session['session_id']}", headers=headers)
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["state"] == "ready"
    assert fetched.json()["result"]["result_id"] == result["result_id"]
    return session, result


def counted_classifier(client, monkeypatch):
    # Existing deterministic classifier fixture only; no fabricated model_calls.
    set_classifier(client, monkeypatch, decision="term", ambiguous=False)
    original = client.app.state.classifier.classify
    calls = []

    async def counted(before, after, left, right):
        calls.append((before, after))
        return await original(before, after, left, right)

    monkeypatch.setattr(client.app.state.classifier, "classify", counted)
    return calls


def correct(client, headers, session, result, correction_id=None):
    return submit(client, headers, session, result, "SQL lite", "SQLite",
                  correction_id=correction_id or key())


def denied_without_effect(client, call, status, code, user_ids,
                          owner_headers, session_id, classifier_calls):
    before = business_snapshot(*user_ids)
    totals = stats(client, owner_headers, session_id)
    response = call()
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code
    assert response.json()["error"]["retryable"] is False
    assert response.json()["error"]["request_id"]
    assert business_snapshot(*user_ids) == before
    assert stats(client, owner_headers, session_id) == totals
    assert classifier_calls == []
    return response


def test_tc_insert_19_two_installations_role_and_tenant_matrix(app_client, monkeypatch):
    calls = counted_classifier(app_client, monkeypatch)
    email = f"insert19-{key()}@example.com"
    installation_ids = [key(), key()]
    first = new_user(app_client, email, installation_ids[0])
    second_login = login(app_client, email, installation_ids[1])
    assert second_login.status_code == 200, second_login.text
    second = second_login.json()
    owner_id = first["user"]["id"]
    assert second["user"]["id"] == owner_id
    desktop_headers = [auth_headers(first), auth_headers(second)]
    device_ids = [fetch_one(
        "SELECT id FROM devices WHERE user_id=$1 AND installation_id=$2",
        owner_id, installation,
    ) for installation in installation_ids]
    assert all(device_ids) and device_ids[0] != device_ids[1]

    # Only role setup is synthetic, as in existing login_admin. Credentials,
    # admin_web session and its no-device identity come from the formal login.
    execute_sql("UPDATE users SET role='admin' WHERE id=$1", owner_id)
    admin_login = app_client.post("/api/auth/login", json={
        "email": email, "password": PASSWORD, "client_type": "admin_web",
    })
    assert admin_login.status_code == 200, admin_login.text
    admin_headers = auth_headers(admin_login.json())
    admin_ok = app_client.get("/api/admin/users", headers=admin_headers)
    assert admin_ok.status_code == 200, admin_ok.text
    other = new_user(app_client, f"other-{key()}@example.com", key())
    other_id = other["user"]["id"]
    other_headers = auth_headers(other)
    assert app_client.get("/api/v1/settings", headers=other_headers).status_code == 200

    session, result = ready(app_client, desktop_headers[0])
    session_id = session["session_id"]
    before_claim = business_snapshot(owner_id, other_id)
    assert before_claim["insertions"] == before_claim["learning_events"] == []
    assert before_claim["dictionary_terms"] == []
    assert all(row["version"] == 0 for row in before_claim["dictionary_state"])
    insertion_ids = [key(), key()]
    responses = concurrent(*[
        (lambda index=index: claim(app_client, desktop_headers[index], session_id,
                                  result, insertion_ids[index]))
        for index in range(2)
    ])
    assert sorted(response.status_code for response in responses) == [201, 409]
    winner = next(index for index, response in enumerate(responses) if response.status_code == 201)
    loser = 1 - winner
    assert responses[loser].json()["error"]["code"] == "INSERTION_ALREADY_CLAIMED"
    assert responses[winner].json()["status"] == "reserved"
    assert responses[winner].json()["replayed"] is False
    insertion_id = responses[winner].json()["insertion_id"]
    assert insertion_id == insertion_ids[winner]
    session["insertion_id"] = insertion_id
    after_claim = business_snapshot(owner_id, other_id)
    assert len(after_claim["insertions"]) == 1
    row = after_claim["insertions"][0]
    assert (row["id"], row["user_id"], row["session_id"], row["result_id"], row["device_id"]) == (
        insertion_id, owner_id, session_id, result["result_id"], device_ids[winner],
    )
    assert row["status"] == "reserved" and row["observable"] is False and row["completed_at"] is None
    for table in BUSINESS_TABLES:
        if table != "insertions":
            assert after_claim[table] == before_claim[table], table
    assert stats(app_client, desktop_headers[winner], session_id)["inserted_sessions"] == 0

    # Simultaneous claim is real HTTP with a two-thread start barrier; neither
    # terminal state nor the winning device is forced through a DB update.
    for actor, actor_headers, denied_status, denied_code in (
        ("nonwinning_device", desktop_headers[loser], 403, "FORBIDDEN"),
        ("same_account_admin_web", admin_headers, 403, "FORBIDDEN"),
        ("different_tenant_desktop", other_headers, 404, "NOT_FOUND"),
    ):
        operations = {
            "receipt": lambda h=actor_headers: receipt(app_client, h, session_id, insertion_id, "inserted", True),
            "correction": lambda h=actor_headers: correct(app_client, h, session, result),
        }
        if actor != "nonwinning_device":
            operations["claim"] = lambda h=actor_headers: claim(app_client, h, session_id, result, key())
        for operation, invoke in operations.items():
            denied_without_effect(app_client, invoke, denied_status, denied_code,
                                  (owner_id, other_id), desktop_headers[winner], session_id, calls)

    winner_headers = desktop_headers[winner]
    positive_receipt = receipt(app_client, winner_headers, session_id, insertion_id, "inserted", True)
    assert positive_receipt.status_code == 200, positive_receipt.text
    assert positive_receipt.json()["status"] == "inserted"
    assert positive_receipt.json()["observable"] is True
    assert positive_receipt.json()["replayed"] is False
    inserted = business_snapshot(owner_id, other_id)
    assert inserted["session_contributions"][0]["inserted"] is True
    assert sum(row["inserted_sessions"] for row in inserted["usage_daily"]) == 1
    assert stats(app_client, winner_headers, session_id)["inserted_sessions"] == 1
    assert inserted["model_calls"] == before_claim["model_calls"]

    # Check the device rule again on the now-eligible insertion, rather than
    # accepting a 409 eligibility rejection as evidence of device isolation.
    for invoke in (
        lambda: receipt(app_client, desktop_headers[loser], session_id, insertion_id, "inserted", True),
        lambda: correct(app_client, desktop_headers[loser], session, result),
    ):
        denied_without_effect(app_client, invoke, 403, "FORBIDDEN", (owner_id, other_id),
                              winner_headers, session_id, calls)
    replay = receipt(app_client, winner_headers, session_id, insertion_id, "inserted", True)
    assert replay.status_code == 200 and replay.json()["replayed"] is True
    assert business_snapshot(owner_id, other_id) == inserted

    accepted = correct(app_client, winner_headers, session, result)
    assert accepted.status_code == 202, accepted.text
    event = wait_event(app_client, winner_headers, accepted.json()["event_id"])
    assert event["status"] == "learned", event
    assert event["term"]["term"] == "SQLite"
    assert calls == [("SQL lite", "SQLite")]
    final = business_snapshot(owner_id, other_id)
    assert len(final["insertions"]) == len(final["learning_events"]) == len(final["dictionary_terms"]) == 1
    assert final["insertions"] == inserted["insertions"]
    assert final["learning_events"][0]["user_id"] == owner_id
    assert final["learning_events"][0]["insertion_id"] == insertion_id
    versions = {row["user_id"]: row["version"] for row in final["dictionary_state"]}
    assert versions == {owner_id: 1, other_id: 0}
    assert all(row["user_id"] == owner_id for row in final["dictionary_terms"])
    assert final["model_calls"] == inserted["model_calls"]  # classifier is explicitly a stub
    totals = stats(app_client, winner_headers, session_id)
    assert (totals["sessions"], totals["result_sessions"], totals["inserted_sessions"], totals["learning_events"]) == (1, 1, 1, 1)


@pytest.mark.parametrize("status,observable,error_code,inserted_count", [
    ("failed", False, "ZERO_WRITE", 0),
    ("unverified", False, "VERIFY_FAILED", 0),
    ("inserted", False, None, 1),
])
def test_tc_learn_08_terminal_unobservable_receipts_cannot_learn(
    app_client, monkeypatch, status, observable, error_code, inserted_count,
):
    calls = counted_classifier(app_client, monkeypatch)
    tokens = new_user(app_client, f"learn08-{key()}@example.com", key())
    headers, user_id = auth_headers(tokens), tokens["user"]["id"]
    session, result = ready(app_client, headers)
    insertion_id = key()
    claimed = claim(app_client, headers, session["session_id"], result, insertion_id)
    assert claimed.status_code == 201, claimed.text
    session["insertion_id"] = insertion_id
    saved = receipt(app_client, headers, session["session_id"], insertion_id,
                    status, observable, error_code)
    assert saved.status_code == 200, saved.text
    assert saved.json()["status"] == status and saved.json()["observable"] is False
    assert saved.json()["completed_at"] is not None
    before = business_snapshot(user_id)
    assert before["learning_events"] == before["dictionary_terms"] == []
    assert before["dictionary_state"][0]["version"] == 0
    assert stats(app_client, headers, session["session_id"])["inserted_sessions"] == inserted_count
    denied_without_effect(app_client, lambda: correct(app_client, headers, session, result),
                          409, "LEARNING_NOT_ALLOWED", (user_id,), headers,
                          session["session_id"], calls)
    assert business_snapshot(user_id) == before
    assert calls == []
