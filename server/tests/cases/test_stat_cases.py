from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import time
import uuid
from zoneinfo import ZoneInfo

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.models import (
    Device, Insertion, LoginSession, SessionAttempt, SessionContribution,
    SessionResult, User, VoiceSession,
)
from app.services import stats_service
from app.services.cleanup import cleanup_history
from tests.conftest import TEST_URL, auth_headers, execute_sql
from tests.cases.test_dict_cases import fetch_one, key, new_user
from tests.cases.test_learn_cases import (
    set_classifier,
    set_text,
    submit,
    wait_event,
)
from tests.test_sessions import create_session, dictation, frame

TZ = ZoneInfo("Asia/Shanghai")
PASSWORD = "Password123"


def uid(tokens) -> str:
    import jwt

    return jwt.decode(tokens["access_token"], options={"verify_signature": False})["sub"]


def add_contribution(
    user_id: str,
    session_id: str,
    day: str,
    samples: int,
    *,
    outcome="ready",
    has_result=True,
    chars=0,
    fallback=False,
    inserted=False,
    counted=True,
):
    # Arithmetic fixtures use real tenant-owned parents, including accounts
    # seeded without a desktop login (ADMIN-04). Typed metadata avoids reusing
    # an untyped $1 as both a SELECT expression and a VARCHAR predicate.
    business_day = dt.date.fromisoformat(day)
    created = dt.datetime.combine(business_day, dt.time(12), tzinfo=TZ)
    ended = None if outcome == "active" else created + dt.timedelta(seconds=samples / 16000)
    device_id, login_id, result_id = sid(), sid(), sid()
    text = "字" * chars if chars else "。"
    text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

    async def seed():
        engine = create_async_engine(TEST_URL, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                owner = await connection.scalar(select(User.id).where(User.id == user_id))
                assert owner is not None, "statistics fixture requires an existing user"
                await connection.execute(insert(Device).values(
                    id=device_id, user_id=user_id, installation_id=sid(),
                    name="statistics-fixture", platform="macos", client_version="0.0.1",
                    created_at=created, last_seen_at=created))
                await connection.execute(insert(LoginSession).values(
                    id=login_id, user_id=user_id, device_id=device_id, client_type="desktop",
                    # Store random digest bytes only; no access/refresh token is issued.
                    refresh_hash=uuid.uuid4().bytes + uuid.uuid4().bytes,
                    refresh_expires_at=created + dt.timedelta(days=30),
                    created_at=created, last_used_at=created, revoked_at=ended))
                await connection.execute(insert(VoiceSession).values(
                    id=session_id, user_id=user_id, device_id=device_id, created_login_sid=login_id,
                    client_request_id=session_id, request_hash=bytes(32), mode="dictation",
                    state="streaming" if outcome == "active" else outcome,
                    current_attempt_no=1, dictionary_version=0, learning_enabled=True,
                    received_samples=samples, audio_complete=outcome != "active", created_at=created,
                    end_accepted_at=ended, first_terminal_at=ended, retry_cache_status="none"))
                await connection.execute(insert(SessionAttempt).values(
                    id=sid(), user_id=user_id, session_id=session_id, attempt_no=1, kind="initial",
                    state="running" if outcome == "active" else outcome,
                    raw_text=text if has_result else None, started_at=created, ended_at=ended))
                if has_result:
                    assert outcome == "ready", "a result fixture must be ready"
                    await connection.execute(insert(SessionResult).values(
                        id=result_id, user_id=user_id, session_id=session_id, attempt_no=1,
                        raw_text=text, refined_text=text, review_required=fallback,
                        review_reason="llm_unavailable" if fallback else None,
                        text_hash=text_hash, dictionary_version=0, char_count=chars, created_at=ended))
                    await connection.execute(update(VoiceSession).where(
                        VoiceSession.user_id == user_id, VoiceSession.id == session_id
                    ).values(current_result_id=result_id))
                if inserted:
                    assert has_result, "an insertion fixture requires its result parent"
                    await connection.execute(insert(Insertion).values(
                        id=sid(), user_id=user_id, session_id=session_id, result_id=result_id,
                        device_id=device_id, text_hash=text_hash, review_confirmed=fallback,
                        status="inserted", observable=True, request_hash=bytes(32),
                        claimed_at=ended, completed_at=ended))
                # Preserve the F-STAT arithmetic inputs and expected totals.
                await connection.execute(insert(SessionContribution).values(
                    user_id=user_id, session_id=session_id, business_date=business_day,
                    received_samples=samples, counted_session=counted, outcome=outcome,
                    has_result=has_result, char_count=chars,
                    successful_audio_samples=samples if has_result else 0,
                    fallback=fallback, inserted=inserted, revision=1))
        finally:
            await engine.dispose()

    asyncio.run(seed())


def sid() -> str:
    return str(uuid.uuid4())


def stats(client, headers, date_from, date_to, **params):
    return client.get(
        "/api/v1/stats/me",
        params={"date_from": date_from, "date_to": date_to, **params},
        headers=headers,
    )


def run_session(app_client, headers, request_id, text="请帮我加一个按钮"):
    set_text(app_client, text)
    session = create_session(app_client, headers, request_id).json()
    events = dictation(app_client, headers, session["session_id"])
    return session, events[-1]["result"]


def claim_and_receipt(app_client, headers, session, result, status="inserted"):
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
    assert claim.status_code == 201, claim.text
    receipt = app_client.post(
        f"/api/v1/sessions/{session['session_id']}/insertions/{insertion_id}/receipt",
        json={"status": status, "observable": True, "error_code": None},
        headers=headers,
    )
    assert receipt.status_code == 200, receipt.text
    return insertion_id


def learn_once(app_client, headers, request_id, text, before, after):
    set_text(app_client, text)
    session = create_session(app_client, headers, request_id).json()
    events = dictation(app_client, headers, session["session_id"])
    result = events[-1]["result"]
    insertion_id = claim_and_receipt(app_client, headers, session, result)
    response = submit(
        app_client,
        headers,
        {**session, "insertion_id": insertion_id},
        result,
        before,
        after,
    )
    assert response.status_code == 202, response.text
    return wait_event(app_client, headers, response.json()["event_id"])


def test_tc_stat_01_fixture_totals(app_client):
    tokens = new_user(app_client, "stat01@example.com", "stat01-d1")
    headers = auth_headers(tokens)
    user = uid(tokens)
    add_contribution(user, sid(), "2026-10-16", 160000, chars=5, inserted=True)
    add_contribution(user, sid(), "2026-10-16", 320000, chars=8)
    add_contribution(
        user,
        sid(),
        "2026-10-16",
        80000,
        outcome="cancelled",
        has_result=False,
        counted=True,
    )
    body = stats(app_client, headers, "2026-10-16", "2026-10-16").json()
    totals = body["totals"]
    assert totals["sessions"] == 3
    assert totals["received_audio_ms"] == 35000
    assert totals["result_sessions"] == 2
    assert totals["chars"] == 13
    assert totals["successful_audio_ms"] == 30000
    assert totals["average_chars_per_minute"] == 26.0
    assert totals["processing_success_rate"] == 1.0
    assert totals["insertion_success_rate"] == 0.5
    assert totals["fallback_rate"] == 0.0
    assert body["active_days_in_range"] == 1
    assert body["daily"][0]["sessions"] == 3
    assert body["daily"][0]["heat_level"] == 2


def test_tc_stat_02_natural_day_streaks(app_client, monkeypatch):
    tokens = new_user(app_client, "stat02@example.com", "stat02-d1")
    headers = auth_headers(tokens)
    user = uid(tokens)
    for day, count in (("2026-10-14", 1), ("2026-10-15", 2), ("2026-10-16", 5)):
        for _ in range(count):
            add_contribution(user, sid(), day, 16000, chars=1)
    monkeypatch.setattr(
        stats_service,
        "utcnow",
        lambda: dt.datetime(2026, 10, 17, 4, 0, tzinfo=dt.timezone.utc),
    )
    body = stats(app_client, headers, "2026-10-14", "2026-10-17").json()
    assert body["active_days_in_range"] == 3
    assert body["current_streak_days"] == 3
    assert body["longest_streak_days"] == 3
    assert [day["heat_level"] for day in body["daily"]] == [1, 2, 3, 0]
    assert [day["sessions"] for day in body["daily"]] == [1, 2, 5, 0]

    # 23:59:59 local creation still belongs to the 17th.
    add_contribution(user, sid(), "2026-10-17", 16000, chars=1)
    monkeypatch.setattr(
        stats_service,
        "utcnow",
        lambda: dt.datetime(2026, 10, 18, 4, 0, tzinfo=dt.timezone.utc),
    )
    body = stats(app_client, headers, "2026-10-14", "2026-10-18").json()
    assert body["active_days_in_range"] == 4
    assert body["current_streak_days"] == 4
    assert body["longest_streak_days"] == 4
    assert body["daily"][-1]["heat_level"] == 0
    monkeypatch.setattr(
        stats_service,
        "utcnow",
        lambda: dt.datetime(2026, 10, 19, 4, 0, tzinfo=dt.timezone.utc),
    )
    body = stats(app_client, headers, "2026-10-14", "2026-10-19").json()
    assert body["current_streak_days"] == 0
    assert body["longest_streak_days"] == 4


def test_tc_stat_03_heat_levels(app_client):
    tokens = new_user(app_client, "stat03@example.com", "stat03-d1")
    headers = auth_headers(tokens)
    user = uid(tokens)
    counts = [0, 1, 2, 4, 5, 9, 10]
    for index, count in enumerate(counts):
        day = f"2026-09-{index + 1:02d}"
        for _ in range(count):
            add_contribution(user, sid(), day, 16000, chars=1)
    body = stats(app_client, headers, "2026-09-01", "2026-09-07").json()
    assert [day["heat_level"] for day in body["daily"]] == [0, 1, 2, 2, 3, 3, 4]


def test_tc_stat_04_learning_count_and_terms(app_client, monkeypatch):
    set_classifier(app_client, monkeypatch, decision="term")
    tokens = new_user(app_client, "stat04@example.com", "stat04-d1")
    headers = auth_headers(tokens)
    first = learn_once(
        app_client, headers, "stat04-one", "请用 cloud 保存", "cloud", "Claude"
    )
    second = learn_once(
        app_client, headers, "stat04-two", "请用 docker 保存", "docker", "Docker"
    )
    assert first["status"] == "learned"
    assert second["status"] == "learned"
    undo = app_client.post(
        f"/api/v1/learning-events/{first['event_id']}/undo",
        headers={**headers, "Idempotency-Key": key()},
    )
    assert undo.status_code == 200
    day = dt.datetime.now(TZ).date().isoformat()
    body = stats(app_client, headers, day, day).json()
    assert body["cumulative_learning_events"] == 2
    assert body["dictionary_terms"] == 1


def test_tc_stat_05_reads_are_side_effect_free_and_retry_replaces(app_client):
    tokens = new_user(app_client, "stat05@example.com", "stat05-d1")
    headers = auth_headers(tokens)
    session, result = run_session(app_client, headers, "stat05-session", "请帮我加一个按钮")
    day = dt.datetime.now(TZ).date().isoformat()
    first = stats(app_client, headers, day, day).json()
    for _ in range(10):
        assert stats(app_client, headers, day, day).json() == first
    assert first["totals"]["sessions"] == 1

    execute_sql(
        "update results set raw_text='新的结果文本', review_required=true where id=$1",
        result["result_id"],
    )
    retry = app_client.post(
        f"/api/v1/sessions/{session['session_id']}/retry",
        json={"retry_kind": "refine"},
        headers={**headers, "Idempotency-Key": key()},
    )
    assert retry.status_code == 202, retry.text
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        detail = app_client.get(
            f"/api/v1/sessions/{session['session_id']}", headers=headers
        ).json()
        if detail["state"] == "ready" and detail["result"]["attempt_no"] == 2:
            break
        time.sleep(0.05)
    updated = stats(app_client, headers, day, day).json()
    assert updated["totals"]["sessions"] == 1
    assert updated["totals"]["chars"] == 6

    # A replayed receipt is side-effect free on a second session.
    second, second_result = run_session(
        app_client, headers, "stat05-second", "请帮我加一个按钮"
    )
    insertion_id = claim_and_receipt(app_client, headers, second, second_result)
    before_replay = stats(app_client, headers, day, day).json()
    replay = app_client.post(
        f"/api/v1/sessions/{second['session_id']}/insertions/{insertion_id}/receipt",
        json={"status": "inserted", "observable": True, "error_code": None},
        headers=headers,
    )
    assert replay.status_code == 200
    assert stats(app_client, headers, day, day).json() == before_replay
    assert before_replay["totals"]["sessions"] == 2


def test_tc_stat_06_tenant_isolation_and_admin_denied(app_client):
    u1 = new_user(app_client, "stat06-u1@example.com", "stat06-d1")
    u2 = new_user(app_client, "stat06-u2@example.com", "stat06-d2")
    h1 = auth_headers(u1)
    add_contribution(uid(u1), sid(), "2026-10-16", 16000, chars=1)
    add_contribution(uid(u2), sid(), "2026-10-16", 32000, chars=2)
    one = stats(app_client, h1, "2026-10-16", "2026-10-16").json()
    assert one["totals"]["sessions"] == 1
    assert one["totals"]["chars"] == 1
    forbidden = stats(
        app_client, h1, "2026-10-16", "2026-10-16", user_id=uid(u2)
    )
    assert forbidden.status_code == 422
    assert forbidden.json()["error"]["code"] == "VALIDATION_ERROR"
    admin = app_client.get("/api/admin/stats/overview", headers=h1)
    assert admin.status_code == 403


def test_tc_stat_07_cleanup_keeps_frozen_contributions(app_client):
    tokens = new_user(app_client, "stat07@example.com", "stat07-d1")
    headers = auth_headers(tokens)
    user = uid(tokens)
    session, result = run_session(app_client, headers, "stat07-a", "你好 API")
    execute_sql(
        "update session_contributions set received_samples=160000, char_count=5, "
        "successful_audio_samples=160000, inserted=true where session_id=$1",
        session["session_id"],
    )
    add_contribution(user, sid(), "2026-10-16", 320000, chars=8)
    add_contribution(
        user,
        sid(),
        "2026-10-16",
        80000,
        outcome="cancelled",
        has_result=False,
        counted=True,
    )
    execute_sql(
        "update session_contributions set business_date='2026-10-16' "
        "where session_id=$1",
        session["session_id"],
    )
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
        "select raw_text from results where id=$1", result["result_id"]
    ) is None
    body = stats(app_client, headers, "2026-10-16", "2026-10-16").json()
    assert body["totals"]["sessions"] == 3
    assert body["totals"]["chars"] == 13
    assert body["totals"]["successful_audio_ms"] == 30000
    assert body["totals"]["received_audio_ms"] == 35000
    assert body["totals"]["average_chars_per_minute"] == 26.0


