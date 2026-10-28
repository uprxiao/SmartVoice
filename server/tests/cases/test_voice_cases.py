from __future__ import annotations

from tests.conftest import auth_headers
from tests.cases.test_dict_cases import fetch_one, new_user
from tests.test_sessions import create_session, dictation


def test_tc_voice_20_terminal_cancel_returns_conflict(app_client):
    tokens = new_user(app_client, "voice20@example.com", "voice20-d1")
    headers = auth_headers(tokens)
    session = create_session(app_client, headers, "voice20-req").json()
    session_id = session["session_id"]
    events = dictation(app_client, headers, session_id, chunks=2)
    assert any(event.get("type") == "result.ready" for event in events)
    result_id = events[-1]["result"]["result_id"]

    cancelled = app_client.post(f"/api/v1/sessions/{session_id}/cancel", headers=headers)
    assert cancelled.status_code == 409, cancelled.text
    assert cancelled.json()["error"]["code"] == "SESSION_TERMINAL"

    # "Close result" is a renderer-only action; the server keeps the terminal
    # session and its current result unchanged.
    detail = app_client.get(f"/api/v1/sessions/{session_id}", headers=headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["state"] == "ready"
    assert detail.json()["current_result_id"] == result_id
    assert (
        fetch_one(
            "select count(*)::int from results where session_id=$1", session_id
        )
        == 1
    )


def test_tc_voice_07_cancel_is_idempotent_and_has_no_side_effects(app_client):
    tokens = new_user(app_client, "voice07@example.com", "voice07-d1")
    headers = auth_headers(tokens)
    session = create_session(app_client, headers, "voice07-req").json()
    session_id = session["session_id"]

    first = app_client.post(f"/api/v1/sessions/{session_id}/cancel", headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()["state"] == "cancelled"

    # Replaying cancel on an already-cancelled session is idempotent and adds
    # no result, insertion, or learning event.
    second = app_client.post(f"/api/v1/sessions/{session_id}/cancel", headers=headers)
    assert second.status_code == 200, second.text
    assert second.json()["state"] == "cancelled"

    assert fetch_one("select state from sessions where id=$1", session_id) == "cancelled"
    assert (
        fetch_one("select count(*)::int from results where session_id=$1", session_id) == 0
    )
    assert (
        fetch_one("select count(*)::int from insertions where session_id=$1", session_id)
        == 0
    )
    assert (
        fetch_one(
            "select count(*)::int from learning_events where session_id=$1", session_id
        )
        == 0
    )
