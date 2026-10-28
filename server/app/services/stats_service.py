from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import SQLAlchemyError

from ..models import LearningEvent, SessionContribution, UsageDaily, User, VoiceSession
from . import dictionary_service as svc

TZ = ZoneInfo("Asia/Shanghai")


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def business_date(value: dt.datetime) -> dt.date:
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(TZ).date()


USAGE_FIELDS = (
    "sessions", "received_audio_samples", "result_sessions", "failed_sessions",
    "empty_sessions", "cancelled_sessions", "chars", "successful_audio_samples",
    "fallback_sessions", "inserted_sessions", "learning_events",
)


def _session_usage(row: SessionContribution | None) -> dict[str, int]:
    if row is None:
        return dict.fromkeys(USAGE_FIELDS, 0)
    return {
        "sessions": int(bool(row.counted_session)),
        "received_audio_samples": row.received_samples or 0,
        "result_sessions": int(bool(row.has_result)),
        "failed_sessions": int(row.outcome == "failed"),
        "empty_sessions": int(row.outcome == "empty"),
        "cancelled_sessions": int(row.outcome == "cancelled"),
        "chars": row.char_count or 0,
        "successful_audio_samples": row.successful_audio_samples or 0,
        "fallback_sessions": int(bool(row.fallback)),
        "inserted_sessions": int(bool(row.inserted)),
        "learning_events": 0,
    }


async def _lock_statistics_user(db: AsyncSession, user_id: str) -> None:
    # Disabled users still need recovery/backfill; this is a consistency lock,
    # not an authorization check. Public callers already authorize separately.
    with db.no_autoflush:
        owner = await db.scalar(select(User.id).where(User.id == user_id).with_for_update())
    if owner is None:
        raise ValueError("STATISTICS_USER_NOT_FOUND")


async def _add_usage_delta(db: AsyncSession, user_id: str, day: dt.date,
                           delta: dict[str, int]) -> None:
    # The caller owns User before Session/Event/Dictionary locks. Never commit
    # here: business state, permanent contribution and daily projection are one
    # transaction. Zero/replayed transitions do not touch the projection.
    values = {name: delta.get(name, 0) for name in USAGE_FIELDS}
    if not any(values.values()):
        return
    statement = insert(UsageDaily).values(user_id=user_id, date=day, **values)
    result = await db.execute(statement.on_conflict_do_update(
        index_elements=["user_id", "date"],
        set_={name: getattr(UsageDaily, name) + statement.excluded[name] for name in USAGE_FIELDS},
    ).returning(*(getattr(UsageDaily, name) for name in USAGE_FIELDS)))
    if any(value < 0 for value in result.one()):
        # A negative delta is valid; a negative final projection is not. Raise
        # inside the business transaction so the outer owner rolls back both.
        # SQLAlchemyError uses the existing safe 503 handler, with no new API.
        raise SQLAlchemyError("USAGE_PROJECTION_INCONSISTENT")


async def record_learning_applied(db: AsyncSession, event: LearningEvent,
                                  applied_at: dt.datetime) -> bool:
    """First application only; caller holds User -> Session -> Event locks.

    Both automatic application and explicit confirmation use this helper after
    the dictionary write. Undo preserves applied_at and never invokes a negative
    delta. A failed surrounding transaction rolls back timestamp and projection.
    """
    if event.applied_at is not None:
        return False
    event.applied_at = applied_at
    await _add_usage_delta(db, event.user_id, business_date(applied_at), {"learning_events": 1})
    return True


async def _rebuild_usage_day_locked(db: AsyncSession, user_id: str, day: dt.date) -> dict[str, int]:
    # User is the common fence for every contribution/application writer; no
    # inverse acquisition of Session/Event/Dictionary after a daily lock.
    rows = (await db.scalars(select(SessionContribution).where(
        SessionContribution.user_id == user_id, SessionContribution.business_date == day,
    ))).all()
    totals = dict.fromkeys(USAGE_FIELDS, 0)
    for row in rows:
        for name, value in _session_usage(row).items():
            totals[name] += value
    start = dt.datetime.combine(day, dt.time.min, tzinfo=TZ).astimezone(dt.timezone.utc)
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min, tzinfo=TZ).astimezone(dt.timezone.utc)
    totals["learning_events"] = int(await db.scalar(select(func.count(LearningEvent.id)).where(
        LearningEvent.user_id == user_id, LearningEvent.applied_at >= start,
        LearningEvent.applied_at < end,
    )) or 0)  # Include undone/content-cleared events; applied_at is permanent.
    if any(value < 0 for value in totals.values()):
        raise SQLAlchemyError("USAGE_PROJECTION_SOURCE_INVALID")
    statement = insert(UsageDaily).values(user_id=user_id, date=day, **totals)
    await db.execute(statement.on_conflict_do_update(
        index_elements=["user_id", "date"],
        set_={name: statement.excluded[name] for name in USAGE_FIELDS},
    ))
    return totals


