from __future__ import annotations

from tests.ids import fixture_uuid

import time
import uuid

from app.providers.asr.base import ProviderError
from app.services.cleanup import cleanup_history, cleanup_retry_audio
from tests.conftest import auth_headers, execute_sql
from tests.cases.test_dict_cases import fetch_one, key, new_user
from tests.cases.test_learn_cases import (
    set_classifier,
    set_text,
    submit,
    wait_event,
)
from tests.test_sessions import create_session, dictation, frame

PASSWORD = "Password123"


def login(client, email, installation="hist-device"):
    return client.post(
        "/api/auth/login",
        json={
            "email": email,
            "password": PASSWORD,
            "client_type": "desktop",
            "device": {
                "installation_id": fixture_uuid(installation),
                "name": "hist-mac",
                "platform": "macos",
                "client_version": "0.0.1",
            },
        },
    )


def run_session(app_client, headers, request_id, text="请帮我加一个按钮"):
    set_text(app_client, text)
    session = create_session(app_client, headers, request_id).json()
    events = dictation(app_client, headers, session["session_id"])
    return session, events[-1]["result"]


def empty_session(app_client, headers, request_id):
    session = create_session(app_client, headers, request_id).json()
    with app_client.websocket_connect(
        f"/ws/v1/sessions/{session['session_id']}/audio", headers=headers
    ) as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "audio.end", "last_seq": -1, "total_samples": 0})
        while True:
            event = ws.receive_json()
            if event["type"] in ("session.empty", "error", "cancelled"):
                break
    return session


def cancel_session(app_client, headers, request_id):
    session = create_session(app_client, headers, request_id).json()
    response = app_client.post(
        f"/api/v1/sessions/{session['session_id']}/cancel", headers=headers
    )
    assert response.status_code == 200, response.text
    return session


def fail_first_asr(app_client, monkeypatch):
    provider = app_client.app.state.sessions.asr_provider
    original = provider.open

    async def failing_open(options):
        session = await original(options)
        if options.attempt_no == 1:
            async def fail(total_samples, deadline):
                raise ProviderError("ASR_FAILED", "injected ASR failure")

            session.finish = fail
        return session

    monkeypatch.setattr(provider, "open", failing_open)
    return provider


