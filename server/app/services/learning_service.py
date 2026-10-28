from __future__ import annotations

import asyncio
import datetime as dt
import logging

from sqlalchemy import select

from ..api.deps import ApiError
from ..models import DictionaryTerm, Insertion, LearningEvent, SessionResult, User, VoiceSession
from ..schemas.learning import LearningEventOut
from . import dictionary_service as svc
from . import stats_service
from .idempotency import begin, lock_user
from .retention import content_expired
from .inference_context import CallScope, call_scope


class LearningError(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _snapshot(term: DictionaryTerm) -> dict:
    return {
        "term": term.term,
        "aliases": list(term.aliases or []),
        "contexts": list(term.contexts or []),
        "source": term.source,
        "version": term.version,
    }


def _context(before: str, after: str, left: str, right: str) -> dict:
    return {
        "alias_norm": svc.norm_of(before),
        "before": before[:80],
        "after": after[:80],
        "left": left[:80],
        "right": right[:80],
    }


async def _alias_owner(
    db, user_id: str, norm: str, exclude_id: str | None
) -> DictionaryTerm | None:
    rows = (
        await db.scalars(
            select(DictionaryTerm).where(
                DictionaryTerm.user_id == user_id,
                DictionaryTerm.deleted_at.is_(None),
            )
        )
    ).all()
    for row in rows:
        if row.id == exclude_id:
            continue
        if row.norm == norm:
            return row
        for alias in row.aliases or []:
            if isinstance(alias, str) and svc.norm_of(alias) == norm:
                return row
    return None


async def apply_term(
    db,
    user_id: str,
    before: str,
    after: str,
    left: str,
    right: str,
    expected_term_id: str | None = None,
    expected_term_version: int | None = None,
) -> tuple[DictionaryTerm, dict | None]:
    await svc.lock_state(db, user_id)
    before = svc.clean_text(before, "别名")
    after = svc.clean_text(after, "词条")
    before_norm = svc.norm_of(before)
    after_norm = svc.norm_of(after)
    existing = await svc.find_by_norm(db, user_id, after_norm)
    if expected_term_id is None:
        if existing is not None:
            raise LearningError("VERSION_CONFLICT")
    elif (
        existing is None
        or existing.id != expected_term_id
        or existing.version != expected_term_version
    ):
        raise LearningError("VERSION_CONFLICT")
    owner = await _alias_owner(
        db, user_id, before_norm, exclude_id=existing.id if existing else None
    )
    if owner is not None:
        raise LearningError("ALIAS_CONFLICT")
    target_owner = await _alias_owner(
        db, user_id, after_norm, exclude_id=existing.id if existing else None
    )
    if target_owner is not None:
        raise LearningError("ALIAS_CONFLICT")
    context = _context(before, after, left, right)
    if existing is not None:
        snapshot = _snapshot(existing)
        aliases = [a for a in (existing.aliases or []) if isinstance(a, str)]
        if before_norm != after_norm and not any(
            svc.norm_of(alias) == before_norm for alias in aliases
        ):
            if len(aliases) < 10:
                aliases.append(before)
        contexts = [
            item
            for item in (existing.contexts or [])
            if isinstance(item, dict)
            and not (
                item.get("alias_norm") == before_norm
                and item.get("left") == context["left"]
                and item.get("right") == context["right"]
            )
        ]
        contexts.append(context)
        existing.aliases = aliases
        existing.contexts = contexts[-3:]
        existing.source = "auto"
        existing.version += 1
        existing.updated_at = utcnow()
        await svc.bump_version(db, user_id)
        return existing, snapshot
    if await svc.active_count(db, user_id) >= svc.MAX_TERMS:
        raise LearningError("DICTIONARY_LIMIT")
    term = DictionaryTerm(
        user_id=user_id,
        term=after,
        norm=after_norm,
        aliases=[before] if before_norm != after_norm else [],
        contexts=[context],
        scope="default",
        source="auto",
        version=1,
    )
    db.add(term)
    await db.flush()
    await svc.bump_version(db, user_id)
    return term, None


async def _locked_event(db, event_id: str, user_id: str | None = None):
    """Lock publication context in User -> Session -> Event order."""
    query = select(LearningEvent).where(LearningEvent.id == event_id)
    if user_id is not None:
        query = query.where(LearningEvent.user_id == user_id)
    event = await db.scalar(query)
    if event is None:
        return None
    await db.scalar(select(User).where(User.id == event.user_id).with_for_update()
                    .execution_options(populate_existing=True))
    await db.scalar(select(VoiceSession).where(
        VoiceSession.user_id == event.user_id, VoiceSession.id == event.session_id,
    ).with_for_update().execution_options(populate_existing=True))
    return await db.scalar(query.with_for_update().execution_options(populate_existing=True))


async def _event_content_expired(db, event: LearningEvent) -> bool:
    user = await db.get(User, event.user_id)
    session = await db.scalar(select(VoiceSession).where(
        VoiceSession.user_id == event.user_id, VoiceSession.id == event.session_id,
    ))
    return bool(event.content_cleared_at is not None or session is None
                or content_expired(session, user))


async def _context_is_valid(db, event: LearningEvent) -> tuple[bool, str]:
    user = await db.get(User, event.user_id)
    if user is None or user.status != "active":
        return False, "ACCOUNT_DISABLED"
    if not user.learning_enabled:
        return False, "LEARNING_DISABLED"
    session = await db.scalar(select(VoiceSession).where(
        VoiceSession.user_id == event.user_id, VoiceSession.id == event.session_id,
    ))
    if session is None or not session.learning_enabled:
        return False, "LEARNING_DISABLED"
    if event.content_cleared_at is not None or content_expired(session, user):
        return False, "CONTENT_EXPIRED"
    insertion = await db.scalar(select(Insertion).where(
        Insertion.user_id == event.user_id, Insertion.session_id == event.session_id,
        Insertion.id == event.insertion_id,
    ))
    if insertion is None or insertion.status != "inserted" or not insertion.observable:
        return False, "LEARNING_NOT_ALLOWED"
    if session.current_result_id != insertion.result_id:
        return False, "CONTENT_EXPIRED"
    result = await db.scalar(select(SessionResult).where(
        SessionResult.user_id == event.user_id, SessionResult.session_id == event.session_id,
        SessionResult.id == insertion.result_id,
    ))
    if result is None or result.content_cleared_at is not None:
        return False, "CONTENT_EXPIRED"
    return True, ""


class _LearningStopping(Exception):
    pass


async def _fail_stopping_event(app, user_id: str | None, event_id: str):
    async with app.state.db.session_factory() as db:
        event = await _locked_event(db, event_id, user_id)
        if event is not None and event.status == "pending":
            event.status, event.reason = "failed", "SERVER_SHUTDOWN"
            await db.commit()


async def process_event(app, event_id: str, user_id: str | None = None) -> None:
    try:
        await _process_event(app, event_id, user_id)
    except _LearningStopping:
        # Raising out of the application transaction first rolls back any
        # dictionary changes made while shutdown began during an awaited lock.
        await _fail_stopping_event(app, user_id, event_id)


async def _process_event(app, event_id: str, user_id: str | None) -> None:
    async with app.state.db.session_factory() as db:
        event = await _locked_event(db, event_id, user_id)
        if event is None or event.status != "pending":
            return
        if getattr(app.state, "learning_closing", False):
            raise _LearningStopping
        ok, reason = await _context_is_valid(db, event)
        if not ok:
            event.status = "failed"
            event.reason = reason
            if getattr(app.state, "learning_closing", False):
                raise _LearningStopping
            await db.commit()
            return
        payload = {
            "before": event.before_text or "",
            "after": event.after_text or "",
            "left": event.left_context or "",
            "right": event.right_context or "",
        }
        result = await db.scalar(select(SessionResult).join(
            Insertion,
            (Insertion.user_id == SessionResult.user_id)
            & (Insertion.session_id == SessionResult.session_id)
            & (Insertion.result_id == SessionResult.id),
        ).where(Insertion.user_id == event.user_id,
                Insertion.session_id == event.session_id,
                Insertion.id == event.insertion_id))
        scope = CallScope(user_id=event.user_id, session_id=event.session_id,
                          attempt_no=result.attempt_no, learning_event_id=event.id)
    # No row locks survive the network/model call. Reacquire and recheck every
    # mutable precondition before publishing the result.
    if getattr(app.state, "learning_closing", False):
        raise _LearningStopping
    with call_scope(scope):
        classification = await app.state.classifier.classify(**payload)
    async with app.state.db.session_factory() as db:
        event = await _locked_event(db, event_id, user_id)
        if event is None or event.status != "pending":
            return
        if getattr(app.state, "learning_closing", False):
            raise _LearningStopping
        ok, reason = await _context_is_valid(db, event)
        if not ok:
            event.status = "failed"
            event.reason = reason
            if getattr(app.state, "learning_closing", False):
                raise _LearningStopping
            await db.commit()
            return
        if classification.reason_code:
            event.status = "failed"
            event.reason = classification.reason_code
            event.ambiguous = classification.ambiguous
            if getattr(app.state, "learning_closing", False):
                raise _LearningStopping
            await db.commit()
            return
        event.ambiguous = classification.ambiguous
        event.evidence = classification.evidence
        if (classification.decision == "term" and not classification.ambiguous
            and classification.category in ("proper_term", "spelling")):
            evidence = classification.evidence or {}
            if (
                type(evidence.get("before_start")) is not int or evidence.get("before_start") != 0
                or type(evidence.get("before_end")) is not int or evidence.get("before_end") != len(event.before_text or "")
                or str(evidence.get("before", "")) != (event.before_text or "")
                or str(evidence.get("after", "")) != (event.after_text or "")
            ):
                event.status = "failed"
                event.reason = "EVIDENCE_MISMATCH"
                if getattr(app.state, "learning_closing", False):
                    raise _LearningStopping
                await db.commit()
                return
            try:
                term, snapshot = await apply_term(
                    db,
                    event.user_id,
                    event.before_text or "",
                    event.after_text or "",
                    event.left_context or "",
                    event.right_context or "",
                    event.expected_term_id,
                    event.expected_term_version,
                )
            except (LearningError, ApiError) as exc:
                event.status = "failed"
                event.reason = exc.reason if isinstance(exc, LearningError) else "INVALID_TERM"
            else:
                event.status = "learned"
                event.classification = "term"
                event.term_id = term.id
                event.after_version = term.version
                event.term_before = snapshot
                await stats_service.record_learning_applied(db, event, utcnow())
                event.reason = None
        elif classification.decision in ("term", "suggest"):
            event.status = "suggested"
            event.classification = "suggest"
        else:
            event.status = "ignored"
            event.classification = "ignore"
        if getattr(app.state, "learning_closing", False):
            raise _LearningStopping
        await db.commit()


async def event_out(db, event: LearningEvent, *, replayed: bool | None = None) -> dict:
    term = None
    term_row = None
    if event.term_id:
        term_row = await db.scalar(select(DictionaryTerm).where(
            DictionaryTerm.user_id == event.user_id, DictionaryTerm.id == event.term_id,
        ))
        if term_row is not None:
            state = await svc.get_state(db, event.user_id)
            index = await svc.ambiguity_index(db, event.user_id)
            term = svc.term_out(term_row, state.version, index)
    undo_available = bool(
        event.status == "learned"
        and event.undone_at is None
        and term_row is not None
        and term_row.deleted_at is None
        and event.after_version is not None
        and term_row.version == event.after_version
        and not await _event_content_expired(db, event)
    )
    body = LearningEventOut(
        event_id=event.id,
        correction_id=event.correction_id,
        status=event.status,
        reason=event.reason,
        classification=event.classification,
        ambiguous=event.ambiguous,
        term=term,
        term_id=event.term_id,
        after_version=event.after_version,
        undone=event.undone_at is not None,
        undo_available=undo_available,
        created_at=event.created_at,
    ).model_dump(mode="json")
    if replayed is not None:
        body["replayed"] = replayed
    return body


async def confirm_event(
    app, event_id: str, user_id: str, idempotency_key: str
) -> dict:
    secret = app.state.config.idempotency_secret
    async with app.state.db.session_factory() as db:
        await lock_user(db, user_id)
        event = await _locked_event(db, event_id, user_id)
        if event is None:
            raise ApiError(404, "NOT_FOUND", "学习事件不存在")
        replayed, _ = await begin(
            db,
            secret,
            user_id,
            "learning.confirm",
            idempotency_key,
            {"event_id": event_id},
            "learning_event",
            event_id,
        )
        if replayed:
            return await event_out(db, event, replayed=replayed)
        if event.status == "learned" and event.classification == "term":
            await db.commit()  # Persist this operation key even for a no-op.
            return await event_out(db, event, replayed=replayed)
        if event.status != "suggested":
            raise ApiError(409, "LEARNING_CONFLICT", "该建议已不可确认")
        ok, reason = await _context_is_valid(db, event)
        if not ok:
            raise ApiError(409, "LEARNING_CONFLICT", "学习条件已变化", details={"reason": reason})
        try:
            term, snapshot = await apply_term(
                db,
                user_id,
                event.before_text or "",
                event.after_text or "",
                event.left_context or "",
                event.right_context or "",
                event.expected_term_id,
                event.expected_term_version,
            )
        except LearningError as exc:
            raise ApiError(
                409, "LEARNING_CONFLICT", "词条冲突", details={"reason": exc.reason}
            ) from exc
        event.status = "learned"
        event.classification = "term"
        event.term_id = term.id
        event.after_version = term.version
        event.term_before = snapshot
        await stats_service.record_learning_applied(db, event, utcnow())
        event.reason = None
        await db.commit()
        return await event_out(db, event, replayed=replayed)


async def undo_event(
    app, event_id: str, user_id: str, idempotency_key: str
) -> dict:
    secret = app.state.config.idempotency_secret
    async with app.state.db.session_factory() as db:
        await lock_user(db, user_id)
        event = await _locked_event(db, event_id, user_id)
        if event is None:
            raise ApiError(404, "NOT_FOUND", "学习事件不存在")
        replayed, _ = await begin(
            db,
            secret,
            user_id,
            "learning.undo",
            idempotency_key,
            {"event_id": event_id},
            "learning_event",
            event_id,
        )
        state = await svc.lock_state(db, user_id)
        if replayed or event.undone_at is not None:
            await db.commit()
            term_row = await db.scalar(select(DictionaryTerm).where(
            DictionaryTerm.user_id == event.user_id, DictionaryTerm.id == event.term_id,
        )) if event.term_id else None
            term = None
            if term_row is not None:
                index = await svc.ambiguity_index(db, user_id)
                term = svc.term_out(term_row, state.version, index)
            return {
                "event_id": event.id,
                "undone": True,
                "replayed": replayed,
                "term": term,
                "dictionary_version": state.version,
            }
        if event.status != "learned" or event.classification != "term":
            raise ApiError(409, "LEARNING_CONFLICT", "该事件不可撤销")
        if await _event_content_expired(db, event):
            raise ApiError(410, "CONTENT_EXPIRED", "学习记录已过期")
        term_row = await db.scalar(select(DictionaryTerm).where(
            DictionaryTerm.user_id == event.user_id, DictionaryTerm.id == event.term_id,
        )) if event.term_id else None
        if (
            term_row is None
            or event.after_version is None
            or term_row.version != event.after_version
        ):
            raise ApiError(
                409,
                "VERSION_CONFLICT",
                "词条已变化，无法撤销",
                details={"current_version": term_row.version if term_row else None},
            )
        snapshot = event.term_before
        if isinstance(snapshot, dict):
            term_row.term = str(snapshot.get("term", term_row.term))
            term_row.norm = svc.norm_of(term_row.term)
            term_row.aliases = [
                alias
                for alias in snapshot.get("aliases", [])
                if isinstance(alias, str)
            ]
            term_row.contexts = [
                item for item in snapshot.get("contexts", []) if isinstance(item, dict)
            ]
            term_row.source = str(snapshot.get("source", "manual"))
            term_row.deleted_at = None
        else:
            term_row.deleted_at = utcnow()
        term_row.version += 1
        term_row.updated_at = utcnow()
        dictionary_version = await svc.bump_version(db, user_id)
        event.undone_at = utcnow()
        await db.commit()
        term = None
        if term_row.deleted_at is None:
            index = await svc.ambiguity_index(db, user_id)
            term = svc.term_out(term_row, dictionary_version, index).model_dump(mode="json")
        return {
            "event_id": event.id,
            "undone": True,
            "replayed": False,
            "term": term,
            "dictionary_version": dictionary_version,
        }


def begin_shutdown(app, *, abort=False) -> None:
    state = app.state
    state.learning_closing = True
    jobs = getattr(state, "learning_jobs", {})
    pending = getattr(state, "shutdown_learning_events", None)
    if pending is None:
        pending = state.shutdown_learning_events = set()
    pending.update(jobs.values())
    if abort:
        state.learning_abort = True
        for task in tuple(jobs):
            if not task.done() and not task.cancelling():
                task.cancel()


def schedule(app, event_id: str, user_id: str) -> None:
    state = app.state
    if getattr(state, "learning_closing", False):
        # A correction accepted before the cutoff may finish committing while
        # shutdown is waiting for its HTTP request. Record its exact identity;
        # never start a new classifier or scan another instance's pending rows.
        state.shutdown_learning_events.add((user_id, event_id))
        return
    if not hasattr(state, "background_tasks"):
        state.background_tasks = set()
    if not hasattr(state, "learning_jobs"):
        state.learning_jobs = {}
    task = asyncio.create_task(process_event(app, event_id, user_id))
    state.background_tasks.add(task)
    state.learning_jobs[task] = (user_id, event_id)

    def finished(done):
        state.background_tasks.discard(done)
        state.learning_jobs.pop(done, None)
        if not done.cancelled() and done.exception() is not None:
            state.learning_cleanup_failed = True
            logging.getLogger(__name__).warning("Learning task incomplete: %s", type(done.exception()).__name__)

    task.add_done_callback(finished)


async def close(app, *, abort=False) -> None:
    begin_shutdown(app, abort=abort)
    state = app.state
    owner = getattr(state, "learning_close_task", None)
    if owner is None:
        owner = state.learning_close_task = asyncio.create_task(_close_owned(app))
    await asyncio.shield(owner)


async def _close_owned(app) -> None:
    state = app.state
    # HttpGuard's markers end when this request returns, not when its caller's
    # surrounding coroutine ends. A late accepted schedule is therefore owned.
    requests = tuple(getattr(state, "correction_requests", ()))
    if requests:
        await asyncio.gather(*requests)
    tasks = tuple(getattr(state, "background_tasks", ()))
    if tasks:
        # Normal shutdown lets the existing classifier finish within its own
        # original 10s budget. A real instance fence has already cancelled once.
        await asyncio.gather(*tasks, return_exceptions=True)
    failures = []
    for user_id, event_id in tuple(state.shutdown_learning_events):
        try:
            await _fail_stopping_event(app, user_id, event_id)
        except Exception as exc:
            failures.append(exc)
    state.shutdown_learning_events.clear()
    if failures or getattr(state, "learning_cleanup_failed", False):
        raise RuntimeError("LEARNING_SHUTDOWN_PERSISTENCE_INCOMPLETE")