async def rebuild_usage_daily(db: AsyncSession, user_id: str, day: dt.date) -> dict[str, int]:
    """Replace one user's one day from permanent sources; caller commits.

    This internal maintenance operation is idempotent and needs no HTTP route or
    background worker. Acquire it before any lower-level row locks.
    """
    if type(day) is not dt.date:
        raise ValueError("STATISTICS_DAY_REQUIRED")
    await _lock_statistics_user(db, user_id)
    return await _rebuild_usage_day_locked(db, user_id, day)


async def rebuild_all_usage_daily(database) -> int:
    """Before traffic AND session restart recovery, initialize legacy projections.

    Each user is a separate atomic transaction. Re-entry repairs the same rows,
    including stale days; partial startup failure leaves readiness false. No
    content is read and the model_usage_daily ledger is untouched.
    """
    async with database.session_factory() as db:
        users = list((await db.scalars(select(User.id).order_by(User.id))).all())
    rebuilt = 0
    for user_id in users:
        async with database.session_factory() as db:
            await _lock_statistics_user(db, user_id)
            days = set((await db.scalars(select(SessionContribution.business_date).where(
                SessionContribution.user_id == user_id,
            ).distinct())).all())
            days.update((await db.scalars(select(func.date(func.timezone("Asia/Shanghai", LearningEvent.applied_at))).where(
                LearningEvent.user_id == user_id, LearningEvent.applied_at.is_not(None),
            ).distinct())).all())
            days.update((await db.scalars(select(UsageDaily.date).where(UsageDaily.user_id == user_id))).all())
            for day in sorted(days):
                await _rebuild_usage_day_locked(db, user_id, day)
            await db.commit()
            rebuilt += len(days)
    return rebuilt


async def _contribution(
    db: AsyncSession, user_id: str, session_id: str
) -> SessionContribution | None:
    return await db.scalar(
        select(SessionContribution)
        .where(
            SessionContribution.user_id == user_id,
            SessionContribution.session_id == session_id,
        )
        .with_for_update()
    )


async def record_session(
    db: AsyncSession,
    session: VoiceSession,
    outcome: str,
    *,
    has_result: bool = False,
    char_count: int = 0,
    fallback: bool = False,
) -> SessionContribution:
    """Upsert the per-session contribution used by every statistics query."""
    await _lock_statistics_user(db, session.user_id)
    await db.scalar(select(VoiceSession.id).where(
        VoiceSession.user_id == session.user_id, VoiceSession.id == session.id
    ).with_for_update())
    row = await _contribution(db, session.user_id, session.id)
    before = _session_usage(row)
    if row is None:
        row = SessionContribution(
            user_id=session.user_id,
            session_id=session.id,
            business_date=business_date(session.created_at),
        )
        db.add(row)
    row.received_samples = session.received_samples
    row.outcome = outcome
    row.has_result = has_result
    row.char_count = char_count
    row.fallback = fallback
    row.successful_audio_samples = session.received_samples if has_result else 0
    row.counted_session = session.received_samples > 0
    row.revision = (row.revision or 0) + 1
    after = _session_usage(row)
    await _add_usage_delta(db, session.user_id, row.business_date,
                           {name: after[name] - before[name] for name in USAGE_FIELDS})
    return row


async def record_inserted(db: AsyncSession, user_id: str, session_id: str) -> None:
    await _lock_statistics_user(db, user_id)
    row = await _contribution(db, user_id, session_id)
    if row is not None and not row.inserted:
        row.inserted = True
        row.revision = (row.revision or 0) + 1
        await _add_usage_delta(db, user_id, row.business_date, {"inserted_sessions": 1})


