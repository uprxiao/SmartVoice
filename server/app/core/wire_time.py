"""Serialize explicitly owned outward datetime fields; never rewrite text."""
from datetime import datetime, timezone


def utc_iso8601(value: datetime) -> str:
    if not isinstance(value, datetime):
        raise TypeError("datetime required")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("aware datetime required")
    return value.astimezone(timezone.utc).isoformat().removesuffix("+00:00") + "Z"
