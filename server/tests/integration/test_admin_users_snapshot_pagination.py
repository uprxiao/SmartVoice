"""F-PAGE user list: real admin creation/API/PG, deterministic fixture IDs/times.

No user is SQL-inserted. Only the admin module's UUID source and this test's
already-created users' created_at values are controlled. No GUI claim.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from uuid import UUID

from sqlalchemy import func, select, update

from app.api import admin as admin_module
from app.models import (
    CapacityChange, DictionaryState, OperationRequest, RuntimeCapacity,
    SessionAttempt, User, VoiceSession,
)
from tests.conftest import auth_headers
from tests.ids import fixture_uuid
from tests.test_admin import login_admin


QUERY = "f-page-users-"
PASSWORD = "FPageFixture123!"
BASE = datetime(2026, 10, 1, tzinfo=timezone.utc)
# This is a fixed independently enumerated oracle, NOT sorted from API/SQL rows.
# Adjacent triples share created_at; older triple groups follow newer ones;
# equal timestamps require UUID DESC. The last group has two records.
EXPECTED_NUMBERS = (
    3, 2, 1, 6, 5, 4, 9, 8, 7, 12, 11, 10, 15, 14, 13, 18, 17, 16,
    21, 20, 19, 24, 23, 22, 27, 26, 25, 30, 29, 28, 33, 32, 31,
    36, 35, 34, 39, 38, 37, 42, 41, 40, 45, 44, 43, 48, 47, 46,
    51, 50, 49, 53, 52,
)


def user_id(number):
    return f"40000000-0000-4000-8000-{number:012d}"


def email(number):
    return f"{QUERY}{number:03d}@example.invalid"


def at(number):
    return BASE + timedelta(minutes=17 - (number - 1) // 3)


async def arrange_times(app):
    async with app.state.db.session_factory() as db:
        for number in range(1, 54):
            result = await db.execute(update(User).where(
                User.id == user_id(number), User.email == email(number),
                User.role == "user").values(created_at=at(number)))
            assert result.rowcount == 1
        await db.commit()


async def database_fingerprint(app):
    async with app.state.db.session_factory() as db:
        users = (await db.execute(select(User.id, User.email, User.created_at,
            User.role, User.status).where(User.email.startswith(QUERY)).order_by(User.id))).all()
        # Select no password/login credentials. Full counts make list reads and
        # same-key replay unable to hide an accidental new business row.
        counts = {model.__tablename__: int(await db.scalar(select(func.count()).select_from(model)))
                  for model in (User, DictionaryState, OperationRequest, VoiceSession,
                                SessionAttempt, CapacityChange)}
        capacity = await db.get(RuntimeCapacity, 1)
        return {"users": [tuple(row) for row in users], "counts": counts,
            "capacity": (capacity.id, capacity.version, capacity.max_active_sessions)}


def test_tc_admin_04_f_page_user_snapshot_and_uuid_ties(app_client, monkeypatch, record_property):
    admin = auth_headers(login_admin(app_client))
    target = {"id": None}

    def fixed_uuid():
        assert target["id"] is not None
        return UUID(target["id"])

    # Replace this module's reference, NOT global uuid.uuid4. This leaves JWTs,
    # device/login IDs, request IDs and idempotency implementation untouched.
    monkeypatch.setattr(admin_module, "uuid", SimpleNamespace(uuid4=fixed_uuid))

    def create(number):
        target["id"] = user_id(number)
        response = app_client.post("/api/admin/users", headers=admin | {
            "Idempotency-Key": fixture_uuid(f"admin04-f-page-create-{number}")},
            json={"email": email(number), "password": PASSWORD})
        return response

    for number in range(1, 54):
        response = create(number)
        assert response.status_code == 201
        assert response.json()["replayed"] is False
        assert response.json()["user"]["id"] == user_id(number)
        assert response.json()["user"]["email"] == email(number)
        assert response.json()["user"]["role"] == "user"
    app_client.portal.call(arrange_times, app_client.app)
    before = app_client.portal.call(database_fingerprint, app_client.app)
    assert len(before["users"]) == 53
    assert before["counts"]["users"] == before["counts"]["dictionary_state"] == 54
    assert before["counts"]["operation_requests"] == 53
    assert before["counts"]["sessions"] == before["counts"]["session_attempts"] == 0
    assert before["counts"]["capacity_changes"] == 0

    def page(cursor=None):
        params = {"limit": 20, "q": QUERY}
        if cursor is not None:
            params["cursor"] = cursor
        response = app_client.get("/api/admin/users", headers=admin, params=params)
        assert response.status_code == 200
        body = response.json()
        assert set(body) == {"items", "next_cursor"}
        for item in body["items"]:
            assert item["totals"] == {"sessions": 0, "received_audio_ms": 0,
                "result_sessions": 0, "last_session_at": None}
            assert not ({"password", "password_hash", "access_token", "refresh_token",
                "raw_text", "refined_text"} & set(item["user"]))
        return body

    expected = [user_id(number) for number in EXPECTED_NUMBERS]
    first = page()
    assert [item["user"]["id"] for item in first["items"]] == expected[:20]
    assert first["next_cursor"] is not None
    assert app_client.portal.call(database_fingerprint, app_client.app) == before

    # A genuinely new API-created record after page1 has a newer server-created
    # timestamp. No backdating or SQL INSERT is used for this positive control.
    inserted = create(54)
    assert inserted.status_code == 201 and inserted.json()["replayed"] is False
    assert inserted.json()["user"]["id"] == user_id(54)
    assert datetime.fromisoformat(inserted.json()["user"]["created_at"].replace("Z", "+00:00")) > at(1)
    after_insert = app_client.portal.call(database_fingerprint, app_client.app)
    assert len(after_insert["users"]) == 54
    assert after_insert["counts"]["users"] == after_insert["counts"]["dictionary_state"] == 55
    assert after_insert["counts"]["operation_requests"] == 54
    assert after_insert["capacity"] == before["capacity"]

    second = page(first["next_cursor"])
    assert [item["user"]["id"] for item in second["items"]] == expected[20:40]
    assert second["next_cursor"] is not None
    third = page(second["next_cursor"])
    assert [item["user"]["id"] for item in third["items"]] == expected[40:]
    assert third["next_cursor"] is None
    pages = (first, second, third)
    old_ids = [item["user"]["id"] for body in pages for item in body["items"]]
    assert old_ids == expected and len(set(old_ids)) == 53 and user_id(54) not in old_ids
    assert [len(body["items"]) for body in pages] == [20, 20, 13]
    assert page(first["next_cursor"]) == second  # old cursor replay stays read-only
    refreshed = page()
    assert [item["user"]["id"] for item in refreshed["items"]] == [user_id(54), *expected[:19]]
    assert app_client.portal.call(database_fingerprint, app_client.app) == after_insert

    # Formal create-key replay must not add a second user or consume capacity.
    replay = create(54)
    assert replay.status_code == 200 and replay.json()["replayed"] is True
    assert replay.json()["user"]["id"] == user_id(54)
    assert app_client.portal.call(database_fingerprint, app_client.app) == after_insert
    login = app_client.post("/api/auth/login", json={"email": email(54), "password": PASSWORD,
        "client_type": "desktop", "device": {
            "installation_id": fixture_uuid("admin04-f-page-positive-device"),
            "name": "F-PAGE fixture", "platform": "macos", "client_version": "0.0.1"}})
    assert login.status_code == 200 and login.json()["user"]["id"] == user_id(54)
    assert app_client.portal.call(database_fingerprint, app_client.app) == after_insert
    record_property("admin04_f_page", json.dumps({
        "created_via_admin_api": 54, "initial_snapshot_rows": 53,
        "page_sizes": [20, 20, 13], "expected_order_numbers": EXPECTED_NUMBERS,
        "newer_row_absent_from_old_pages": True, "newer_row_visible_after_refresh": True,
        "same_key_create_replayed_once": True, "business_counts_and_capacity_unchanged_by_reads": True,
        "cursor_contract": "opaque-black-box-no-new-as_of-field", "gui_executed": False,
    }, sort_keys=True))
