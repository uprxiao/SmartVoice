from __future__ import annotations

from tests.ids import fixture_uuid

import datetime as dt
import uuid
from zoneinfo import ZoneInfo

import pytest

from tests.conftest import auth_headers, execute_sql, register_and_login
from tests.cases.test_dict_cases import fetch_one, key, new_user
from tests.cases.test_stat_cases import add_contribution, sid, uid
from tests.test_admin import login_admin
from tests.test_sessions import create_session
from tests.usage_fixture import seed_f_usage

TZ = ZoneInfo("Asia/Shanghai")
DAY = "2026-10-16"


def admin_headers(app_client):
    return auth_headers(login_admin(app_client))


def test_tc_admin_01_role_isolation(app_client):
    user = new_user(app_client, "admin01-user@example.com", "admin01-d1")
    assert (
        app_client.get("/api/admin/users", headers=auth_headers(user)).status_code
        == 403
    )
    admin = admin_headers(app_client)
    assert app_client.get("/api/admin/users", headers=admin).status_code == 200


def test_tc_admin_02_create_user_and_login(app_client):
    admin = admin_headers(app_client)
    created = app_client.post(
        "/api/admin/users",
        json={"email": "admin02-member@example.com", "password": "MemberPass123"},
        headers={**admin, "Idempotency-Key": key()},
    )
    assert created.status_code == 201, created.text
    assert created.json()["user"]["role"] == "user"
    login = app_client.post(
        "/api/auth/login",
        json={
            "email": "admin02-member@example.com",
            "password": "MemberPass123",
            "client_type": "desktop",
            "device": {
                "installation_id": fixture_uuid("admin02-d1"),
                "name": "member-mac",
                "platform": "macos",
                "client_version": "0.0.1",
            },
        },
    )
    assert login.status_code == 200, login.text
    duplicate = app_client.post(
        "/api/admin/users",
        json={"email": "admin02-member@example.com", "password": "MemberPass123"},
        headers={**admin, "Idempotency-Key": key()},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "EMAIL_EXISTS"


def test_tc_admin_04_user_pagination_search_and_privacy(app_client):
    admin = admin_headers(app_client)
    execute_sql(
        """
        insert into users
          (id, email, password_hash, role, status, locale, learning_enabled,
           settings_version, created_at)
        select 'u' || lpad(g::text, 7, '0') || '-0000-4000-8000-' ||
               lpad(g::text, 10, '0'),
               'member' || g || '@example.com', 'x', 'user', 'active',
               'zh-CN', true, 1, timestamptz '2026-10-01 12:00:00+00'
        from generate_series(1, 50) g
        """,
    )
    execute_sql(
        "insert into users (id, email, password_hash, role, status, locale, "
        "learning_enabled, settings_version, created_at) values "
        "('pct-user-0000-4000-8000-000000000001', 'pct%user@example.com', 'x', "
        "'user', 'active', 'zh-CN', true, 1, "
        "timestamptz '2026-10-01 12:00:00+00'), "
        "('und-user-0000-4000-8000-000000000001', 'under_score@example.com', 'x', "
        "'user', 'active', 'zh-CN', true, 1, "
        "timestamptz '2026-10-01 12:00:00+00')"
    )
    page1 = app_client.get(
        "/api/admin/users", params={"limit": 20}, headers=admin
    ).json()
    page2 = app_client.get(
        "/api/admin/users",
        params={"limit": 20, "cursor": page1["next_cursor"]},
        headers=admin,
    ).json()
    page3 = app_client.get(
        "/api/admin/users",
        params={"limit": 20, "cursor": page2["next_cursor"]},
        headers=admin,
    ).json()
    assert [len(page1["items"]), len(page2["items"]), len(page3["items"])] == [
        20,
        20,
        13,
    ]
    ids = [
        item["user"]["id"]
        for page in (page1, page2, page3)
        for item in page["items"]
    ]
    assert len(set(ids)) == 53
    percent = app_client.get(
        "/api/admin/users", params={"q": "%"}, headers=admin
    ).json()["items"]
    assert [item["user"]["email"] for item in percent] == ["pct%user@example.com"]
    underscore = app_client.get(
        "/api/admin/users", params={"q": "_"}, headers=admin
    ).json()["items"]
    assert [item["user"]["email"] for item in underscore] == [
        "under_score@example.com"
    ]
    target_id = fetch_one(
        "select id from users where email=$1", "member1@example.com"
    )
    add_contribution(target_id, sid(), DAY, 16000, chars=2)
    searched = app_client.get(
        "/api/admin/users", params={"q": "member1@"}, headers=admin
    ).json()["items"]
    assert len(searched) == 1
    assert searched[0]["totals"]["sessions"] == 1
    raw = str(page1)
    for forbidden in ("password_hash", "access_token", "refresh_token", "refined_text"):
        assert forbidden not in raw


def test_tc_admin_05_overview_fixture(app_client):
    admin = admin_headers(app_client)
    user = new_user(app_client, "admin05@example.com", "admin05-d1")
    owner = uid(user)
    add_contribution(owner, sid(), DAY, 160000, chars=5, inserted=True)
    add_contribution(owner, sid(), DAY, 320000, chars=8)
    add_contribution(owner, sid(), DAY, 80000,
        outcome="cancelled", has_result=False, counted=True)

    def read(path, headers=admin, **params):
        response = app_client.get(path,
            params={"date_from": DAY, "date_to": DAY, **params}, headers=headers)
        assert response.status_code == 200, response.text
        return response.json()

    # Literal F-STAT-1 arithmetic, not an expected value obtained from another
    # production endpoint/SQL aggregate: 10s + 20s result, 5s cancellation.
    expected_u1 = {
        "sessions": 3, "received_audio_ms": 35000, "result_sessions": 2,
        "failed_sessions": 0, "empty_sessions": 0, "cancelled_sessions": 1,
        "chars": 13, "successful_audio_ms": 30000,
        "average_chars_per_minute": 26.0, "fallback_sessions": 0,
        "inserted_sessions": 1, "processing_success_rate": 1.0,
        "insertion_success_rate": 0.5, "fallback_rate": 0.0, "learning_events": 0,
    }
    expected_daily = {
        "date": DAY, "daily_active_users": 1, "sessions": 3,
        "received_audio_ms": 35000, "result_sessions": 2,
        "failed_sessions": 0, "empty_sessions": 0, "cancelled_sessions": 1,
    }
    body = read("/api/admin/stats/overview")
    assert (body["timezone"], body["date_from"], body["date_to"]) == ("Asia/Shanghai", DAY, DAY)
    assert body["distinct_active_users"] == 1
    assert body["totals"] == expected_u1
    assert body["daily"] == [expected_daily]
    assert read("/api/v1/stats/me", auth_headers(user))["totals"] == expected_u1
    u1_row = {
        "date": DAY, "user_id": owner, "sessions": 3,
        "received_audio_ms": 35000, "result_sessions": 2, "chars": 13,
        "successful_audio_ms": 30000, "fallback_sessions": 0,
        "inserted_sessions": 1, "learning_events": 0,
    }
    assert read("/api/admin/stats/usage", user_id=owner) == {
        "timezone": "Asia/Shanghai", "items": [u1_row], "next_cursor": None,
    }

    # A different user's declared F-USAGE fixture makes leakage observable.
    # Its seven recorder facts are not seven sessions: user audio is 3000ms,
    # even though overlapping ASR processing windows sum to 6000ms.
    other = new_user(app_client, "admin05-other@example.com", "admin05-d2")
    fixture = seed_f_usage(app_client, other, DAY, "admin05-f-usage-other")
    expected_u2 = {
        **expected_u1, "sessions": 1, "received_audio_ms": 3000,
        "result_sessions": 1, "cancelled_sessions": 0, "chars": 10,
        "successful_audio_ms": 3000, "average_chars_per_minute": 200.0,
        "insertion_success_rate": 1.0,
    }
    u2_row = {
        "date": DAY, "user_id": fixture.user_id, "sessions": 1,
        "received_audio_ms": 3000, "result_sessions": 1, "chars": 10,
        "successful_audio_ms": 3000, "fallback_sessions": 0,
        "inserted_sessions": 1, "learning_events": 0,
    }
    all_usage = read("/api/admin/stats/usage")
    assert all_usage == {
        "timezone": "Asia/Shanghai",
        "items": sorted([u1_row, u2_row], key=lambda row: row["user_id"]),
        "next_cursor": None,
    }
    for tokens, expected_totals, expected_row in (
        (user, expected_u1, u1_row), (other, expected_u2, u2_row),
    ):
        assert read("/api/v1/stats/me", auth_headers(tokens))["totals"] == expected_totals
        assert read("/api/admin/stats/usage", user_id=expected_row["user_id"]) == {
            "timezone": "Asia/Shanghai", "items": [expected_row], "next_cursor": None,
        }
        # Valid dates/body ensure this is actual role denial, not schema 422.
        for path, params in (
            ("/api/admin/stats/overview", {}),
            ("/api/admin/stats/usage", {"user_id": owner}),
            ("/api/admin/stats/usage", {"user_id": fixture.user_id}),
        ):
            denied = app_client.get(path, headers=auth_headers(tokens),
                params={"date_from": DAY, "date_to": DAY, **params})
            assert denied.status_code == 403, denied.text
            assert denied.json()["error"]["code"] == "FORBIDDEN"

    body = read("/api/admin/stats/overview", date_from="2026-10-15")
    assert body["distinct_active_users"] == 2
    assert body["totals"] == {
        **expected_u1, "sessions": 4, "received_audio_ms": 38000,
        "result_sessions": 3, "chars": 23, "successful_audio_ms": 33000,
        "average_chars_per_minute": 41.8, "inserted_sessions": 2,
        "insertion_success_rate": 0.6667,
    }
    assert body["daily"] == [
        {"date": "2026-10-15", "daily_active_users": 0, "sessions": 0,
         "received_audio_ms": 0, "result_sessions": 0, "failed_sessions": 0,
         "empty_sessions": 0, "cancelled_sessions": 0},
        {**expected_daily, "daily_active_users": 2, "sessions": 4,
         "received_audio_ms": 38000, "result_sessions": 3},
    ]
    # Filtering a different date cannot leak either user's same-day facts.
    assert read("/api/admin/stats/usage", date_from="2026-10-15", date_to="2026-10-15") == {
        "timezone": "Asia/Shanghai", "items": [], "next_cursor": None,
    }


@pytest.mark.parametrize("path", ["/api/admin/stats/overview", "/api/admin/stats/usage"])
def test_tc_admin_05_stat_date_contract(app_client, path):
    admin = admin_headers(app_client)
    for params in (
        {"date_to": DAY},
        {"date_from": DAY},
        {"date_from": "2026-13-01", "date_to": DAY},
        {"date_from": "2026-10-17", "date_to": DAY},
        {"date_from": "2025-01-01", "date_to": "2026-01-02"},  # 367 inclusive days
    ):
        response = app_client.get(path, params=params, headers=admin)
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    # The written upper bound is 366 inclusive days, not 365.
    valid = app_client.get(path, headers=admin,
        params={"date_from": "2025-01-01", "date_to": "2026-01-01"})
    assert valid.status_code == 200, valid.text


def test_tc_admin_06_model_usage_fixture(app_client):
    admin = admin_headers(app_client)
    user = new_user(app_client, "admin06@example.com", "admin06-d1")
    fixture = seed_f_usage(app_client, user, DAY, "admin06-f-usage")
    response = app_client.get(
        "/api/admin/stats/models",
        params={"date_from": DAY, "date_to": DAY},
        headers=admin,
    )
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    # F-USAGE has two incremental decodes and one final decode: three actual
    # fixture calls / six seconds processed, not three finalization attempts.
    asr = [item for item in items if item["purpose"] in ("asr_decode", "asr_finish")]
    assert sum(item["calls"] for item in asr) == 3
    assert sum(item["processed_audio_ms"] for item in asr) == 6000
    assert {item["purpose"]: item["calls"] for item in asr} == {"asr_decode": 2, "asr_finish": 1}
    refine = next(item for item in items if item["purpose"] == "refine")
    assert refine["calls"] == 2
    assert refine["successes"] == 1
    assert refine["failures"] == 1
    assert refine["prompt_tokens_known"] == 100
    assert refine["completion_tokens_known"] == 20
    assert refine["prompt_tokens_unknown_calls"] == 1
    assert refine["completion_tokens_unknown_calls"] == 1
    audit = next(item for item in items if item["purpose"] == "audit")
    assert (audit["prompt_tokens_known"], audit["completion_tokens_known"]) == (40, 5)
    classify = next(item for item in items if item["purpose"] == "classify")
    assert (classify["prompt_tokens_known"], classify["completion_tokens_known"]) == (30, 3)
    llm = [item for item in items if item["purpose"] in ("refine", "audit", "classify")]
    assert sum(item["calls"] for item in llm) == 4
    assert sum(item["successes"] for item in llm) == 3
    assert sum(item["failures"] for item in llm) == 1
    assert sum(item["prompt_tokens_known"] for item in llm) == 170
    assert sum(item["completion_tokens_known"] for item in llm) == 28
    assert sum(item["prompt_tokens_unknown_calls"] for item in llm) == 1
    assert sum(item["completion_tokens_unknown_calls"] for item in llm) == 1
    assert fetch_one("select count(*) from model_calls where user_id=$1 and aggregated_at is not null", fixture.user_id) == 7
    personal = app_client.get("/api/v1/stats/me", headers=auth_headers(user),
        params={"date_from": DAY, "date_to": DAY})
    assert personal.status_code == 200, personal.text
    assert personal.json()["totals"]["received_audio_ms"] == 3000


def test_tc_admin_07_role_revoked_is_checked_per_request(app_client):
    admin = admin_headers(app_client)
    admin_id = fetch_one(
        "select id from users where email=$1", "admin@example.com"
    )
    execute_sql("update users set role='user' where id=$1", admin_id)
    for path in ("/api/admin/users", "/api/admin/stats/overview", "/api/admin/capacity"):
        response = app_client.get(
            path,
            params={"date_from": DAY, "date_to": DAY},
            headers=admin,
        )
        assert response.status_code == 403, (path, response.text)
        assert response.json()["error"]["code"] == "FORBIDDEN"


def test_tc_admin_08_missing_token_is_401(app_client):
    for path in ("/api/admin/users", "/api/admin/stats/overview"):
        response = app_client.get(
            path, params={"date_from": DAY, "date_to": DAY}
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "AUTH_INVALID"
        assert "password" not in response.text.lower()


@pytest.mark.parametrize("second_admin_status", ["active", "disabled"])
def test_tc_admin_09_protects_self_and_last_admin(app_client, second_admin_status):
    admin = admin_headers(app_client)
    admin_id = fetch_one(
        "select id from users where email=$1", "admin@example.com"
    )
    second_tokens = register_and_login(app_client, email="admin09-second@example.com")
    second_id = uid(second_tokens)
    execute_sql("UPDATE users SET role='admin' WHERE id=$1", second_id)
    second_login = app_client.post("/api/auth/login", json={
        "email": "admin09-second@example.com", "password": "SmokePass123",
        "client_type": "admin_web",
    })
    assert second_login.status_code == 200
    second_admin = auth_headers(second_login.json())
    if second_admin_status == "disabled":
        disabled_second = app_client.patch(f"/api/admin/users/{second_id}",
            json={"status": "disabled"}, headers=admin)
        assert disabled_second.status_code == 200
        assert disabled_second.json()["user"]["status"] == "disabled"

    def protected_state():
        actors = [admin_id, second_id]
        # Normal authenticated reads update last_used_at, not SID credentials,
        # revocation or user status; exclude only that expected touch timestamp.
        return (
            fetch_one("SELECT md5(jsonb_agg(jsonb_build_object('id',id,'role',role,'status',status) ORDER BY id)::text) FROM users WHERE id=ANY($1::varchar[])", actors),
            fetch_one("SELECT md5(jsonb_agg(to_jsonb(t)-'last_used_at' ORDER BY id)::text) FROM login_sessions t WHERE user_id=ANY($1::varchar[])", actors),
        )

    before = protected_state()
    assert fetch_one("SELECT status FROM users WHERE id=$1", second_id) == second_admin_status
    assert fetch_one("SELECT count(*) FROM users WHERE role='admin' AND status='active'") == (
        2 if second_admin_status == "active" else 1)
    self_disable = app_client.patch(
        f"/api/admin/users/{admin_id}",
        json={"status": "disabled"},
        headers=admin,
    )
    assert self_disable.status_code == 409
    assert self_disable.json()["error"]["code"] == "ADMIN_PROTECTED"
    assert protected_state() == before
    # Read back via the original access tokens; rejected self-disable must not
    # revoke A0, and must not resurrect an already disabled A1's old SID.
    assert app_client.get("/api/admin/users", headers=admin).status_code == 200
    old_second = app_client.get("/api/admin/users", headers=second_admin)
    if second_admin_status == "active":
        assert old_second.status_code == 200
    else:
        assert old_second.status_code == 403
        assert old_second.json()["error"]["code"] == "ACCOUNT_DISABLED"
    assert protected_state() == before
    member = app_client.post(
        "/api/admin/users",
        json={"email": "admin09-member@example.com", "password": "MemberPass123"},
        headers={**admin, "Idempotency-Key": key()},
    ).json()["user"]
    disabled = app_client.patch(
        f"/api/admin/users/{member['id']}",
        json={"status": "disabled"},
        headers=admin,
    )
    assert disabled.status_code == 200
    assert disabled.json()["user"]["status"] == "disabled"
    assert fetch_one("SELECT status FROM users WHERE id=$1", member["id"]) == "disabled"
    assert protected_state() == before


def test_tc_admin_10_usage_is_idempotent_and_survives_cleanup(app_client, monkeypatch):
    from app.services import cleanup as cleanup_module

    admin = admin_headers(app_client)
    user = new_user(app_client, "admin10@example.com", "admin10-d1")
    fixture = seed_f_usage(app_client, user, DAY, "admin10-f-usage")

    def model_usage():
        response = app_client.get("/api/admin/stats/models",
            params={"date_from": DAY, "date_to": DAY}, headers=admin)
        assert response.status_code == 200, response.text
        return response.json()

    first = model_usage()
    assert sum(item["calls"] for item in first["items"]) == 7
    for _ in range(10):
        # Replay a business idempotency key, rather than merely repeating GET.
        replay = create_session(app_client, auth_headers(user), fixture.request_label)
        assert replay.status_code == 200, replay.text
        assert replay.json()["replayed"] is True
        assert replay.json()["session_id"] == fixture.session_id
        assert model_usage() == first
    # A delayed duplicate terminal callback cannot reaggregate or overwrite the
    # known/unknown token split, even if its attempted values differ.
    for call_id in fixture.call_ids:
        app_client.portal.call(app_client.app.state.recorder.finished,
            call_id, "succeeded", None, 999, 999, 999)
    assert model_usage() == first

    # Ensure the cleanup actually clears content (the former test had no
    # retained session/result and no expired policy, so cleanup was a no-op).
    execute_sql("update users set history_retention_days=7 where id=$1", fixture.user_id)
    expired_now = dt.datetime.fromisoformat(DAY + "T10:00:00+00:00") + dt.timedelta(days=8)
    monkeypatch.setattr(cleanup_module, "utcnow", lambda: expired_now)
    assert app_client.portal.call(cleanup_module.cleanup_history, app_client.app) == 1
    assert fetch_one("select raw_text is null and refined_text is null from results where id=$1", fixture.result_id) is True
    assert fetch_one("select raw_text is null from session_attempts where user_id=$1 and session_id=$2 and attempt_no=1",
        fixture.user_id, fixture.session_id) is True
    assert fetch_one("select before_text is null and after_text is null from learning_events where id=$1", fixture.learning_event_id) is True
    assert model_usage() == first
    assert fetch_one("select count(*) from model_calls where user_id=$1", fixture.user_id) == 7
    refine = next(item for item in first["items"] if item["purpose"] == "refine")
    # The permitted retry is the second F-USAGE refine dispatch. It remains
    # counted after replay/cleanup. Do not manufacture a forbidden third call
    # after inserted/expired content merely to make the counter increase.
    assert (refine["calls"], refine["failures"], refine["successes"]) == (2, 1, 1)
    assert (refine["prompt_tokens_known"], refine["completion_tokens_known"]) == (100, 20)
    assert (refine["prompt_tokens_unknown_calls"], refine["completion_tokens_unknown_calls"]) == (1, 1)


def test_tc_admin_11_admin_cannot_read_other_users_content(app_client):
    user = new_user(app_client, "admin11-user@example.com", "admin11-d1")
    session = create_session(app_client, auth_headers(user), "admin11-session").json()
    admin = admin_headers(app_client)
    admin_desktop_login = app_client.post(
        "/api/auth/login",
        json={
            "email": "admin@example.com",
            "password": "SmokePass123",
            "client_type": "desktop",
            "device": {
                "installation_id": fixture_uuid("admin11-d1"),
                "name": "admin-mac",
                "platform": "macos",
                "client_version": "0.0.1",
            },
        },
    )
    assert admin_desktop_login.status_code == 200, admin_desktop_login.text
    admin_desktop = auth_headers(admin_desktop_login.json())
    foreign = app_client.get(
        f"/api/v1/sessions/{session['session_id']}", headers=admin_desktop
    )
    assert foreign.status_code == 404
    assert foreign.json()["error"]["code"] == "NOT_FOUND"
    listed = app_client.get("/api/admin/users", headers=admin).json()
    raw = str(listed)
    assert "refined_text" not in raw and "raw_text" not in raw

    owner = auth_headers(user)
    created = app_client.post("/api/v1/dictionary/terms",
        json={"term": "React", "aliases": ["ree act"], "scope": "default"},
        headers={**owner, "Idempotency-Key": key()})
    assert created.status_code == 201
    foreign_term = created.json()
    own_created = app_client.post("/api/v1/dictionary/terms",
        json={"term": "Docker", "aliases": ["dock er"], "scope": "default"},
        headers={**admin_desktop, "Idempotency-Key": key()})
    assert own_created.status_code == 201
    own_term = own_created.json()
    owner_id, admin_id = uid(user), uid(admin_desktop_login.json())

    def dictionary_state_for(user_id):
        return tuple(fetch_one(
            f"SELECT md5(COALESCE(jsonb_agg(to_jsonb(t) ORDER BY {order})::text,'[]')) FROM {table} t WHERE user_id=$1",
            user_id,
        ) for table, order in (("dictionary_terms", "id"),
                               ("dictionary_state", "user_id"), ("operation_requests", "id")))

    before_owner, before_admin = dictionary_state_for(owner_id), dictionary_state_for(admin_id)
    owner_page = app_client.get("/api/v1/dictionary/terms", headers=owner)
    assert owner_page.status_code == 200
    admin_page = app_client.get("/api/v1/dictionary/terms", headers=admin_desktop)
    assert admin_page.status_code == 200
    assert [term["id"] for term in admin_page.json()["items"]] == [own_term["id"]]
    assert foreign_term["id"] not in [term["id"] for term in admin_page.json()["items"]]

    # Both paths exist and their bodies/versions are valid. A missing GET
    # /terms/{id} route would return a meaningless 404, so do not use it here.
    forbidden_patch_key, forbidden_delete_key = key(), key()
    patched = app_client.patch(f"/api/v1/dictionary/terms/{foreign_term['id']}",
        json={"version": foreign_term["version"], "term": "ReactUpdated", "aliases": ["react-edited"]},
        headers={**admin_desktop, "Idempotency-Key": forbidden_patch_key})
    assert patched.status_code == 404 and patched.json()["error"]["code"] == "NOT_FOUND"
    assert dictionary_state_for(owner_id) == before_owner
    assert dictionary_state_for(admin_id) == before_admin
    deleted = app_client.delete(f"/api/v1/dictionary/terms/{foreign_term['id']}",
        params={"version": foreign_term["version"]},
        headers={**admin_desktop, "Idempotency-Key": forbidden_delete_key})
    assert deleted.status_code == 404 and deleted.json()["error"]["code"] == "NOT_FOUND"
    assert dictionary_state_for(owner_id) == before_owner
    assert dictionary_state_for(admin_id) == before_admin
    for rejected_key in (forbidden_patch_key, forbidden_delete_key):
        assert fetch_one("SELECT count(*) FROM operation_requests WHERE user_id=$1 AND request_key=$2",
                         admin_id, rejected_key) == 0
    assert app_client.get("/api/v1/dictionary/terms", headers=owner).json() == owner_page.json()

    # Positive control: the exact same real routes work for the administrator's
    # own tenant, with version increments and actual read-back of both effects.
    edited = app_client.patch(f"/api/v1/dictionary/terms/{own_term['id']}",
        json={"version": own_term["version"], "aliases": ["docker-cli"]},
        headers={**admin_desktop, "Idempotency-Key": key()})
    assert edited.status_code == 200
    assert edited.json()["version"] == own_term["version"] + 1
    assert edited.json()["dictionary_version"] == own_term["dictionary_version"] + 1
    own_page = app_client.get("/api/v1/dictionary/terms", headers=admin_desktop)
    assert own_page.status_code == 200
    assert own_page.json()["items"][0]["id"] == own_term["id"]
    assert own_page.json()["items"][0]["aliases"] == ["docker-cli"]
    removed = app_client.delete(f"/api/v1/dictionary/terms/{own_term['id']}",
        params={"version": edited.json()["version"]},
        headers={**admin_desktop, "Idempotency-Key": key()})
    assert removed.status_code == 200 and removed.json()["deleted"] is True
    assert removed.json()["version"] == edited.json()["version"] + 1
    assert removed.json()["dictionary_version"] == edited.json()["dictionary_version"] + 1
    assert app_client.get("/api/v1/dictionary/terms", headers=admin_desktop).json()["items"] == []
    assert fetch_one("SELECT deleted_at IS NOT NULL FROM dictionary_terms WHERE user_id=$1 AND id=$2",
                     admin_id, own_term["id"]) is True
    assert dictionary_state_for(owner_id) == before_owner
    assert app_client.get("/api/v1/dictionary/terms", headers=owner).json() == owner_page.json()