def test_tc_stat_08_rates_fixture(app_client):
    tokens = new_user(app_client, "stat08@example.com", "stat08-d1")
    headers = auth_headers(tokens)
    user = uid(tokens)
    add_contribution(user, sid(), "2026-10-16", 16000, chars=1, inserted=True)
    add_contribution(user, sid(), "2026-10-16", 16000, chars=1, fallback=True)
    add_contribution(
        user,
        sid(),
        "2026-10-16",
        16000,
        outcome="failed",
        has_result=False,
        counted=True,
    )
    add_contribution(
        user,
        sid(),
        "2026-10-16",
        16000,
        outcome="empty",
        has_result=False,
        counted=True,
    )
    add_contribution(
        user,
        sid(),
        "2026-10-16",
        16000,
        outcome="cancelled",
        has_result=False,
        counted=True,
    )
    body = stats(app_client, headers, "2026-10-16", "2026-10-16").json()
    assert body["totals"]["processing_success_rate"] == 0.5
    assert body["totals"]["fallback_rate"] == 0.5
    assert body["totals"]["insertion_success_rate"] == 0.5


def test_tc_stat_09_zero_denominators_and_unicode_count(app_client):
    fresh = new_user(app_client, "stat09-fresh@example.com", "stat09-d1")
    day = dt.datetime.now(TZ).date().isoformat()
    empty = stats(app_client, auth_headers(fresh), day, day).json()
    assert empty["totals"]["sessions"] == 0
    assert empty["totals"]["average_chars_per_minute"] == 0
    assert empty["totals"]["processing_success_rate"] == 0
    assert empty["totals"]["insertion_success_rate"] == 0

    tokens = new_user(app_client, "stat09-cancel@example.com", "stat09-d2")
    headers = auth_headers(tokens)
    session = create_session(app_client, headers, "stat09-cancelled").json()
    assert (
        app_client.post(
            f"/api/v1/sessions/{session['session_id']}/cancel", headers=headers
        ).status_code
        == 200
    )
    cancelled = stats(app_client, headers, day, day).json()
    assert cancelled["totals"]["sessions"] == 0
    assert cancelled["totals"]["processing_success_rate"] == 0
    assert cancelled["totals"]["average_chars_per_minute"] == 0

    received = create_session(app_client, headers, "stat09-cancelled-after-sample").json()
    with app_client.websocket_connect(f"/ws/v1/sessions/{received['session_id']}/audio", headers=headers) as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_bytes(frame(0, 1))
        ack = ws.receive_json()
        assert ack["type"] == "audio.ack" and ack["total_samples"] == 1
        response = app_client.post(f"/api/v1/sessions/{received['session_id']}/cancel", headers=headers)
        assert response.status_code == 200
        assert ws.receive_json()["type"] == "cancelled"
    cancelled = stats(app_client, headers, day, day).json()
    assert cancelled["totals"]["sessions"] == 1
    assert cancelled["totals"]["processing_success_rate"] == 0
    assert cancelled["totals"]["average_chars_per_minute"] == 0

    counts = [
        ("你好 API", 5),
        ("版本 2 React", 13),
        ("🙂 API 2.0！", 18),
        ("é", 19),
        ("é", 20),
    ]
    count_tokens = new_user(app_client, "stat09-count@example.com", "stat09-d3")
    count_headers = auth_headers(count_tokens)
    for index, (text, expected) in enumerate(counts):
        run_session(app_client, count_headers, f"stat09-count-{index}", text)
        body = stats(app_client, count_headers, day, day).json()
        assert body["totals"]["chars"] == expected, (text, body["totals"]["chars"])


