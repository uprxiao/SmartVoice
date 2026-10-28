"""Application PG connections use UTC; independent DB display zone may differ.

Read one normally registered user's stored instant through both connections.
No SET TIME ZONE, timestamp seed, clock / provider patch or statistics write.
"""
from __future__ import annotations

import json
from zoneinfo import ZoneInfo

from sqlalchemy import text

from tests.cases.test_dict_cases import fetch_one
from tests.conftest import register_and_login
from tests.database_instants import database_instant
from tests.integration.test_insertion_device_role_matrix import business_snapshot


def test_application_database_connection_timezone_is_utc(app_client, record_property):
    tokens = register_and_login(app_client, email="database-timezone@example.invalid")
    owner = tokens["user"]["id"]
    before = business_snapshot(owner)

    async def application_observation():
        async with app_client.app.state.db.session_factory() as db:
            timezone = await db.scalar(text("SHOW TimeZone"))
            row = (await db.execute(text(
                "SELECT created_at, (created_at AT TIME ZONE 'Asia/Shanghai')::date AS business_date "
                "FROM users WHERE id=:owner"
            ), {"owner": owner})).mappings().one()
            return timezone, row["created_at"], row["business_date"]

    application_timezone, application_created_at, application_business_date = (
        app_client.portal.call(application_observation)
    )
    independent = json.loads(fetch_one(
        "SELECT json_build_object('timezone', current_setting('TimeZone'), "
        "'created_at', created_at, "
        "'business_date', (created_at AT TIME ZONE 'Asia/Shanghai')::date)::text "
        "FROM users WHERE id=$1", owner,
    ))
    assert isinstance(application_timezone, str)
    assert isinstance(independent["timezone"], str)
    assert application_created_at.tzinfo is not None
    assert application_created_at.utcoffset() is not None
    instant = database_instant(independent["created_at"])
    assert application_created_at == instant
    assert application_business_date == instant.astimezone(ZoneInfo("Asia/Shanghai")).date()
    assert independent["business_date"] == application_business_date.isoformat()
    assert business_snapshot(owner) == before
    record_property("database_timezone_contract", json.dumps({
        "application_timezone": application_timezone,
        "independent_timezone": independent["timezone"],
        "application_created_at": application_created_at.isoformat(),
        "independent_created_at": independent["created_at"],
        "same_exact_stored_instant": True,
        "business_timezone": "Asia/Shanghai",
        "business_date": application_business_date.isoformat(),
        "business_snapshot_unchanged": True,
        "scope": "actual application SQLAlchemy connection and independent PostgreSQL read",
    }, sort_keys=True))
    # Only the application's own connection is contractually UTC. The separate
    # observer keeps the server default, including a supported non-UTC display.
    assert application_timezone == "UTC"