async def stats_me(
    db: AsyncSession,
    user_id: str,
    date_from: dt.date,
    date_to: dt.date,
) -> dict:
    rows = (
        await db.scalars(
            select(SessionContribution).where(
                SessionContribution.user_id == user_id,
                SessionContribution.business_date >= date_from,
                SessionContribution.business_date <= date_to,
            )
        )
    ).all()
    totals = {
        "sessions": sum(1 for row in rows if row.counted_session),
        "received_audio_ms": sum(row.received_samples for row in rows) * 1000 // 16000,
        "result_sessions": sum(1 for row in rows if row.has_result),
        "failed_sessions": sum(1 for row in rows if row.outcome == "failed"),
        "empty_sessions": sum(1 for row in rows if row.outcome == "empty"),
        "cancelled_sessions": sum(1 for row in rows if row.outcome == "cancelled"),
        "chars": sum(row.char_count for row in rows),
        "successful_audio_ms": sum(row.successful_audio_samples for row in rows) * 1000 // 16000,
        "fallback_sessions": sum(1 for row in rows if row.fallback),
        "inserted_sessions": sum(1 for row in rows if row.inserted),
    }
    learning_events = int(
        await db.scalar(
            select(func.count(LearningEvent.id)).where(
                LearningEvent.user_id == user_id,
                LearningEvent.applied_at.is_not(None),
                LearningEvent.applied_at
                >= dt.datetime.combine(date_from, dt.time.min, tzinfo=TZ).astimezone(
                    dt.timezone.utc
                ),
                LearningEvent.applied_at
                < dt.datetime.combine(
                    date_to + dt.timedelta(days=1), dt.time.min, tzinfo=TZ
                ).astimezone(dt.timezone.utc),
            )
        )
        or 0
    )
    totals["learning_events"] = learning_events
    totals["average_chars_per_minute"] = round(
        totals["chars"] / (totals["successful_audio_ms"] / 60_000), 1
    ) if totals["successful_audio_ms"] else 0.0
    processed_sessions = sum(1 for row in rows if row.outcome in ("ready", "failed", "empty"))
    totals["processing_success_rate"] = round(
        totals["result_sessions"] / processed_sessions, 4
    ) if processed_sessions else 0.0
    totals["insertion_success_rate"] = round(
        totals["inserted_sessions"] / totals["result_sessions"], 4
    ) if totals["result_sessions"] else 0.0
    totals["fallback_rate"] = round(
        totals["fallback_sessions"] / totals["result_sessions"], 4
    ) if totals["result_sessions"] else 0.0

    all_dates = {
        row.business_date
        for row in (
            await db.scalars(
                select(SessionContribution).where(
                    SessionContribution.user_id == user_id,
                    SessionContribution.counted_session.is_(True),
                )
            )
        ).all()
    }
    per_day: dict[dt.date, dict[str, int]] = {}
    for row in rows:
        if not row.counted_session:
            continue
        bucket = per_day.setdefault(
            row.business_date,
            {"sessions": 0, "chars": 0, "successful_audio_samples": 0},
        )
        bucket["sessions"] += 1
        bucket["chars"] += row.char_count
        bucket["successful_audio_samples"] += row.successful_audio_samples
    daily = []
    day = date_from
    while day <= date_to:
        bucket = per_day.get(day)
        sessions = bucket["sessions"] if bucket else 0
        daily.append(
            {
                "date": day.isoformat(),
                "sessions": sessions,
                "chars": bucket["chars"] if bucket else 0,
                "successful_audio_ms": (
                    bucket["successful_audio_samples"] * 1000 // 16000
                    if bucket
                    else 0
                ),
                "heat_level": 0
                if sessions == 0
                else 1
                if sessions == 1
                else 2
                if sessions <= 4
                else 3
                if sessions <= 9
                else 4,
            }
        )
        day += dt.timedelta(days=1)

    today = business_date(utcnow())
    current = 0
    cursor_day = today if today in all_dates else today - dt.timedelta(days=1)
    while cursor_day in all_dates:
        current += 1
        cursor_day -= dt.timedelta(days=1)
    longest = 0
    run = 0
    previous = None
    for day in sorted(all_dates):
        if previous is not None and day == previous + dt.timedelta(days=1):
            run += 1
        else:
            run = 1
        longest = max(longest, run)
        previous = day

    return {
        "timezone": "Asia/Shanghai",
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "totals": totals,
        "active_days_in_range": len(
            {row.business_date for row in rows if row.counted_session}
        ),
        "current_streak_days": current,
        "longest_streak_days": longest,
        "dictionary_terms": await svc.active_count(db, user_id),
        "cumulative_learning_events": int(
            await db.scalar(
                select(func.count(LearningEvent.id)).where(
                    LearningEvent.user_id == user_id,
                    LearningEvent.applied_at.is_not(None),
                )
            )
            or 0
        ),
        "daily": daily,
    }
