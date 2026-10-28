from __future__ import annotations

import asyncio
import time
import uuid

from app.services.classifier import Classification
from tests.conftest import auth_headers, execute_sql
from tests.cases.test_dict_cases import fetch_one, key, login, new_user
from tests.test_sessions import create_session, dictation

PASSWORD = "Password123"


def wait_event(client, headers, event_id, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/learning-events/{event_id}", headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        if body["status"] != "pending":
            return body
        time.sleep(0.05)
    raise AssertionError("learning event stayed pending")


def set_text(app_client, text: str) -> None:
    app_client.app.state.sessions.asr_provider.text = text


def set_classifier(
    app_client,
    monkeypatch,
    decision="term",
    ambiguous=False,
    delay=0.0,
    evidence="match",
):
    async def fake(before, after, left, right):
        if delay:
            await asyncio.sleep(delay)
        payload = (
            {"before_start": 0, "before_end": len(before), "before": before, "after": after}
            if evidence == "match"
            else {"before": "wrong", "after": "wrong"}
        )
        return Classification(
            decision=decision,
            ambiguous=ambiguous,
            category="proper_term",
            rationale="test classifier",
            evidence=payload,
        )

    monkeypatch.setattr(app_client.app.state.classifier, "classify", fake)


def prepare_inserted(app_client, text, request_id="learn-session"):
    set_text(app_client, text)
    tokens = new_user(app_client, f"{request_id}@example.com", f"{request_id}-d1")
    headers = auth_headers(tokens)
    session = create_session(app_client, headers, request_id).json()
    events = dictation(app_client, headers, session["session_id"])
    result = events[-1]["result"]
    insertion_id = key()
    claimed = app_client.post(
        f"/api/v1/sessions/{session['session_id']}/insertions/claim",
        json={
            "result_id": result["result_id"],
            "insertion_id": insertion_id,
            "text_hash": result["text_hash"],
            "review_confirmed": True,
        },
        headers=headers,
    )
    assert claimed.status_code == 201, claimed.text
    receipt = app_client.post(
        f"/api/v1/sessions/{session['session_id']}/insertions/{insertion_id}/receipt",
        json={"status": "inserted", "observable": True, "error_code": None},
        headers=headers,
    )
    assert receipt.status_code == 200, receipt.text
    return headers, session, result, insertion_id


def submit(
    app_client,
    headers,
    session,
    result,
    before,
    after,
    correction_id=None,
    left=None,
    right=None,
    extra=None,
):
    text = result["refined_text"]
    offset = text.index(before)
    if left is None:
        left = text[max(0, offset - 4) : offset]
    if right is None:
        right = text[offset + len(before) : offset + len(before) + 4]
    body = {
        "session_id": session["session_id"],
        "insertion_id": session["insertion_id"],
        "correction_id": correction_id or key(),
        "offset": offset,
        "before": before,
        "after": after,
        "left": left,
        "right": right,
    }
    if extra:
        body.update(extra)
    return app_client.post("/api/v1/corrections", json=body, headers=headers)


def attach_insertion(session, insertion_id):
    session["insertion_id"] = insertion_id


def list_dict(client, headers):
    return client.get("/api/v1/dictionary/terms", headers=headers).json()


def test_tc_learn_01_auto_learns_once_with_guard(app_client, monkeypatch):
    set_classifier(app_client, monkeypatch, decision="term", ambiguous=False)
    headers, session, result, insertion_id = prepare_inserted(
        app_client, "请使用 SQL lite 数据库", "learn-01"
    )
    attach_insertion(session, insertion_id)
    response = submit(
        app_client, headers, session, result, "SQL lite", "SQLite"
    )
    assert response.status_code == 202, response.text
    event = wait_event(app_client, headers, response.json()["event_id"])
    assert event["status"] == "learned", event
    assert event["undo_available"] is True
    assert event["term"]["term"] == "SQLite"
    assert "SQL lite" in event["term"]["aliases"]
    assert event["term"]["source"] == "auto"
    terms = list_dict(app_client, headers)
    assert [item["term"] for item in terms["items"]] == ["SQLite"]
    replay = submit(
        app_client,
        headers,
        session,
        result,
        "SQL lite",
        "SQLite",
        correction_id=response.json()["correction_id"],
    )
    assert replay.status_code == 200
    assert replay.json()["event_id"] == event["event_id"]
    assert len(list_dict(app_client, headers)["items"]) == 1


def test_tc_learn_02_guard_and_classifier_rejections(app_client, monkeypatch):
    headers, session, result, insertion_id = prepare_inserted(
        app_client, "今天把这个按钮改成一，不要修改数据库。", "learn-02"
    )
    attach_insertion(session, insertion_id)
    set_classifier(app_client, monkeypatch, decision="ignore")
    cases = [
        ("一", "二"),
        ("按钮", "按钮。"),
        ("今天", "明天"),
        ("不要修改", "要修改"),
    ]
    for before, after in cases:
        response = submit(app_client, headers, session, result, before, after)
        assert response.status_code == 202, (before, after, response.text)
        body = response.json()
        assert body["status"] == "ignored", (before, after, body)
    too_long = submit(app_client, headers, session, result, "按钮", "x" * 33)
    assert too_long.status_code == 422
    empty = submit(app_client, headers, session, result, "按钮", "")
    assert empty.status_code == 422
    # A rewrite that passes the deterministic guard but the classifier rejects.
    rewrite = submit(app_client, headers, session, result, "按钮", "按键控件")
    assert rewrite.status_code == 202
    event = wait_event(app_client, headers, rewrite.json()["event_id"])
    assert event["status"] == "ignored"
    assert list_dict(app_client, headers)["items"] == []
    assert list_dict(app_client, headers)["dictionary_version"] == 0


def test_tc_learn_04_suggested_requires_confirm_and_replays(app_client, monkeypatch):
    set_classifier(app_client, monkeypatch, decision="suggest", ambiguous=True)
    headers, session, result, insertion_id = prepare_inserted(
        app_client, "请用 cloud 保存", "learn-04"
    )
    attach_insertion(session, insertion_id)
    response = submit(app_client, headers, session, result, "cloud", "Claude")
    event = wait_event(app_client, headers, response.json()["event_id"])
    assert event["status"] == "suggested"
    assert event["ambiguous"] is True
    assert list_dict(app_client, headers)["items"] == []
    confirm_key = key()
    confirmed = app_client.post(
        f"/api/v1/learning-events/{event['event_id']}/confirm",
        headers={**headers, "Idempotency-Key": confirm_key},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "learned"
    version = list_dict(app_client, headers)["dictionary_version"]
    replay = app_client.post(
        f"/api/v1/learning-events/{event['event_id']}/confirm",
        headers={**headers, "Idempotency-Key": confirm_key},
    )
    assert replay.status_code == 200
    assert replay.json()["event_id"] == event["event_id"]
    assert list_dict(app_client, headers)["dictionary_version"] == version
    count = fetch_one("select count(*) from learning_events")
    assert count == 1


def test_tc_learn_08_ineligible_insertions(app_client, monkeypatch):
    set_classifier(app_client, monkeypatch, decision="term")
    headers, session, result, insertion_id = prepare_inserted(
        app_client, "请用 cloud 保存", "learn-08"
    )
    unknown = app_client.post(
        "/api/v1/corrections",
        json={
            "session_id": session["session_id"],
            "insertion_id": str(uuid.uuid4()),
            "correction_id": key(),
            "offset": 0,
            "before": "请",
            "after": "请用",
        },
        headers=headers,
    )
    assert unknown.status_code == 404
    reserved_session = create_session(app_client, headers, "learn08-reserved").json()
    reserved_events = dictation(app_client, headers, reserved_session["session_id"])
    reserved_result = reserved_events[-1]["result"]
    reserved_insertion = key()
    claim = app_client.post(
        f"/api/v1/sessions/{reserved_session['session_id']}/insertions/claim",
        json={
            "result_id": reserved_result["result_id"],
            "insertion_id": reserved_insertion,
            "text_hash": reserved_result["text_hash"],
            "review_confirmed": True,
        },
        headers=headers,
    )
    assert claim.status_code == 201
    reserved = app_client.post(
        "/api/v1/corrections",
        json={
            "session_id": reserved_session["session_id"],
            "insertion_id": reserved_insertion,
            "correction_id": key(),
            "offset": reserved_result["refined_text"].index("cloud"),
            "before": "cloud",
            "after": "Claude",
        },
        headers=headers,
    )
    assert reserved.status_code == 409
    assert reserved.json()["error"]["code"] == "LEARNING_NOT_ALLOWED"
    assert list_dict(app_client, headers)["items"] == []


def test_tc_learn_09_code_point_offsets_and_context_limits(app_client, monkeypatch):
    set_classifier(app_client, monkeypatch, decision="suggest")
    text = "😀" * 5 + "a" * 32 + "按钮" + "b" * 32
    headers, session, result, insertion_id = prepare_inserted(app_client, text, "learn-09")
    attach_insertion(session, insertion_id)
    offset = text.index("a" * 32)
    left = text[:offset][-80:]
    ok = submit(
        app_client,
        headers,
        session,
        result,
        "a" * 32,
        "c" * 32,
        left=left,
        right="按钮",
    )
    assert ok.status_code == 202, ok.text
    event = wait_event(app_client, headers, ok.json()["event_id"])
    stored = fetch_one(
        "select left_context from learning_events where id=$1",
        event["event_id"],
    )
    assert len(stored) <= 80
    too_long = app_client.post(
        "/api/v1/corrections",
        json={
            "session_id": session["session_id"],
            "insertion_id": insertion_id,
            "correction_id": key(),
            "offset": text.index("b" * 32),
            "before": "b" * 33,
            "after": "d" * 33,
        },
        headers=headers,
    )
    assert too_long.status_code == 422
    too_wide = submit(
        app_client,
        headers,
        session,
        result,
        "按钮",
        "按键",
        left="x" * 81,
    )
    assert too_wide.status_code == 422


def test_tc_learn_11_correction_idempotency(app_client, monkeypatch):
    set_classifier(app_client, monkeypatch, decision="suggest", delay=0.2)
    headers, session, result, insertion_id = prepare_inserted(
        app_client, "请用 cloud 保存", "learn-11"
    )
    attach_insertion(session, insertion_id)
    correction_id = key()
    first = submit(
        app_client,
        headers,
        session,
        result,
        "cloud",
        "Claude",
        correction_id=correction_id,
    )
    assert first.status_code == 202
    assert first.json()["status"] == "pending"
    event = wait_event(app_client, headers, first.json()["event_id"])
    replay = submit(
        app_client,
        headers,
        session,
        result,
        "cloud",
        "Claude",
        correction_id=correction_id,
    )
    assert replay.status_code == 200
    assert replay.json()["event_id"] == event["event_id"]
    conflict = submit(
        app_client,
        headers,
        session,
        result,
        "cloud",
        "Claudia",
        correction_id=correction_id,
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    extra = app_client.post(
        f"/api/v1/learning-events/{event['event_id']}/confirm",
        json={"force": True},
        headers={**headers, "Idempotency-Key": key()},
    )
    assert extra.status_code == 422
    count = fetch_one("select count(*) from learning_events")
    assert count == 1


def _start_delayed_correction(app_client, monkeypatch, request_id, decision="term"):
    set_classifier(app_client, monkeypatch, decision=decision, delay=0.6)
    headers, session, result, insertion_id = prepare_inserted(
        app_client, "请用 cloud 保存", request_id
    )
    attach_insertion(session, insertion_id)
    response = submit(app_client, headers, session, result, "cloud", "Claude")
    assert response.status_code == 202, response.text
    return headers, session, result, response.json()["event_id"]


def test_tc_learn_12_learning_disabled_during_classification(app_client, monkeypatch):
    headers, session, result, event_id = _start_delayed_correction(
        app_client, monkeypatch, "learn12a"
    )
    time.sleep(0.1)
    settings = app_client.get("/api/v1/settings", headers=headers).json()
    disabled = app_client.patch(
        "/api/v1/settings",
        json={"version": settings["version"], "learning_enabled": False},
        headers=headers,
    )
    assert disabled.status_code == 200, disabled.text
    event = wait_event(app_client, headers, event_id)
    assert event["status"] == "failed"
    assert event["reason"] == "LEARNING_DISABLED"
    assert list_dict(app_client, headers)["items"] == []


def test_tc_learn_12_content_expired_during_classification(app_client, monkeypatch):
    headers, session, result, event_id = _start_delayed_correction(
        app_client, monkeypatch, "learn12b"
    )
    time.sleep(0.1)
    execute_sql(
        "update results set content_cleared_at = now() - interval '1 second' "
        "where id=$1",
        result["result_id"],
    )
    event = wait_event(app_client, headers, event_id)
    assert event["status"] == "failed"
    assert event["reason"] == "CONTENT_EXPIRED"
    assert list_dict(app_client, headers)["items"] == []


def test_tc_learn_12_manual_edit_during_classification(app_client, monkeypatch):
    headers, session, result, insertion_id = prepare_inserted(
        app_client, "请用 cloud 保存", "learn12c"
    )
    attach_insertion(session, insertion_id)
    created = app_client.post(
        "/api/v1/dictionary/terms",
        json={"term": "Claude", "aliases": [], "scope": "default"},
        headers={**headers, "Idempotency-Key": key()},
    )
    assert created.status_code == 201, created.text
    term = created.json()
    set_classifier(app_client, monkeypatch, decision="term", delay=0.6)
    response = submit(app_client, headers, session, result, "cloud", "Claude")
    assert response.status_code == 202
    time.sleep(0.1)
    edited = app_client.patch(
        f"/api/v1/dictionary/terms/{term['id']}",
        json={"version": 1, "aliases": ["cloud"]},
        headers={**headers, "Idempotency-Key": key()},
    )
    assert edited.status_code == 200, edited.text
    event = wait_event(app_client, headers, response.json()["event_id"])
    assert event["status"] == "failed"
    assert event["reason"] == "VERSION_CONFLICT"
    assert list_dict(app_client, headers)["items"][0]["version"] == 2


def test_tc_learn_12_alias_conflict_during_classification(app_client, monkeypatch):
    headers, session, result, insertion_id = prepare_inserted(
        app_client, "请用 cloud 保存", "learn12d"
    )
    attach_insertion(session, insertion_id)
    set_classifier(app_client, monkeypatch, decision="term", delay=0.6)
    response = submit(app_client, headers, session, result, "cloud", "Claude")
    assert response.status_code == 202
    time.sleep(0.1)
    conflict = app_client.post(
        "/api/v1/dictionary/terms",
        json={"term": "Other", "aliases": ["cloud"], "scope": "default"},
        headers={**headers, "Idempotency-Key": key()},
    )
    assert conflict.status_code == 201
    event = wait_event(app_client, headers, response.json()["event_id"])
    assert event["status"] == "failed"
    assert event["reason"] == "ALIAS_CONFLICT"
    terms = {item["term"] for item in list_dict(app_client, headers)["items"]}
    assert terms == {"Other"}


def test_tc_learn_12_dictionary_limit_during_classification(app_client, monkeypatch):
    headers, session, result, event_id = _start_delayed_correction(
        app_client, monkeypatch, "learn12e"
    )
    time.sleep(0.1)
    user_id = fetch_one("select id from users where email=$1", "learn12e@example.com")
    execute_sql(
        """
        insert into dictionary_terms
          (id, user_id, term, norm, aliases, contexts, scope, source, version,
           created_at, updated_at)
        select 'l' || lpad(g::text, 7, '0') || '-0000-4000-8000-' ||
               lpad(g::text, 10, '0'),
               $1, 'Fill' || g, lower('Fill' || g), '[]'::jsonb, '[]'::jsonb,
               'default', 'manual', 1, now(), now()
        from generate_series(1, 5000) g
        """,
        user_id,
    )
    event = wait_event(app_client, headers, event_id, timeout=20)
    assert event["status"] == "failed"
    assert event["reason"] == "DICTIONARY_LIMIT"
    assert fetch_one("select count(*) from dictionary_terms") == 5000


def test_tc_learn_13_evidence_ambiguity_and_guard(app_client, monkeypatch):
    headers, session, result, insertion_id = prepare_inserted(
        app_client, "请用 cloud 保存，把按钮改成一。", "learn-13"
    )
    attach_insertion(session, insertion_id)
    set_classifier(app_client, monkeypatch, decision="term", evidence="mismatch")
    mismatched = submit(app_client, headers, session, result, "cloud", "Claude")
    event = wait_event(app_client, headers, mismatched.json()["event_id"])
    assert event["status"] == "failed"
    assert event["reason"] == "EVIDENCE_MISMATCH"
    assert list_dict(app_client, headers)["items"] == []

    set_classifier(app_client, monkeypatch, decision="term", ambiguous=True)
    ambiguous = submit(app_client, headers, session, result, "cloud", "Claude")
    event = wait_event(app_client, headers, ambiguous.json()["event_id"])
    assert event["status"] == "suggested"
    assert event["ambiguous"] is True
    assert list_dict(app_client, headers)["items"] == []

    set_classifier(app_client, monkeypatch, decision="term")
    number = submit(app_client, headers, session, result, "一", "二")
    assert number.status_code == 202
    assert number.json()["status"] == "ignored"
    assert list_dict(app_client, headers)["items"] == []


def test_tc_learn_15_contexts_capped_at_three(app_client, monkeypatch):
    set_classifier(app_client, monkeypatch, decision="term")
    tokens = new_user(app_client, "learn15@example.com", "learn-15-d1")
    headers = auth_headers(tokens)
    corrections = [
        ("第一段 cloud 服务", "cloud"),
        ("第二段 claud 服务", "claud"),
        ("第三段 cloude 服务", "cloude"),
        ("第四段 clowd 服务", "clowd"),
    ]
    # A learned alias is corrected before insertion. Each new context therefore
    # needs a different, not-yet-learned alias for the same canonical term.
    for index, (text, before) in enumerate(corrections):
        set_text(app_client, text)
        session = create_session(app_client, headers, f"learn15-s{index}").json()
        events = dictation(app_client, headers, session["session_id"])
        result = events[-1]["result"]
        insertion_id = key()
        claim = app_client.post(
            f"/api/v1/sessions/{session['session_id']}/insertions/claim",
            json={
                "result_id": result["result_id"],
                "insertion_id": insertion_id,
                "text_hash": result["text_hash"],
                "review_confirmed": True,
            },
            headers=headers,
        )
        assert claim.status_code == 201, claim.text
        assert app_client.post(
            f"/api/v1/sessions/{session['session_id']}/insertions/{insertion_id}/receipt",
            json={"status": "inserted", "observable": True, "error_code": None},
            headers=headers,
        ).status_code == 200
        session = {**session, "insertion_id": insertion_id}
        offset = text.index(before)
        response = submit(
            app_client,
            headers,
            session,
            result,
            before,
            "Claude",
            left=text[max(0, offset - 80) : offset],
            right=text[offset + len(before) : offset + len(before) + 80],
        )
        assert response.status_code == 202, response.text
        event = wait_event(app_client, headers, response.json()["event_id"])
        assert event["status"] == "learned", (index, event)
    final = list_dict(app_client, headers)["items"]
    assert [item["term"] for item in final] == ["Claude"]
    contexts = final[0]["contexts"]
    assert len(contexts) == 3
    assert all(len(item["left"]) <= 80 and len(item["right"]) <= 80 for item in contexts)
    assert len({item["left"] for item in contexts}) == 3
