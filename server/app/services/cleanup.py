from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time

from sqlalchemy import null, select, update

from ..models import LearningEvent, SessionAttempt, SessionResult, User, VoiceSession
from .recovery_snapshot import RecoveryIdentity

logger = logging.getLogger("smartvoiceinput.cleanup")


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


async def mark_restarted_pending(app, *, identities=None) -> None:
    """Pending classifications cannot survive a restart; mark them failed."""
    if identities is None:
        async with app.state.db.session_factory() as db:
            identities = tuple(RecoveryIdentity(*row) for row in (await db.execute(
                select(LearningEvent.user_id, LearningEvent.id).where(LearningEvent.status == "pending")
            )).all())
    by_user = {}
    for identity in sorted(set(identities)):
        by_user.setdefault(identity.user_id, []).append(identity)
    for user_id, selected in sorted(by_user.items()):
        async with app.state.db.session_factory() as db:
            if await db.scalar(select(User.id).where(User.id == user_id).with_for_update()) is None:
                continue
            for identity in selected:
                # Exact tenant + frozen ID, with current pending CAS. Do not
                # enumerate a successor's newly created events for this user.
                await db.execute(update(LearningEvent).where(
                    LearningEvent.user_id == user_id, LearningEvent.id == identity.resource_id,
                    LearningEvent.status == "pending",
                ).values(status="failed", reason="SERVER_RESTARTED"))
            await db.commit()


async def _cleanup_user(db, user: User, now: dt.datetime) -> int:
    cleared = 0
    cutoff = now - dt.timedelta(days=user.history_retention_days or 0)
    sessions = (
        await db.scalars(
            select(VoiceSession).where(
                VoiceSession.user_id == user.id,
                VoiceSession.content_cleared_at.is_(None),
                VoiceSession.first_terminal_at.is_not(None),
                VoiceSession.first_terminal_at <= cutoff,
            ).order_by(VoiceSession.id).with_for_update()
        )
    ).all()
    for session in sessions:
        session.content_cleared_at = now
        session.asr_hints = null()
        session.llm_hints = null()
        await db.execute(update(SessionAttempt).where(
            SessionAttempt.user_id == user.id,
            SessionAttempt.session_id == session.id,
        ).values(raw_text=None))
        results = (
            await db.scalars(
                select(SessionResult).where(
                    SessionResult.user_id == user.id,
                    SessionResult.session_id == session.id,
                    SessionResult.content_cleared_at.is_(None),
                )
            )
        ).all()
        for result in results:
            result.content_cleared_at = now
            result.raw_text = None
            result.refined_text = None
        events = (
            await db.scalars(
                select(LearningEvent).where(
                    LearningEvent.user_id == user.id,
                    LearningEvent.session_id == session.id,
                    LearningEvent.content_cleared_at.is_(None),
                )
            )
        ).all()
        for event in events:
            event.content_cleared_at = now
            event.before_text = None
            event.after_text = None
            event.left_context = None
            event.right_context = None
            event.term_before = null()
            event.evidence = null()
            if event.status in ("pending", "suggested"):
                event.status = "failed"
                event.reason = "CONTENT_EXPIRED"
        cleared += 1
    return cleared


async def cleanup_history(app) -> int:
    """Clear content past each user's retention window; keep aggregates."""
    cleared = 0
    async with app.state.db.session_factory() as db:
        user_ids = list((await db.scalars(
            select(User.id).where(User.history_retention_days.is_not(None))
        )).all())
    # One user per transaction keeps lock duration bounded and preserves the
    # same User -> Session -> LearningEvent order as learning publication.
    for user_id in user_ids:
        cleared += await cleanup_user(app, user_id)
    return cleared


async def cleanup_user(app, user_id: str) -> int:
    """Apply a shortened retention policy immediately for one user."""
    async with app.state.db.session_factory() as db:
        user = await db.scalar(select(User).where(User.id == user_id).with_for_update())
        if user is None or user.history_retention_days is None:
            return 0
        cleared = await _cleanup_user(db, user, utcnow())
        await db.commit()
        return cleared


async def cleanup_retry_audio(app) -> int:
    sessions = getattr(app.state, "sessions", None)
    if sessions is None:
        return 0
    # SessionManager owns every PCM copy, retry task and reservation. Clearing
    # only runtime.audio here leaves decoder working copies and stale DB state.
    return await sessions.expire_retry_audio(utcnow().timestamp())


async def cleanup_connect_timeouts(app) -> int:
    sessions = getattr(app.state, "sessions", None)
    if sessions is None:
        return 0
    timeout = app.state.config.sessions.connect_timeout_ms / 1000
    now = time.monotonic()
    expired = 0
    for runtime in list(sessions.runtimes.values()):
        if (
            not runtime.connected
            and not runtime.cancelled
            and runtime.state == "created"
            and now - runtime.created_at > timeout
        ):
            await sessions._fail(runtime, "CONNECT_TIMEOUT")
            expired += 1
    return expired


async def cleanup_loop(app) -> None:
    interval = max(5, app.state.config.cleanup.history_interval_seconds)
    last_history_success: float | None = None
    last_failure_log: dict[str, float] = {}

    async def run_job(phase, job) -> bool:
        try:
            await job(app)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            now = asyncio.get_running_loop().time()
            previous = last_failure_log.get(phase)
            # Retry on the next bounded tick, but do not flood the log during
            # an outage. Never log exception text/tracebacks: SQL errors may
            # contain credentials or retained user content.
            if previous is None or now - previous >= 60:
                logger.warning(
                    "CLEANUP_JOB_FAILED phase=%s exception_type=%s",
                    phase,
                    type(error).__name__,
                    extra={"code": "CLEANUP_JOB_FAILED", "phase": phase,
                           "exception_type": type(error).__name__},
                )
                last_failure_log[phase] = now
            return False
        last_failure_log.pop(phase, None)
        return True

    while True:
        # At most one attempt per phase per tick, including repeated failures.
        # Keeping the existing one-second cadence avoids delaying PCM expiry.
        await asyncio.sleep(1)
        await run_job("connect_timeouts", cleanup_connect_timeouts)
        await run_job("retry_audio", cleanup_retry_audio)
        now = asyncio.get_running_loop().time()
        if last_history_success is None or now - last_history_success >= interval:
            if await run_job("history", cleanup_history):
                last_history_success = asyncio.get_running_loop().time()