def test_tc_stat_10_date_range_validation(app_client):
    tokens = new_user(app_client, "stat10@example.com", "stat10-d1")
    headers = auth_headers(tokens)
    assert stats(app_client, headers, "2026-10-17", "2026-10-17").status_code == 200
    assert stats(app_client, headers, "2025-10-17", "2026-10-17").status_code == 200
    assert stats(app_client, headers, "2025-10-16", "2026-10-17").status_code == 422
    assert stats(app_client, headers, "2025-10-15", "2026-10-17").status_code == 422
    assert stats(app_client, headers, "2026-10-17", "2026-10-01").status_code == 422


def test_tc_stat_11_learning_idempotency_does_not_double_count(app_client, monkeypatch):
    set_classifier(app_client, monkeypatch, decision="suggest", ambiguous=True)
    tokens = new_user(app_client, "stat11@example.com", "stat11-d1")
    headers = auth_headers(tokens)
    event = learn_once(
        app_client, headers, "stat11-one", "请用 cloud 保存", "cloud", "Claude"
    )
    assert event["status"] == "suggested"
    confirm_key = key()
    first = app_client.post(
        f"/api/v1/learning-events/{event['event_id']}/confirm",
        headers={**headers, "Idempotency-Key": confirm_key},
    )
    assert first.status_code == 200
    second = app_client.post(
        f"/api/v1/learning-events/{event['event_id']}/confirm",
        headers={**headers, "Idempotency-Key": confirm_key},
    )
    assert second.status_code == 200
    undo_key = key()
    for _ in range(2):
        undone = app_client.post(
            f"/api/v1/learning-events/{event['event_id']}/undo",
            headers={**headers, "Idempotency-Key": undo_key},
        )
        assert undone.status_code == 200
    day = dt.datetime.now(TZ).date().isoformat()
    body = stats(app_client, headers, day, day).json()
    assert body["cumulative_learning_events"] == 1
    assert body["dictionary_terms"] == 0
    other = new_user(app_client, "stat11-other@example.com", "stat11-d2")
    other_body = stats(app_client, auth_headers(other), day, day).json()
    assert other_body["cumulative_learning_events"] == 0
    assert other_body["dictionary_terms"] == 0
