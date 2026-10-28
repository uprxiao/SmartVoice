from __future__ import annotations

import datetime as dt

from ..models import User, VoiceSession


def content_expired(
    session: VoiceSession, user: User | None, now: dt.datetime | None = None
) -> bool:
    """Retention applies from the first terminal time, not from retries."""
    if session.content_cleared_at is not None:
        return True
    days = user.history_retention_days if user is not None else None
    if not days or session.first_terminal_at is None:
        return False
    reference = now or dt.datetime.now(dt.timezone.utc)
    first = session.first_terminal_at
    if first.tzinfo is None:
        first = first.replace(tzinfo=dt.timezone.utc)
    return first + dt.timedelta(days=days) <= reference


def content_expires_at(session: VoiceSession, user: User | None) -> dt.datetime | None:
    days = user.history_retention_days if user is not None else None
    if not days or session.first_terminal_at is None:
        return None
    first = session.first_terminal_at
    if first.tzinfo is None:
        first = first.replace(tzinfo=dt.timezone.utc)
    return first + dt.timedelta(days=days)