def wait_state(app_client, headers, session_id, states, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = app_client.get(
            f"/api/v1/sessions/{session_id}", headers=headers
        ).json()
        if body["state"] in states:
            return body
        time.sleep(0.05)
    raise AssertionError(f"session stayed in {body['state']}")


def retain_audio_owners(app_client, monkeypatch):
    """Keep original objects so retiring a cache key cannot conceal retained PCM."""
    manager = app_client.app.state.sessions
    original_create = manager.create
    owners = {}

    async def capture(*args, **kwargs):
        session, created = await original_create(*args, **kwargs)
        if created:
            runtime = manager.runtimes[session.id]
            provider = runtime.asr
            assert provider is not None
            owner = {
                "runtime": runtime,
                "audio": runtime.audio,
                "cancel_complete": False,
            }
            original_cancel = provider.cancel

            async def cancelled():
                await original_cancel()
                owner["cancel_complete"] = True

            monkeypatch.setattr(provider, "cancel", cancelled)
            owners[session.id] = owner
        return session, created

    monkeypatch.setattr(manager, "create", capture)
    return owners


def assert_audio_released(app_client, owner):
    runtime = owner["runtime"]
    assert owner["audio"] == b"", "original PCM bytearray was not cleared"
    assert runtime.audio == b"", "runtime still owns PCM"
    assert runtime.asr is None, "provider still attached after release"
    assert owner["cancel_complete"], "provider cancellation did not complete"
    assert runtime.retry_expires_at is None
    # Retirement or an empty runtime are both valid; a removed key alone is not proof.
    live = app_client.app.state.sessions.runtimes.get(runtime.session_id)
    if live is not None:
        assert live.audio == b""
        assert live.asr is None
        assert live.retry_expires_at is None


def history(client, headers, **params):
    return client.get("/api/v1/history", params=params, headers=headers)


def test_tc_hist_01_states_have_honest_content(app_client):
    tokens = new_user(app_client, "hist01@example.com", "hist01-d1")
    headers = auth_headers(tokens)
    ready, result = run_session(app_client, headers, "hist01-ready")
    empty = empty_session(app_client, headers, "hist01-empty")
    cancelled = cancel_session(app_client, headers, "hist01-cancelled")
    items = history(app_client, headers).json()["items"]
    by_id = {item["session_id"]: item for item in items}
    assert by_id[ready["session_id"]]["state"] == "ready"
    assert by_id[ready["session_id"]]["result"]["refined_text"] == result["refined_text"]
    assert by_id[empty["session_id"]]["state"] == "empty"
    assert by_id[empty["session_id"]]["result"] is None
    assert by_id[cancelled["session_id"]]["state"] == "cancelled"
    assert by_id[cancelled["session_id"]]["result"] is None
    for item in items:
        assert item["created_at"]
        assert item["mode"] == "dictation"
        assert isinstance(item["audio_ms"], int)


def test_tc_hist_02_all_and_dictation_are_same_dataset(app_client):
    tokens = new_user(app_client, "hist02@example.com", "hist02-d1")
    headers = auth_headers(tokens)
    run_session(app_client, headers, "hist02-one")
    run_session(app_client, headers, "hist02-two")
    all_items = history(app_client, headers, type="all").json()["items"]
    dictation_items = history(app_client, headers, type="dictation").json()["items"]
    assert [item["session_id"] for item in all_items] == [
        item["session_id"] for item in dictation_items
    ]
    invalid = history(app_client, headers, type="translation")
    assert invalid.status_code == 422


def test_tc_hist_03_asr_retry_reuses_session(app_client, monkeypatch):
    tokens = new_user(app_client, "hist03@example.com", "hist03-d1")
    headers = auth_headers(tokens)
    fail_first_asr(app_client, monkeypatch)
    set_text(app_client, "请帮我加一个按钮")
    session = create_session(app_client, headers, "hist03-session").json()
    events = dictation(app_client, headers, session["session_id"])
    assert events[-1]["type"] == "error"
    failed = wait_state(app_client, headers, session["session_id"], ("failed",))
    assert failed["error_code"] == "ASR_FAILED"
    assert failed["retry_kind"] == "asr"

    retry_key = key()
    first = app_client.post(
        f"/api/v1/sessions/{session['session_id']}/retry",
        json={"retry_kind": "asr"},
        headers={**headers, "Idempotency-Key": retry_key},
    )
    assert first.status_code == 202, first.text
    replay = app_client.post(
        f"/api/v1/sessions/{session['session_id']}/retry",
        json={"retry_kind": "asr"},
        headers={**headers, "Idempotency-Key": retry_key},
    )
    assert replay.status_code == 200, replay.text
    ready = wait_state(app_client, headers, session["session_id"], ("ready", "failed"))
    assert ready["state"] == "ready", ready
    assert ready["current_attempt_no"] == 2
    assert ready["result"]["attempt_no"] == 2
    assert ready["result"]["attempt_kind"] == "asr"
    assert ready["result"]["result_id"]
    items = history(app_client, headers).json()["items"]
    assert len(items) == 1


def test_tc_hist_04_raw_and_refined_are_separate(app_client):
    tokens = new_user(app_client, "hist04@example.com", "hist04-d1")
    headers = auth_headers(tokens)
    session, result = run_session(app_client, headers, "hist04-session")
    detail = app_client.get(
        f"/api/v1/sessions/{session['session_id']}", headers=headers
    ).json()
    assert detail["result"]["raw_text"] == result["raw_text"]
    assert detail["result"]["refined_text"] == result["refined_text"]
    assert isinstance(detail["result"]["raw_text"], str)
    assert isinstance(detail["result"]["refined_text"], str)


def test_tc_hist_05_retention_boundary_and_cleanup(app_client, monkeypatch):
    tokens = new_user(app_client, "hist05@example.com", "hist05-d1")
    headers = auth_headers(tokens)
    set_classifier(app_client, monkeypatch, decision="term")
    session, result = run_session(app_client, headers, "hist05-session")
    create = app_client.post(
        "/api/v1/dictionary/terms",
        json={"term": "Claude", "aliases": [], "scope": "default"},
        headers={**headers, "Idempotency-Key": key()},
    )
    assert create.status_code == 201
    settings = app_client.get("/api/v1/settings", headers=headers).json()
    updated = app_client.patch(
        "/api/v1/settings",
        json={"version": settings["version"], "history_retention_days": 7},
        headers=headers,
    )
    assert updated.status_code == 200, updated.text
    execute_sql(
        "update sessions set first_terminal_at = now() - interval '8 days' "
        "where id=$1",
        session["session_id"],
    )
    detail = app_client.get(
        f"/api/v1/sessions/{session['session_id']}", headers=headers
    ).json()
    assert detail["content_expired"] is True
    assert detail["result"] is None
    assert history(app_client, headers).json()["items"] == []
    claim = app_client.post(
        f"/api/v1/sessions/{session['session_id']}/insertions/claim",
        json={
            "result_id": result["result_id"],
            "insertion_id": key(),
            "text_hash": result["text_hash"],
            "review_confirmed": False,
        },
        headers=headers,
    )
    assert claim.status_code == 410
    retry = app_client.post(
        f"/api/v1/sessions/{session['session_id']}/retry",
        json={"retry_kind": "refine"},
        headers={**headers, "Idempotency-Key": key()},
    )
    assert retry.status_code == 410
    app_client.portal.call(cleanup_history, app_client.app)
    cleared = fetch_one(
        "select content_cleared_at from sessions where id=$1", session["session_id"]
    )
    assert cleared is not None
    assert fetch_one(
        "select raw_text from results where id=$1", result["result_id"]
    ) is None
    assert fetch_one("select count(*) from dictionary_terms") == 1
    import datetime as dt
    from zoneinfo import ZoneInfo

    today = dt.datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    stats = app_client.get(
        "/api/v1/stats/me",
        params={"date_from": today, "date_to": today},
        headers=headers,
    ).json()
    assert stats["totals"]["sessions"] >= 1


def test_tc_hist_07_cross_tenant_history_isolation(app_client):
    u1 = new_user(app_client, "hist07-u1@example.com", "hist07-d1")
    u2 = new_user(app_client, "hist07-u2@example.com", "hist07-d2")
    h1 = auth_headers(u1)
    h2 = auth_headers(u2)
    s1, _ = run_session(app_client, h1, "hist07-one")
    s2, _ = run_session(app_client, h2, "hist07-two")
    foreign = app_client.get(
        f"/api/v1/sessions/{s2['session_id']}", headers=h1
    )
    assert foreign.status_code == 404
    assert foreign.json()["error"]["code"] == "NOT_FOUND"
    page2 = history(app_client, h2, limit=1).json()
    forged = history(app_client, h1, limit=1, cursor=page2["next_cursor"] or "bad")
    assert forged.status_code in (400, 422), forged.text
    assert [item["session_id"] for item in history(app_client, h1).json()["items"]] == [
        s1["session_id"]
    ]


def test_tc_hist_08_frozen_pagination_and_stable_order(app_client):
    tokens = new_user(app_client, "hist08@example.com", "hist08-d1")
    headers = auth_headers(tokens)
    device_id = fetch_one("select id from devices where user_id=$1", u1_user_id(tokens))
    sid = fetch_one(
        "select id from login_sessions where user_id=$1 order by created_at desc limit 1",
        u1_user_id(tokens),
    )
    assert device_id and sid
    execute_sql(
        """
        insert into sessions
          (id, user_id, device_id, created_login_sid, client_request_id,
           request_hash, mode, state, current_attempt_no, dictionary_version,
           learning_enabled, received_samples, audio_complete, created_at,
           first_terminal_at, retry_cache_status)
        select lpad(g::text, 8, '0') || '-0000-4000-8000-' ||
               lpad(g::text, 12, '0'),
               $1, $2, $3, md5('hist08-' || g)::uuid::text, decode(repeat('00', 32), 'hex'),
               'dictation', 'ready', 1, 0, true, 8000, true,
               timestamptz '2026-10-01 12:00:00+00',
               timestamptz '2026-10-01 12:00:01+00', 'none'
        from generate_series(1, 53) g
        """,
        u1_user_id(tokens),
        device_id,
        sid,
    )
    page1 = history(app_client, headers, limit=20).json()
    page2 = history(
        app_client, headers, limit=20, cursor=page1["next_cursor"]
    ).json()
    page3 = history(
        app_client, headers, limit=20, cursor=page2["next_cursor"]
    ).json()
    ids = [
        item["session_id"]
        for page in (page1, page2, page3)
        for item in page["items"]
    ]
    assert len(ids) == 53
    assert len(set(ids)) == 53
    # Same created_at is ordered stably by descending id.
    assert page1["items"][0]["session_id"] > page1["items"][1]["session_id"]
    execute_sql(
        """
        insert into sessions
          (id, user_id, device_id, created_login_sid, client_request_id,
           request_hash, mode, state, current_attempt_no, dictionary_version,
           learning_enabled, received_samples, audio_complete, created_at,
           first_terminal_at, retry_cache_status)
        values ('ffffffff-0000-4000-8000-000000000001', $1, $2, $3,
                md5('hist08-new')::uuid::text, decode(repeat('00', 32), 'hex'), 'dictation',
                'ready', 1, 0, true, 8000, true,
                timestamptz '2026-10-02 12:00:00+00',
                timestamptz '2026-10-02 12:00:01+00', 'none')
        """,
        u1_user_id(tokens),
        device_id,
        sid,
    )
    refreshed = history(app_client, headers, limit=20).json()
    assert refreshed["items"][0]["session_id"] == "ffffffff-0000-4000-8000-000000000001"
    continued = history(
        app_client, headers, limit=20, cursor=page1["next_cursor"]
    ).json()
    assert all(
        item["session_id"] != "ffffffff-0000-4000-8000-000000000001"
        for item in continued["items"]
    )


def u1_user_id(tokens):
    import jwt

    return jwt.decode(
        tokens["access_token"], options={"verify_signature": False}
    )["sub"]


def test_tc_hist_10_refine_retry_does_not_call_asr(app_client, monkeypatch):
    tokens = new_user(app_client, "hist10@example.com", "hist10-d1")
    headers = auth_headers(tokens)
    owners = retain_audio_owners(app_client, monkeypatch)
    session, result = run_session(app_client, headers, "hist10-session")
    execute_sql(
        "update results set review_required=true where id=$1", result["result_id"]
    )
    assert_audio_released(app_client, owners[session["session_id"]])
    provider = app_client.app.state.sessions.asr_provider
    calls = []
    original = provider.open

    async def counting_open(options):
        calls.append(options.attempt_no)
        return await original(options)

    monkeypatch.setattr(provider, "open", counting_open)
    response = app_client.post(
        f"/api/v1/sessions/{session['session_id']}/retry",
        json={"retry_kind": "refine"},
        headers={**headers, "Idempotency-Key": key()},
    )
    assert response.status_code == 202, response.text
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        body = app_client.get(
            f"/api/v1/sessions/{session['session_id']}", headers=headers
        ).json()
        if (
            body["state"] == "ready"
            and body["result"]
            and body["result"]["result_id"] != result["result_id"]
        ):
            break
        time.sleep(0.05)
    assert body["result"]["attempt_no"] == 2
    assert body["result"]["attempt_kind"] == "refine"
    assert body["result"]["raw_text"] == result["raw_text"]
    assert calls == []
    assert body["result"]["stop_to_result_ms"] >= 0
    assert_audio_released(app_client, owners[session["session_id"]])


def test_tc_hist_11_retry_conditions(app_client):
    tokens = new_user(app_client, "hist11@example.com", "hist11-d1")
    headers = auth_headers(tokens)
    session, result = run_session(app_client, headers, "hist11-base")

    cancelled = cancel_session(app_client, headers, "hist11-cancelled")
    cancelled_retry = app_client.post(
        f"/api/v1/sessions/{cancelled['session_id']}/retry",
        json={"retry_kind": "asr"},
        headers={**headers, "Idempotency-Key": key()},
    )
    assert cancelled_retry.status_code == 409
    assert cancelled_retry.json()["error"]["details"]["reason"] == "cancelled"

    empty = empty_session(app_client, headers, "hist11-empty")
    empty_retry = app_client.post(
        f"/api/v1/sessions/{empty['session_id']}/retry",
        json={"retry_kind": "asr"},
        headers={**headers, "Idempotency-Key": key()},
    )
    assert empty_retry.status_code == 409
    assert empty_retry.json()["error"]["details"]["reason"] == "empty"

    insertion_id = key()
    claim = app_client.post(
        f"/api/v1/sessions/{session['session_id']}/insertions/claim",
        json={
            "result_id": result["result_id"],
            "insertion_id": insertion_id,
            "text_hash": result["text_hash"],
            "review_confirmed": False,
        },
        headers=headers,
    )
    assert claim.status_code == 201
    reserved = app_client.post(
        f"/api/v1/sessions/{session['session_id']}/retry",
        json={"retry_kind": "refine"},
        headers={**headers, "Idempotency-Key": key()},
    )
    assert reserved.status_code == 409
    assert reserved.json()["error"]["details"]["reason"] == "insertion_reserved"
    receipt = app_client.post(
        f"/api/v1/sessions/{session['session_id']}/insertions/{insertion_id}/receipt",
        json={"status": "inserted", "observable": True, "error_code": None},
        headers=headers,
    )
    assert receipt.status_code == 200
    inserted = app_client.post(
        f"/api/v1/sessions/{session['session_id']}/retry",
        json={"retry_kind": "refine"},
        headers={**headers, "Idempotency-Key": key()},
    )
    assert inserted.status_code == 409
    assert inserted.json()["error"]["details"]["reason"] == "already_inserted"

    second_session, second_result = run_session(app_client, headers, "hist11-second")
    second_insertion = key()
    app_client.post(
        f"/api/v1/sessions/{second_session['session_id']}/insertions/claim",
        json={
            "result_id": second_result["result_id"],
            "insertion_id": second_insertion,
            "text_hash": second_result["text_hash"],
            "review_confirmed": False,
        },
        headers=headers,
    )
    unverified_receipt = app_client.post(
        f"/api/v1/sessions/{second_session['session_id']}/insertions/{second_insertion}/receipt",
        json={
            "status": "unverified",
            "observable": False,
            "error_code": "VERIFY_FAILED",
        },
        headers=headers,
    )
    assert unverified_receipt.status_code == 200, unverified_receipt.text
    unverified = app_client.post(
        f"/api/v1/sessions/{second_session['session_id']}/retry",
        json={"retry_kind": "refine"},
        headers={**headers, "Idempotency-Key": key()},
    )
    assert unverified.status_code == 409
    assert unverified.json()["error"]["details"]["reason"] == "insertion_unverified"

    third_session, third_result = run_session(app_client, headers, "hist11-third")
    before_calls = fetch_one("select count(*) from model_calls")
    execute_sql(
        "update sessions set content_cleared_at = now() where id=$1",
        third_session["session_id"],
    )
    expired = app_client.post(
        f"/api/v1/sessions/{third_session['session_id']}/retry",
        json={"retry_kind": "refine"},
        headers={**headers, "Idempotency-Key": key()},
    )
    assert expired.status_code == 410
    assert expired.json()["error"]["code"] == "CONTENT_EXPIRED"
    assert fetch_one("select count(*) from model_calls") == before_calls


def test_tc_hist_13_retention_profiles_and_boundaries(app_client):
    users = {}
    for name, days in (("perm", None), ("seven", 7), ("thirty", 30), ("ninety", 90)):
        tokens = new_user(
            app_client, f"hist13-{name}@example.com", f"hist13-{name}"
        )
        headers = auth_headers(tokens)
        if days is not None:
            settings = app_client.get("/api/v1/settings", headers=headers).json()
            patched = app_client.patch(
                "/api/v1/settings",
                json={
                    "version": settings["version"],
                    "history_retention_days": days,
                },
                headers=headers,
            )
            assert patched.status_code == 200, patched.text
        session, _ = run_session(app_client, headers, f"hist13-{name}-session")
        execute_sql(
            "update sessions set first_terminal_at = now() - interval '8 days' "
            "where id=$1",
            session["session_id"],
        )
        recent = None
        if name == "seven":
            recent, _ = run_session(app_client, headers, "hist13-seven-recent")
            execute_sql(
                "update sessions set first_terminal_at = now() - interval '6 days' "
                "where id=$1",
                recent["session_id"],
            )
        users[name] = (headers, session, recent)
    for name, expected in (("perm", False), ("seven", True), ("thirty", False), ("ninety", False)):
        headers, session, _ = users[name]
        detail = app_client.get(
            f"/api/v1/sessions/{session['session_id']}", headers=headers
        ).json()
        assert detail["content_expired"] is expected, (name, detail)
    seven_headers, seven_session, recent_session = users["seven"]
    detail_six = app_client.get(
        f"/api/v1/sessions/{recent_session['session_id']}", headers=seven_headers
    ).json()
    row = fetch_one(
        "select first_terminal_at::text || '|' || "
        "coalesce(content_cleared_at::text,'') from sessions where id=$1",
        recent_session["session_id"],
    )
    assert detail_six["content_expired"] is False, (
        detail_six["content_expired"],
        detail_six["content_expires_at"],
        str(row),
        app_client.get("/api/v1/settings", headers=seven_headers).json(),
    )
    perm_headers, perm_session, _ = users["perm"]
    execute_sql(
        "update sessions set first_terminal_at = now() - interval '10 days' "
        "where id=$1",
        perm_session["session_id"],
    )
    settings = app_client.get("/api/v1/settings", headers=perm_headers).json()
    assert (
        app_client.patch(
            "/api/v1/settings",
            json={"version": settings["version"], "history_retention_days": 7},
            headers=perm_headers,
        ).status_code
        == 200
    )
    assert (
        app_client.get(
            f"/api/v1/sessions/{perm_session['session_id']}", headers=perm_headers
        ).json()["content_expired"]
        is True
    )
    # Retrying refine must not move the first terminal timestamp.
    retry_session, retry_result = run_session(
        app_client, perm_headers, "hist13-retry"
    )
    execute_sql(
        "update results set review_required=true where id=$1",
        retry_result["result_id"],
    )
    original_terminal = fetch_one(
        "select first_terminal_at from sessions where id=$1",
        retry_session["session_id"],
    )
    assert (
        app_client.post(
            f"/api/v1/sessions/{retry_session['session_id']}/retry",
            json={"retry_kind": "refine"},
            headers={**perm_headers, "Idempotency-Key": key()},
        ).status_code
        == 202
    )
    wait_state(app_client, perm_headers, retry_session["session_id"], ("ready",))
    assert (
        fetch_one(
            "select first_terminal_at from sessions where id=$1",
            retry_session["session_id"],
        )
        == original_terminal
    )


def test_tc_hist_14_terms_survive_content_cleanup(app_client, monkeypatch):
    set_classifier(app_client, monkeypatch, decision="term")
    tokens = new_user(app_client, "hist14@example.com", "hist14-d1")
    headers = auth_headers(tokens)
    set_text(app_client, "请用 cloud 保存")
    session = create_session(app_client, headers, "hist14-session").json()
    events = dictation(app_client, headers, session["session_id"])
    result = events[-1]["result"]
    insertion_id = key()
    assert app_client.post(
        f"/api/v1/sessions/{session['session_id']}/insertions/claim",
        json={
            "result_id": result["result_id"],
            "insertion_id": insertion_id,
            "text_hash": result["text_hash"],
            "review_confirmed": False,
        },
        headers=headers,
    ).status_code == 201
    assert app_client.post(
        f"/api/v1/sessions/{session['session_id']}/insertions/{insertion_id}/receipt",
        json={"status": "inserted", "observable": True, "error_code": None},
        headers=headers,
    ).status_code == 200
    response = submit(
        app_client,
        headers,
        {**session, "insertion_id": insertion_id},
        result,
        "cloud",
        "Claude",
    )
    event = wait_event(app_client, headers, response.json()["event_id"])
    assert event["status"] == "learned"
    settings = app_client.get("/api/v1/settings", headers=headers).json()
    app_client.patch(
        "/api/v1/settings",
        json={"version": settings["version"], "history_retention_days": 7},
        headers=headers,
    )
    execute_sql(
        "update sessions set first_terminal_at = now() - interval '8 days' "
        "where id=$1",
        session["session_id"],
    )
    app_client.portal.call(cleanup_history, app_client.app)
    assert fetch_one(
        "select count(*) from dictionary_terms where deleted_at is null"
    ) == 1
    undo = app_client.post(
        f"/api/v1/learning-events/{event['event_id']}/undo",
        headers={**headers, "Idempotency-Key": key()},
    )
    assert undo.status_code == 410
    assert undo.json()["error"]["code"] == "CONTENT_EXPIRED"
    term = app_client.get("/api/v1/dictionary/terms", headers=headers).json()["items"][0]
    edited = app_client.patch(
        f"/api/v1/dictionary/terms/{term['id']}",
        json={"version": term["version"], "aliases": ["cloud", "claude-ai"]},
        headers={**headers, "Idempotency-Key": key()},
    )
    assert edited.status_code == 200, edited.text
    deleted = app_client.delete(
        f"/api/v1/dictionary/terms/{term['id']}",
        params={"version": edited.json()["version"]},
        headers={**headers, "Idempotency-Key": key()},
    )
    assert deleted.status_code == 200


def test_tc_hist_15_retention_change_does_not_restore_content(app_client):
    tokens = new_user(app_client, "hist15@example.com", "hist15-d1")
    headers = auth_headers(tokens)
    session, _ = run_session(app_client, headers, "hist15-session")
    settings = app_client.get("/api/v1/settings", headers=headers).json()
    version = settings["version"]
    # Cancel means no PATCH; the version stays unchanged.
    assert app_client.get("/api/v1/settings", headers=headers).json()["version"] == version
    execute_sql(
        "update sessions set first_terminal_at = now() - interval '8 days' "
        "where id=$1",
        session["session_id"],
    )
    shortened = app_client.patch(
        "/api/v1/settings",
        json={"version": version, "history_retention_days": 7},
        headers=headers,
    )
    assert shortened.status_code == 200
    assert shortened.json()["version"] == version + 1
    assert (
        app_client.get(
            f"/api/v1/sessions/{session['session_id']}", headers=headers
        ).json()["content_expired"]
        is True
    )
    assert fetch_one(
        "select content_cleared_at from sessions where id=$1",
        session["session_id"],
    ) is not None
    extended = app_client.patch(
        "/api/v1/settings",
        json={"version": version + 1, "history_retention_days": 30},
        headers=headers,
    )
    assert extended.status_code == 200
    assert (
        app_client.get(
            f"/api/v1/sessions/{session['session_id']}", headers=headers
        ).json()["content_expired"]
        is True
    )


def test_tc_hist_06_audio_lifecycle_and_storage(app_client, monkeypatch):
    tokens = new_user(app_client, "hist06@example.com", "hist06-d1")
    headers = auth_headers(tokens)
    owners = retain_audio_owners(app_client, monkeypatch)
    ready, result = run_session(app_client, headers, "hist06-ready")
    assert_audio_released(app_client, owners[ready["session_id"]])
    assert owners[ready["session_id"]]["runtime"].samples == 8000
    detail = app_client.get(f"/api/v1/sessions/{ready['session_id']}", headers=headers).json()
    assert detail["result"]["raw_text"] == result["raw_text"]
    assert detail["received_samples"] == 8000

    # Empty/cancel/incomplete branches must release real received bytes, not only
    # pass because their buffers were never populated.
    set_text(app_client, "")
    empty = create_session(app_client, headers, "hist06-empty").json()
    assert dictation(app_client, headers, empty["session_id"])[-1]["type"] == "session.empty"
    assert_audio_released(app_client, owners[empty["session_id"]])
    assert owners[empty["session_id"]]["runtime"].samples == 8000

    cancelled = create_session(app_client, headers, "hist06-cancelled").json()
    with app_client.websocket_connect(
        f"/ws/v1/sessions/{cancelled['session_id']}/audio", headers=headers
    ) as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_bytes(frame(0))
        assert ws.receive_json()["type"] == "audio.ack"
        assert len(owners[cancelled["session_id"]]["audio"]) == 8000
        response = app_client.post(f"/api/v1/sessions/{cancelled['session_id']}/cancel", headers=headers)
        assert response.status_code == 200
        assert response.json()["state"] == "cancelled"
    assert_audio_released(app_client, owners[cancelled["session_id"]])

    incomplete = create_session(app_client, headers, "hist06-incomplete").json()
    with app_client.websocket_connect(
        f"/ws/v1/sessions/{incomplete['session_id']}/audio", headers=headers
    ) as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_bytes(frame(0))
        assert ws.receive_json()["type"] == "audio.ack"
        assert len(owners[incomplete["session_id"]]["audio"]) == 8000
        # Deliberately disconnect before audio.end.
    interrupted = wait_state(app_client, headers, incomplete["session_id"], ("failed",))
    assert interrupted["audio_complete"] is False
    assert interrupted["retry_kind"] == "none"
    assert interrupted["retry_unavailable_reason"] == "incomplete_audio"
    assert_audio_released(app_client, owners[incomplete["session_id"]])
    fail_first_asr(app_client, monkeypatch)
    set_text(app_client, "请帮我加一个按钮")
    failed_session = create_session(app_client, headers, "hist06-failed").json()
    dictation(app_client, headers, failed_session["session_id"])
    failed = wait_state(app_client, headers, failed_session["session_id"], ("failed",))
    runtime = app_client.app.state.sessions.runtimes[failed_session["session_id"]]
    assert len(runtime.audio) == 16000
    assert runtime.audio is owners[failed_session["session_id"]]["audio"]
    assert runtime.retry_expires_at is not None
    assert 0 < runtime.retry_expires_at - time.time() <= 600
    columns = fetch_one(
        "select count(*) from information_schema.columns "
        "where table_name in ('sessions','results','session_attempts') "
        "and data_type='bytea' and column_name not in ('request_hash')"
    )
    assert columns == 0
    assert failed["state"] == "failed"
    assert failed["audio_complete"] is True
    assert failed["retry_kind"] == "asr"


def test_tc_hist_09_audio_cache_expiry(app_client, monkeypatch):
    tokens = new_user(app_client, "hist09@example.com", "hist09-d1")
    headers = auth_headers(tokens)
    fail_first_asr(app_client, monkeypatch)
    set_text(app_client, "请帮我加一个按钮")
    session = create_session(app_client, headers, "hist09-session").json()
    dictation(app_client, headers, session["session_id"])
    wait_state(app_client, headers, session["session_id"], ("failed",))
    runtime = app_client.app.state.sessions.runtimes[session["session_id"]]
    runtime.retry_expires_at = time.time() - 1
    app_client.portal.call(cleanup_retry_audio, app_client.app)
    assert runtime.audio == b""
    no_cache = app_client.get(
        f"/api/v1/sessions/{session['session_id']}", headers=headers
    ).json()
    assert no_cache["retry_kind"] == "none"
    execute_sql(
        "update sessions set first_asr_failed_at = now() - interval '601 seconds' "
        "where id=$1",
        session["session_id"],
    )
    expired = app_client.post(
        f"/api/v1/sessions/{session['session_id']}/retry",
        json={"retry_kind": "asr"},
        headers={**headers, "Idempotency-Key": key()},
    )
    assert expired.status_code == 409
    assert expired.json()["error"]["code"] == "RETRY_UNAVAILABLE"
    assert expired.json()["error"]["details"]["reason"] == "audio_expired"


def test_tc_hist_12_audio_cache_capped_at_twenty(clean_database, monkeypatch):
    from app.main import create_app
    from tests.conftest import SERVER_ROOT, wait_for_readiness
    from fastapi.testclient import TestClient

    config_path = str(SERVER_ROOT / "config" / "app.yaml")
    first_app = create_app(config_path)
    with TestClient(first_app) as app_client:
        wait_for_readiness(app_client)
        assert app_client.get("/healthz").status_code == 200
        tokens = new_user(app_client, "hist12@example.com", "hist12-d1")
        headers = auth_headers(tokens)
        owners = retain_audio_owners(app_client, monkeypatch)
        fail_first_asr(app_client, monkeypatch)
        sessions = []
        expiries = {}
        for index in range(21):
            set_text(app_client, f"第{index}段请加一个按钮")
            session = create_session(app_client, headers, f"hist12-{index:03d}").json()
            session_id = session["session_id"]
            dictation(app_client, headers, session_id)
            failed = wait_state(app_client, headers, session_id, ("failed",))
            assert failed["retry_kind"] == "asr"
            runtime = owners[session_id]["runtime"]
            assert len(runtime.audio) == 16000
            assert runtime.retry_expires_at is not None
            expiries[session_id] = runtime.retry_expires_at
            sessions.append(session_id)
            if index == 19:
                assert sum(bool(r.audio) for r in first_app.state.sessions.runtimes.values()) == 20

        earliest = min(sessions, key=lambda sid: (expiries[sid], sid))
        assert earliest == sessions[0]
        cached = {
            sid for sid, runtime in first_app.state.sessions.runtimes.items()
            if runtime.audio and runtime.retry_expires_at is not None
        }
        assert cached == set(sessions) - {earliest}
        assert len(cached) == 20
        # Hold the evicted object and its original PCM buffer: deleting only the
        # dictionary entry must not make a leaked buffer satisfy this test.
        assert_audio_released(app_client, owners[earliest])
        evicted = app_client.get(f"/api/v1/sessions/{earliest}", headers=headers).json()
        assert evicted["retry_kind"] == "none"
        assert evicted["retry_unavailable_reason"] == "audio_evicted"
        for session_id in cached:
            detail = app_client.get(f"/api/v1/sessions/{session_id}", headers=headers).json()
            assert detail["retry_kind"] == "asr"
            assert detail["retry_unavailable_reason"] is None

    # The old lifespan has actually closed before a new process-equivalent app
    # starts. Keep all original references to prove shutdown cleared their PCM.
    assert first_app.state.sessions.runtimes == {}
    for owner in owners.values():
        assert_audio_released(app_client, owner)
    fresh_app = create_app(config_path)
    with TestClient(fresh_app) as fresh:
        wait_for_readiness(fresh)
        assert fresh.get("/healthz").status_code == 200
        fresh_login = login(fresh, "hist12@example.com", "hist12-d2")
        assert fresh_login.status_code == 200, fresh_login.text
        fresh_headers = auth_headers(fresh_login.json())
        for session_id in sessions:
            detail = fresh.get(f"/api/v1/sessions/{session_id}", headers=fresh_headers).json()
            assert detail["state"] == "failed"
            assert detail["retry_kind"] == "none"
            assert detail["retry_unavailable_reason"] == (
                "audio_evicted" if session_id == earliest else "server_restarted"
            )
            assert detail["result"] is None
            assert detail["received_samples"] == 8000
            assert detail["audio_complete"] is True
        assert fresh_app.state.sessions.runtimes == {}
