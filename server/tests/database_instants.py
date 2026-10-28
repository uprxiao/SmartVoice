"""Exact UTC instants from independent PostgreSQL JSON snapshots, not API wire.

PostgreSQL renders timestamptz JSON in that connection's display timezone.
Wire-format tests must keep their separate strict UTC / trailing-Z checks.
"""
from __future__ import annotations

import datetime as dt


def database_instant(value: str) -> dt.datetime:
    assert isinstance(value, str), "database timestamp must be a string"
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None and parsed.utcoffset() is not None, (
        "database timestamp must include a timezone"
    )
    return parsed.astimezone(dt.timezone.utc)
