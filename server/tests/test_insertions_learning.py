from __future__ import annotations

import time
import uuid

from tests.conftest import auth_headers, register_and_login
from tests.test_sessions import create_session, dictation


def key() -> str:
    return str(uuid.uuid4())


def complete_dictation(app_client, request_id: str):
    tokens = register_and_login(app_client)
    headers = auth_headers(tokens)
    session = create_session(app_client, headers, request_id).json()
    events = dictation(app_client, headers, session["session_id"])
    result = events[-1]["result"]
    return headers, session, result


def claim(app_client, headers, session_id, result, insertion_id=None, review_confirmed=False):
    return app_client.post(
        f"/api/v1/sessions/{session_id}/insertions/claim",
        json={
            "result_id": result["result_id"],
            "insertion_id": insertion_id or key(),
            "text_hash": result["text_hash"],
            "review_confirmed": review_confirmed,
        },
        headers=headers,
    )


def receipt(app_client, headers, session_id, insertion_id, status, observable=False, error_code=None):
    return app_client.post(
        f"/api/v1/sessions/{session_id}/insertions/{insertion_id}/receipt",
        json={"status": status, "observable": observable, "error_code": error_code},
        headers=headers,
    )


def wait_event(app_client, headers, event_id, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = app_client.get(f"/api/v1/learning-events/{event_id}", headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        if body["status"] != "pending":
            return body
        time.sleep(0.1)
    raise AssertionError("learning event stayed pending")


def test_claim_and_receipt_flow(app_client):
    headers, session, result = complete_dictation(app_client, "insert-flow")
    insertion_id = key()
    first = claim(app_client, headers, session["session_id"], result, insertion_id)
    assert first.status_code == 201, first.text
    assert first.json()["status"] == "reserved"
    assert first.json()["observable"] is False

    replay = claim(app_client, headers, session["session_id"], result, insertion_id)
    assert replay.status_code == 200
    assert replay.json()["insertion_id"] == insertion_id

    other = claim(app_client, headers, session["session_id"], result)
    assert other.status_code == 409
    assert other.json()["error"]["code"] == "INSERTION_ALREADY_CLAIMED"

    wrong_hash = app_client.post(
        f"/api/v1/sessions/{session['session_id']}/insertions/claim",
        json={
            "result_id": result["result_id"],
            "insertion_id": key(),
            "text_hash": "0" * 64,
            "review_confirmed": False,
        },
        headers=headers,
    )
    assert wrong_hash.status_code == 409

    done = receipt(app_client, headers, session["session_id"], insertion_id, "inserted", True)
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "inserted"
    assert done.json()["observable"] is True
    assert done.json()["completed_at"]

    same = receipt(app_client, headers, session["session_id"], insertion_id, "inserted", True)
    assert same.status_code == 200
    conflict = receipt(app_client, headers, session["session_id"], insertion_id, "failed", False, "ZERO_WRITE")
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "INSERTION_STATE_CONFLICT"

    fetched = app_client.get(
        f"/api/v1/sessions/{session['session_id']}/insertions/{insertion_id}", headers=headers
    )
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "inserted"


def test_receipt_validation(app_client):
    headers, session, result = complete_dictation(app_client, "receipt-validation")
    insertion_id = claim(app_client, headers, session["session_id"], result).json()["insertion_id"]

    bad = receipt(app_client, headers, session["session_id"], insertion_id, "inserted", True, "ZERO_WRITE")
    assert bad.status_code == 422
    needs_code = receipt(app_client, headers, session["session_id"], insertion_id, "failed")
    assert needs_code.status_code == 422
    bad_code = receipt(app_client, headers, session["session_id"], insertion_id, "failed", False, "NOPE")
    assert bad_code.status_code == 422
    not_inserted_observable = receipt(
        app_client, headers, session["session_id"], insertion_id, "failed", True, "ZERO_WRITE"
    )
    assert not_inserted_observable.status_code == 422


def test_correction_learns_term_and_undoes(app_client):
    headers, session, result = complete_dictation(app_client, "learning-flow")
    insertion_id = claim(app_client, headers, session["session_id"], result).json()["insertion_id"]
    assert receipt(
        app_client, headers, session["session_id"], insertion_id, "inserted", True
    ).status_code == 200

    text = result["refined_text"]
    before, after = "按钮", "按键"
    offset = text.index(before)
    correction_id = key()
    response = app_client.post(
        "/api/v1/corrections",
        json={
            "session_id": session["session_id"],
            "insertion_id": insertion_id,
            "correction_id": correction_id,
            "offset": offset,
            "before": before,
            "after": after,
            "left": text[max(0, offset - 4) : offset],
            "right": text[offset + len(before) : offset + len(before) + 4],
        },
        headers=headers,
    )
    assert response.status_code == 202, response.text
    event = wait_event(app_client, headers, response.json()["event_id"])
    assert event["status"] == "suggested", event
    assert event["classification"] == "suggest"
    assert event["undo_available"] is False

    confirmed = app_client.post(
        f"/api/v1/learning-events/{event['event_id']}/confirm",
        headers={**headers, "Idempotency-Key": key()},
    )
    assert confirmed.status_code == 200, confirmed.text
    learned = confirmed.json()
    assert learned["status"] == "learned"
    assert learned["term"]["term"] == after
    assert before in learned["term"]["aliases"]
    assert learned["undo_available"] is True
    assert learned["after_version"] == 1

    listed = app_client.get("/api/v1/dictionary/terms", headers=headers).json()
    assert [item["term"] for item in listed["items"]] == [after]
    dictionary_version = listed["dictionary_version"]

    undone = app_client.post(
        f"/api/v1/learning-events/{event['event_id']}/undo",
        headers={**headers, "Idempotency-Key": key()},
    )
    assert undone.status_code == 200, undone.text
    assert undone.json()["undone"] is True
    assert undone.json()["term"] is None
    assert undone.json()["dictionary_version"] == dictionary_version + 1
    after_undo = app_client.get("/api/v1/dictionary/terms", headers=headers).json()
    assert after_undo["items"] == []


def test_correction_guard_ignores_numbers(app_client):
    headers, session, result = complete_dictation(app_client, "guard-flow")
    insertion_id = claim(app_client, headers, session["session_id"], result).json()["insertion_id"]
    assert receipt(
        app_client, headers, session["session_id"], insertion_id, "inserted", True
    ).status_code == 200
    text = result["refined_text"]
    offset = text.index("一")
    response = app_client.post(
        "/api/v1/corrections",
        json={
            "session_id": session["session_id"],
            "insertion_id": insertion_id,
            "correction_id": key(),
            "offset": offset,
            "before": "一",
            "after": "二",
        },
        headers=headers,
    )
    assert response.status_code == 202
    assert response.json()["status"] == "ignored"


def test_correction_rejects_wrong_offset(app_client):
    headers, session, result = complete_dictation(app_client, "offset-flow")
    insertion_id = claim(app_client, headers, session["session_id"], result).json()["insertion_id"]
    assert receipt(
        app_client, headers, session["session_id"], insertion_id, "inserted", True
    ).status_code == 200
    response = app_client.post(
        "/api/v1/corrections",
        json={
            "session_id": session["session_id"],
            "insertion_id": insertion_id,
            "correction_id": key(),
            "offset": 0,
            "before": "不存在的片段",
            "after": "替代",
        },
        headers=headers,
    )
    assert response.status_code == 422


def test_correction_rejected_when_learning_disabled(app_client):
    headers, session, result = complete_dictation(app_client, "disabled-flow")
    insertion_id = claim(app_client, headers, session["session_id"], result).json()["insertion_id"]
    assert receipt(
        app_client, headers, session["session_id"], insertion_id, "inserted", True
    ).status_code == 200
    settings = app_client.get("/api/v1/settings", headers=headers).json()
    assert app_client.patch(
        "/api/v1/settings",
        json={"version": settings["version"], "learning_enabled": False},
        headers=headers,
    ).status_code == 200
    text = result["refined_text"]
    offset = text.index("按钮")
    response = app_client.post(
        "/api/v1/corrections",
        json={
            "session_id": session["session_id"],
            "insertion_id": insertion_id,
            "correction_id": key(),
            "offset": offset,
            "before": "按钮",
            "after": "按键",
        },
        headers=headers,
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "LEARNING_NOT_ALLOWED"
