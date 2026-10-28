from __future__ import annotations

from tests.ids import fixture_uuid

import datetime as dt
from zoneinfo import ZoneInfo

from tests.conftest import auth_headers, execute_sql, register_and_login
from tests.test_insertions_learning import claim, receipt, wait_event
from tests.test_sessions import create_session, dictation


def today() -> str:
    return dt.datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def run_session(app_client, headers, request_id):
    session = create_session(app_client, headers, request_id).json()
    events = dictation(app_client, headers, session["session_id"])
    return session, events[-1]["result"]


def test_history_orders_and_paginates(app_client):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    first, _ = run_session(app_client, headers, "history-one")
    second, _ = run_session(app_client, headers, "history-two")

    page = app_client.get("/api/v1/history", headers=headers).json()
    assert [item["session_id"] for item in page["items"]] == [
        second["session_id"],
        first["session_id"],
    ]
    assert page["next_cursor"] is None

    first_page = app_client.get(
        "/api/v1/history", params={"limit": 1}, headers=headers
    ).json()
    assert len(first_page["items"]) == 1
    assert first_page["next_cursor"]
    second_page = app_client.get(
        "/api/v1/history",
        params={"limit": 1, "cursor": first_page["next_cursor"]},
        headers=headers,
    ).json()
    assert [item["session_id"] for item in second_page["items"]] == [first["session_id"]]


def test_history_hides_expired_content(app_client):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    session, _ = run_session(app_client, headers, "history-expired")
    execute_sql(
        "UPDATE sessions SET content_cleared_at=now() WHERE id=$1",
        session["session_id"],
    )
    page = app_client.get("/api/v1/history", headers=headers).json()
    assert page["items"] == []


def test_stats_me_reflects_session_insertion_and_learning(app_client):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    session, result = run_session(app_client, headers, "stats-session")
    insertion_id = claim(app_client, headers, session["session_id"], result).json()[
        "insertion_id"
    ]
    assert receipt(
        app_client, headers, session["session_id"], insertion_id, "inserted", True
    ).status_code == 200

    text = result["refined_text"]
    offset = text.index("按钮")
    correction = app_client.post(
        "/api/v1/corrections",
        json={
            "session_id": session["session_id"],
            "insertion_id": insertion_id,
            "correction_id": fixture_uuid("stats-correction-1"),
            "offset": offset,
            "before": "按钮",
            "after": "按键",
            "left": "",
            "right": "",
        },
        headers=headers,
    )
    assert correction.status_code == 202

    event = wait_event(app_client, headers, correction.json()["event_id"])
    assert event["status"] == "suggested"
    confirmed = app_client.post(f"/api/v1/learning-events/{event['event_id']}/confirm",
        headers={**headers, "Idempotency-Key": fixture_uuid("stats-confirm-1")})
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "learned"
    undone = app_client.post(f"/api/v1/learning-events/{event['event_id']}/undo",
        headers={**headers, "Idempotency-Key": fixture_uuid("stats-undo-1")})
    assert undone.status_code == 200 and undone.json()["undone"] is True

    day = today()
    stats = app_client.get(
        "/api/v1/stats/me",
        params={"date_from": day, "date_to": day},
        headers=headers,
    )
    assert stats.status_code == 200, stats.text
    body = stats.json()
    assert body["timezone"] == "Asia/Shanghai"
    assert body["totals"]["sessions"] == 1
    assert body["totals"]["result_sessions"] == 1
    assert body["totals"]["inserted_sessions"] == 1
    assert body["totals"]["chars"] > 0
    assert body["totals"]["received_audio_ms"] == 500
    assert body["totals"]["processing_success_rate"] == 1.0
    assert body["totals"]["insertion_success_rate"] == 1.0
    assert body["totals"]["learning_events"] == 1
    assert body["active_days_in_range"] == 1
    assert body["current_streak_days"] == 1
    assert body["longest_streak_days"] == 1
    assert len(body["daily"]) == 1
    assert body["daily"][0]["heat_level"] == 1
    assert body["dictionary_terms"] == 0


def test_stats_validation(app_client):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    bad = app_client.get(
        "/api/v1/stats/me",
        params={"date_from": "2026-10-17", "date_to": "2026-10-01"},
        headers=headers,
    )
    assert bad.status_code == 422
    too_long = app_client.get(
        "/api/v1/stats/me",
        params={"date_from": "2024-01-01", "date_to": "2026-10-17"},
        headers=headers,
    )
    assert too_long.status_code == 422
