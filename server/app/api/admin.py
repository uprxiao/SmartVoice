from __future__ import annotations

import datetime as dt
import math
import uuid
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from sqlalchemy import Integer, case, cast, func, or_, select, union
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from ..core.security import hash_password, normalize_email, utcnow
from ..core.wire_time import utc_iso8601
from ..models import (
    DictionaryState,
    LearningEvent,
    LoginSession,
    ModelUsageDaily,
    RuntimeCapacity,
    SessionContribution,
    User,
    VoiceSession,
)
from ..models.session import ACTIVE_STATES
from ..services.capacity import CapacityError
from ..services.idempotency import require_key
from ..services.capacity_report import load_capacity_report
from ..services.pagination import clamp_limit, decode_cursor, encode_cursor
from .deps import ApiError, AuthContext, require_admin
from .auth import user_out, password_work, validate_email, validate_password

router = APIRouter(prefix="/api/admin", tags=["admin"])
TZ = ZoneInfo("Asia/Shanghai")


async def _aggregate_by_users(db, user_ids: list[str]) -> dict[str, dict]:
    if not user_ids:
        return {}
    rows = (
        await db.execute(
            select(
                SessionContribution.user_id,
                func.coalesce(func.sum(cast(SessionContribution.counted_session, Integer)), 0),
                func.coalesce(func.sum(SessionContribution.received_samples), 0),
                func.coalesce(
                    func.sum(
                        cast(SessionContribution.has_result, Integer)
                    ),
                    0,
                ),
            )
            .where(SessionContribution.user_id.in_(user_ids))
            .group_by(SessionContribution.user_id)
        )
    ).all()
    aggregated = {
        row[0]: {
            "sessions": int(row[1]),
            "received_audio_ms": int(row[2]) * 1000 // 16000,
            "result_sessions": int(row[3]),
            "last_session_at": None,
        }
        for row in rows
    }
    last_rows = (
        await db.execute(
            select(VoiceSession.user_id, func.max(VoiceSession.created_at))
            .where(VoiceSession.user_id.in_(user_ids))
            .group_by(VoiceSession.user_id)
        )
    ).all()
    for user_id, last_at in last_rows:
        aggregated.setdefault(
            user_id,
            {
                "sessions": 0,
                "received_audio_ms": 0,
                "result_sessions": 0,
                "last_session_at": None,
            },
        )["last_session_at"] = utc_iso8601(last_at) if last_at is not None else None
    return aggregated


@router.get("/users")
async def list_users(
    request: Request,
    q: str | None = Query(default=None, max_length=254),
    status: str | None = None,
    cursor: str | None = None,
    limit: int | None = None,
    ctx: AuthContext = Depends(require_admin),
):
    if status is not None and status not in ("active", "disabled"):
        raise ApiError(422, "VALIDATION_ERROR", "status 无效")
    secret = request.app.state.config.idempotency_secret
    scope = f"admin.users:{ctx.user.id}:{status}:{q}"
    page_size = clamp_limit(limit)
    async with request.app.state.db.session_factory() as db:
        query = select(User)
        if status:
            query = query.where(User.status == status)
        if q:
            literal = (
                q.strip()
                .replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            pattern = f"%{literal}%"
            query = query.where(
                or_(
                    User.email.ilike(pattern, escape="\\"),
                    User.display_name.ilike(pattern, escape="\\"),
                )
            )
        if cursor:
            payload = decode_cursor(secret, scope, cursor)
            last_created = dt.datetime.fromisoformat(payload["created_at"])
            last_id = payload["id"]
            query = query.where(
                or_(
                    User.created_at < last_created,
                    (User.created_at == last_created) & (User.id < last_id),
                )
            )
        rows = (
            await db.scalars(
                query.order_by(User.created_at.desc(), User.id.desc()).limit(
                    page_size + 1
                )
            )
        ).all()
        has_more = len(rows) > page_size
        rows = rows[:page_size]
        aggregates = await _aggregate_by_users(db, [row.id for row in rows])
        items = [
            {
                "user": user_out(row).model_dump(mode="json"),
                "totals": aggregates.get(
                    row.id,
                    {
                        "sessions": 0,
                        "received_audio_ms": 0,
                        "result_sessions": 0,
                        "last_session_at": None,
                    },
                ),
            }
            for row in rows
        ]
        next_cursor = None
        if has_more and rows:
            next_cursor = encode_cursor(
                secret,
                scope,
                {"created_at": rows[-1].created_at.isoformat(), "id": rows[-1].id},
            )
        return {"items": items, "next_cursor": next_cursor}


@router.post("/users", status_code=201)
async def create_user(
    data: dict,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ctx: AuthContext = Depends(require_admin),
):
    key = require_key(idempotency_key)
    if set(data) - {"email", "password", "display_name"}:
        raise ApiError(422, "VALIDATION_ERROR", "包含未知字段")
    if not isinstance(data.get("email"), str) or not isinstance(data.get("password"), str):
        raise ApiError(422, "VALIDATION_ERROR", "邮箱与密码必须为字符串")
    email, password = validate_email(data["email"]), data["password"]
    validate_password(password)
    display_name = data.get("display_name")
    if display_name is not None and (not isinstance(display_name, str) or not 1 <= len(display_name) <= 64):
        raise ApiError(422, "VALIDATION_ERROR", "显示名长度必须为 1–64 个字符")
    encoded = await password_work(request, hash_password, password)
    secret = request.app.state.config.idempotency_secret
    user_id = str(uuid.uuid4())
    async with request.app.state.db.session_factory() as db:
        from ..services.idempotency import begin
        actor = await db.scalar(select(User).where(User.id == ctx.user.id).with_for_update())
        if actor is None or actor.status != "active" or actor.role != "admin":
            raise ApiError(403, "FORBIDDEN", "需要有效管理员权限")
        replayed, target_id = await begin(db, secret, actor.id, "admin.user.create", key,
            {"email": email, "display_name": display_name, "password": password}, "user", user_id)
        if replayed:
            user = await db.get(User, target_id)
            if user is None:
                raise ApiError(409, "IDEMPOTENCY_CONFLICT", "幂等记录不存在")
            response.status_code = 200
            return {"user": user_out(user).model_dump(mode="json"), "replayed": True}
        if await db.scalar(select(User.id).where(User.email == email)):
            raise ApiError(409, "EMAIL_EXISTS", "该邮箱已注册")
        user = User(id=user_id, email=email, password_hash=encoded,
                    display_name=display_name, role="user", status="active")
        db.add(user)
        try:
            await db.flush()
            db.add(DictionaryState(user_id=user.id))
            await db.commit()
        except IntegrityError:
            await db.rollback()
            if await db.scalar(select(User.id).where(User.email == email)):
                raise ApiError(409, "EMAIL_EXISTS", "该邮箱已注册") from None
            raise
        return {"user": user_out(user).model_dump(mode="json"), "replayed": False}


@router.patch("/users/{user_id}")
async def patch_user(
    user_id: str,
    data: dict,
    request: Request,
    ctx: AuthContext = Depends(require_admin),
):
    if set(data) != {"status"} or data["status"] not in ("active", "disabled"):
        raise ApiError(422, "VALIDATION_ERROR", "status 无效或包含未知字段")
    status = data["status"]
    async with request.app.state.db.session_factory() as db:
        # Ordered locking also covers two admins attempting to disable each
        # other: the loser rechecks its own status rather than removing both.
        locked = list((await db.scalars(select(User).where(or_(User.role == "admin", User.id == user_id))
                                      .order_by(User.id).with_for_update())).all())
        actor = next((u for u in locked if u.id == ctx.user.id), None)
        user = next((u for u in locked if u.id == user_id), None)
        if actor is None or actor.status != "active" or actor.role != "admin":
            raise ApiError(403, "ACCOUNT_DISABLED" if actor and actor.status != "active" else "FORBIDDEN", "管理员权限已失效")
        if user is None:
            raise ApiError(404, "NOT_FOUND", "用户不存在")
        if status == "disabled" and (user.id == actor.id or
            (user.role == "admin" and sum(u.role == "admin" and u.status == "active" for u in locked) <= 1)):
            raise ApiError(409, "ADMIN_PROTECTED", "不能禁用当前或最后一个管理员")
        user.status = status
        if status == "disabled":
            rows = await db.scalars(select(LoginSession).where(LoginSession.user_id == user.id,
                LoginSession.revoked_at.is_(None)).order_by(LoginSession.id).with_for_update())
            for login in rows:
                login.revoked_at = utcnow()
        await db.commit()
        return {"user": user_out(user).model_dump(mode="json")}


async def _range_totals(db, date_from: dt.date, date_to: dt.date, user_id=None) -> dict:
    query = select(
        func.coalesce(func.sum(cast(SessionContribution.counted_session, Integer)), 0),
        func.coalesce(func.sum(SessionContribution.received_samples), 0),
        func.coalesce(
            func.sum(cast(SessionContribution.has_result, Integer)), 0
        ),
        func.coalesce(
            func.sum(
                cast(
                    SessionContribution.outcome == "failed", Integer
                )
            ),
            0,
        ),
        func.coalesce(
            func.sum(
                cast(
                    SessionContribution.outcome == "empty", Integer
                )
            ),
            0,
        ),
        func.coalesce(
            func.sum(
                cast(
                    SessionContribution.outcome == "cancelled", Integer
                )
            ),
            0,
        ),
        func.coalesce(func.sum(SessionContribution.char_count), 0),
        func.coalesce(func.sum(SessionContribution.successful_audio_samples), 0),
        func.coalesce(
            func.sum(cast(SessionContribution.fallback, Integer)), 0
        ),
        func.coalesce(
            func.sum(cast(SessionContribution.inserted, Integer)), 0
        ),
    ).where(
        SessionContribution.business_date >= date_from,
        SessionContribution.business_date <= date_to,
    )
    if user_id:
        query = query.where(SessionContribution.user_id == user_id)
    (
        sessions,
        samples,
        result_sessions,
        failed,
        empty,
        cancelled,
        chars,
        successful,
        fallback,
        inserted,
    ) = (await db.execute(query)).one()
    sessions = int(sessions or 0)
    result_sessions = int(result_sessions or 0)
    successful = int(successful or 0)
    totals = {
        "sessions": sessions,
        "received_audio_ms": int(samples or 0) * 1000 // 16000,
        "result_sessions": result_sessions,
        "failed_sessions": int(failed or 0),
        "empty_sessions": int(empty or 0),
        "cancelled_sessions": int(cancelled or 0),
        "chars": int(chars or 0),
        "successful_audio_ms": successful * 1000 // 16000,
        "fallback_sessions": int(fallback or 0),
        "inserted_sessions": int(inserted or 0),
        "learning_events": 0,
        "average_chars_per_minute": round(
            int(chars or 0) / ((successful * 1000 // 16000) / 60_000), 1
        )
        if successful
        else 0.0,
        "processing_success_rate": round(
            result_sessions / (result_sessions + int(failed or 0) + int(empty or 0)), 4
        )
        if result_sessions + int(failed or 0) + int(empty or 0)
        else 0.0,
        "insertion_success_rate": round(int(inserted or 0) / result_sessions, 4)
        if result_sessions
        else 0.0,
        "fallback_rate": round(int(fallback or 0) / result_sessions, 4)
        if result_sessions
        else 0.0,
    }
    start = dt.datetime.combine(date_from, dt.time.min, tzinfo=TZ).astimezone(
        dt.timezone.utc
    )
    end = dt.datetime.combine(
        date_to + dt.timedelta(days=1), dt.time.min, tzinfo=TZ
    ).astimezone(dt.timezone.utc)
    learning_query = select(func.count(LearningEvent.id)).where(
        LearningEvent.applied_at >= start,
        LearningEvent.applied_at < end,
    )
    if user_id:
        learning_query = learning_query.where(LearningEvent.user_id == user_id)
    totals["learning_events"] = int(await db.scalar(learning_query) or 0)
    return totals


@router.get("/stats/overview")
async def stats_overview(
    request: Request,
    date_from: dt.date = Query(alias="date_from"),
    date_to: dt.date = Query(alias="date_to"),
    ctx: AuthContext = Depends(require_admin),
):
    if date_to < date_from or (date_to - date_from).days > 365:
        raise ApiError(422, "VALIDATION_ERROR", "统计区间无效")
    async with request.app.state.db.session_factory() as db:
        registered = int(await db.scalar(select(func.count(User.id))) or 0)
        active = int(
            await db.scalar(
                select(func.count(User.id)).where(User.status == "active")
            )
            or 0
        )
        disabled = int(
            await db.scalar(
                select(func.count(User.id)).where(User.status == "disabled")
            )
            or 0
        )
        distinct_active = int(
            await db.scalar(
                select(func.count(func.distinct(case((SessionContribution.counted_session.is_(True), SessionContribution.user_id), else_=None)))).where(
                    SessionContribution.business_date >= date_from,
                    SessionContribution.business_date <= date_to,
                    SessionContribution.counted_session.is_(True),
                )
            )
            or 0
        )
        totals = await _range_totals(db, date_from, date_to)
        daily_rows = (
            await db.execute(
                select(
                    SessionContribution.business_date,
                    func.count(func.distinct(case((SessionContribution.counted_session.is_(True), SessionContribution.user_id), else_=None))),
                    func.coalesce(func.sum(cast(SessionContribution.counted_session, Integer)), 0),
                    func.coalesce(func.sum(SessionContribution.received_samples), 0),
                    func.coalesce(
                        func.sum(
                            cast(
                                SessionContribution.has_result, Integer
                            )
                        ),
                        0,
                    ),
                    func.coalesce(
                        func.sum(
                            cast(
                                SessionContribution.outcome == "failed",
                                Integer,
                            )
                        ),
                        0,
                    ),
                    func.coalesce(
                        func.sum(
                            cast(
                                SessionContribution.outcome == "empty",
                                Integer,
                            )
                        ),
                        0,
                    ),
                    func.coalesce(
                        func.sum(
                            cast(
                                SessionContribution.outcome == "cancelled",
                                Integer,
                            )
                        ),
                        0,
                    ),
                )
                .where(
                    SessionContribution.business_date >= date_from,
                    SessionContribution.business_date <= date_to,
                )
                .group_by(SessionContribution.business_date)
                .order_by(SessionContribution.business_date.asc())
            )
        ).all()
        by_date = {row[0]: row for row in daily_rows}
        daily = []
        day = date_from
        while day <= date_to:
            row = by_date.get(day)
            daily.append(
                {
                    "date": day.isoformat(),
                    "daily_active_users": int(row[1]) if row else 0,
                    "sessions": int(row[2]) if row else 0,
                    "received_audio_ms": int(row[3]) * 1000 // 16000 if row else 0,
                    "result_sessions": int(row[4]) if row else 0,
                    "failed_sessions": int(row[5]) if row else 0,
                    "empty_sessions": int(row[6]) if row else 0,
                    "cancelled_sessions": int(row[7]) if row else 0,
                }
            )
            day += dt.timedelta(days=1)
        return {
            "timezone": "Asia/Shanghai",
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "registered_users": registered,
            "active_users": active,
            "disabled_users": disabled,
            "distinct_active_users": distinct_active,
            "totals": totals,
            "daily": daily,
        }


@router.get("/stats/usage")
async def stats_usage(
    request: Request,
    date_from: dt.date = Query(alias="date_from"),
    date_to: dt.date = Query(alias="date_to"),
    user_id: str | None = None,
    cursor: str | None = None,
    limit: int | None = None,
    ctx: AuthContext = Depends(require_admin),
):
    if date_to < date_from or (date_to - date_from).days > 365:
        raise ApiError(422, "VALIDATION_ERROR", "统计区间无效")
    secret = request.app.state.config.idempotency_secret
    scope = f"admin.usage:{ctx.user.id}:{date_from}:{date_to}:{user_id}"
    page_size = clamp_limit(limit)
    start = dt.datetime.combine(date_from, dt.time.min, tzinfo=TZ).astimezone(dt.timezone.utc)
    end = dt.datetime.combine(date_to + dt.timedelta(days=1), dt.time.min, tzinfo=TZ).astimezone(dt.timezone.utc)
    fields = {
        "sessions": func.sum(cast(SessionContribution.counted_session, Integer)),
        "samples": func.sum(SessionContribution.received_samples),
        "result_sessions": func.sum(cast(SessionContribution.has_result, Integer)),
        "chars": func.sum(SessionContribution.char_count),
        "successful_samples": func.sum(SessionContribution.successful_audio_samples),
        "fallback_sessions": func.sum(cast(SessionContribution.fallback, Integer)),
        "inserted_sessions": func.sum(cast(SessionContribution.inserted, Integer)),
    }
    async with request.app.state.db.session_factory() as db:
        contributions = select(SessionContribution.business_date.label("day"), SessionContribution.user_id,
            *(func.coalesce(value, 0).label(key) for key, value in fields.items())).where(
                SessionContribution.business_date >= date_from, SessionContribution.business_date <= date_to)
        local_day = func.date(func.timezone("Asia/Shanghai", LearningEvent.applied_at))
        events = select(local_day.label("day"), LearningEvent.user_id, func.count(LearningEvent.id).label("learning_events"))            .where(LearningEvent.applied_at >= start, LearningEvent.applied_at < end)
        if user_id:
            contributions = contributions.where(SessionContribution.user_id == user_id)
            events = events.where(LearningEvent.user_id == user_id)
        c = contributions.group_by(SessionContribution.business_date, SessionContribution.user_id).subquery()
        e = events.group_by(local_day, LearningEvent.user_id).subquery()
        keys = union(select(c.c.day, c.c.user_id), select(e.c.day, e.c.user_id)).subquery()
        query = select(keys.c.day, keys.c.user_id,
            *(func.coalesce(c.c[key], 0).label(key) for key in fields),
            func.coalesce(e.c.learning_events, 0).label("learning_events"))            .outerjoin(c, (c.c.day == keys.c.day) & (c.c.user_id == keys.c.user_id))            .outerjoin(e, (e.c.day == keys.c.day) & (e.c.user_id == keys.c.user_id))
        if cursor:
            payload = decode_cursor(secret, scope, cursor)
            last_day = dt.date.fromisoformat(payload["date"])
            query = query.where(or_(keys.c.day < last_day,
                (keys.c.day == last_day) & (keys.c.user_id > payload["user_id"])))
        rows = (await db.execute(query.order_by(keys.c.day.desc(), keys.c.user_id.asc()).limit(page_size + 1))).mappings().all()
        has_more, rows = len(rows) > page_size, rows[:page_size]
        items = [{"date": row["day"].isoformat(), "user_id": row["user_id"],
            "sessions": int(row["sessions"]), "received_audio_ms": int(row["samples"]) * 1000 // 16000,
            "result_sessions": int(row["result_sessions"]), "chars": int(row["chars"]),
            "successful_audio_ms": int(row["successful_samples"]) * 1000 // 16000,
            "fallback_sessions": int(row["fallback_sessions"]), "inserted_sessions": int(row["inserted_sessions"]),
            "learning_events": int(row["learning_events"])} for row in rows]
        next_cursor = encode_cursor(secret, scope, {"date": rows[-1]["day"].isoformat(), "user_id": rows[-1]["user_id"]}) if has_more and rows else None
        return {"timezone": "Asia/Shanghai", "items": items, "next_cursor": next_cursor}


@router.get("/stats/models")
async def stats_models(
    request: Request,
    date_from: dt.date = Query(alias="date_from"),
    date_to: dt.date = Query(alias="date_to"),
    user_id: str | None = None,
    ctx: AuthContext = Depends(require_admin),
):
    if date_to < date_from or (date_to - date_from).days > 365:
        raise ApiError(422, "VALIDATION_ERROR", "统计区间无效")
    numeric = ("calls", "successes", "failures", "unknown_calls", "elapsed_ms", "processed_audio_samples",
               "prompt_tokens_known", "completion_tokens_known", "prompt_tokens_unknown_calls", "completion_tokens_unknown_calls")
    async with request.app.state.db.session_factory() as db:
        query = select(ModelUsageDaily.date, ModelUsageDaily.provider, ModelUsageDaily.model_id, ModelUsageDaily.purpose,
            *(func.sum(getattr(ModelUsageDaily, name)).label(name) for name in numeric)).where(
                ModelUsageDaily.date >= date_from, ModelUsageDaily.date <= date_to)
        if user_id:
            query = query.where(ModelUsageDaily.user_id == user_id)
        rows = (await db.execute(query.group_by(ModelUsageDaily.date, ModelUsageDaily.provider,
            ModelUsageDaily.model_id, ModelUsageDaily.purpose).order_by(ModelUsageDaily.date,
            ModelUsageDaily.provider, ModelUsageDaily.model_id, ModelUsageDaily.purpose))).mappings().all()
        items = []
        for row in rows:
            item = {"date": row["date"].isoformat(), "provider": row["provider"], "model_id": row["model_id"], "purpose": row["purpose"]}
            item.update({name: int(row[name] or 0) for name in numeric if name != "processed_audio_samples"})
            item["processed_audio_ms"] = int(row["processed_audio_samples"] or 0) * 1000 // 16000
            items.append(item)
        return {"timezone": "Asia/Shanghai", "items": items}


def _percentile(values: list[int], fraction: float, failures: int) -> int | None:
    total = len(values) + failures
    if total == 0:
        return None
    rank = math.ceil(fraction * total)
    if rank > len(values):
        return None
    return sorted(values)[rank - 1]


async def _capacity_view(request: Request) -> dict:
    from sqlalchemy import select

    from ..models import RuntimeCapacity, SessionAttempt

    config = request.app.state.config
    async with request.app.state.db.session_factory() as db:
        row = await db.get(RuntimeCapacity, 1)
        if row is None:
            raise ApiError(503, "SERVICE_UNAVAILABLE", "容量配置不可用")
        now = utcnow()
        window_start = now - dt.timedelta(seconds=300)
        active_sessions, audio_reservations, known_attempts = (
            await db.execute(
                select(
                    func.count(VoiceSession.id),
                    func.count(SessionAttempt.id).filter(
                        SessionAttempt.kind.in_(("initial", "asr"))),
                    func.count(SessionAttempt.id),
                )
                .select_from(VoiceSession)
                .outerjoin(SessionAttempt,
                    (SessionAttempt.user_id == VoiceSession.user_id)
                    & (SessionAttempt.session_id == VoiceSession.id)
                    & (SessionAttempt.attempt_no == VoiceSession.current_attempt_no))
                .where(VoiceSession.state.in_(ACTIVE_STATES))
            )
        ).one()
        # A text-only refine retry consumes an activity slot, not a new audio
        # reservation. Keep initial/ASR reservations through the terminal state
        # and never count historical attempts a second time.
        reserved_pcm = int(audio_reservations) * config.session_pcm_reserve
        attempt_rows = (
            await db.execute(
                select(SessionAttempt.stop_to_result_ms, SessionAttempt.state, SessionAttempt.error_code)
                .join(
                    VoiceSession,
                    (VoiceSession.user_id == SessionAttempt.user_id)
                    & (VoiceSession.id == SessionAttempt.session_id),
                )
                .where(
                    SessionAttempt.kind == "initial",
                    SessionAttempt.ended_at >= window_start,
                    VoiceSession.audio_complete.is_(True), VoiceSession.received_samples > 0,
                    SessionAttempt.state.in_(("ready", "failed", "empty")),
                )
            )
        ).all()
        successful = [
            int(stop)
            for stop, state, error in attempt_rows
            if state == "ready" and stop is not None and error is None
        ]
        failed = sum(1 for _, state, error in attempt_rows if state in ("failed", "empty") or error is not None)
        from ..models import SessionResult

        fallback = int(
            await db.scalar(
                select(func.count(func.distinct(SessionResult.session_id)))
                .join(SessionAttempt, (SessionAttempt.user_id == SessionResult.user_id)
                    & (SessionAttempt.session_id == SessionResult.session_id)
                    & (SessionAttempt.attempt_no == SessionResult.attempt_no))
                .join(VoiceSession, (VoiceSession.user_id == SessionResult.user_id)
                    & (VoiceSession.id == SessionResult.session_id))
                .where(SessionAttempt.kind == "initial", SessionAttempt.ended_at >= window_start,
                    SessionAttempt.state == "ready", VoiceSession.audio_complete.is_(True),
                    VoiceSession.received_samples > 0, SessionResult.review_required.is_(True))
            )
            or 0
        )
        manager = request.app.state.sessions
        runtime = None
        metrics = getattr(request.app.state, "admission_metrics", None)
        try:
            asr = request.app.state.asr.scheduler.snapshot()
            llm = request.app.state.llm.scheduler.snapshot()
            if manager is not None and metrics is not None and known_attempts == active_sessions:
                runtimes = list(manager.runtimes.values())
                active_pcm = sum(len(item.audio) for item in runtimes if item.state in ACTIVE_STATES)
                retry_pcm = sum(len(item.audio) for item in runtimes if item.state == "failed")
                runtime = {
                    "sampled_at": utc_iso8601(now), "active_sessions": active_sessions,
                    "available_sessions": (max(0, min(row.max_active_sessions, config.effective_admission_ceiling) - active_sessions)
                        if request.app.state.ready else 0),
                    "draining": active_sessions > row.max_active_sessions,
                    "asr_running": asr["running"], "asr_partial_waiting": asr["low_waiting"],
                    "asr_finish_waiting": asr["high_waiting"], "llm_running": llm["running"],
                    "llm_foreground_waiting": llm["high_waiting"], "llm_classify_waiting": llm["low_waiting"],
                    "active_pcm_bytes": active_pcm, "reserved_pcm_bytes": reserved_pcm,
                    "retry_pcm_bytes": retry_pcm, "window_seconds": 300,
                    "sample_count": len(successful) + failed, "rejected_count": metrics.rejected_count(),
                    "failed_count": failed, "fallback_count": fallback,
                    "stop_to_result_p50_ms": _percentile(successful, .5, failed),
                    "stop_to_result_p95_ms": _percentile(successful, .95, failed),
                }
        except (AttributeError, KeyError, TypeError, RuntimeError):
            runtime = None
        capacity_config = getattr(config, "capacity", None)
        validation = load_capacity_report(getattr(capacity_config, "validation_report_path", None),
                                         getattr(request.app.state, "trusted_environment_fingerprint", None))
        if validation["report"] is not None:
            report = validation["report"]
            validation = {**validation, "report": {**report, "tested_at": utc_iso8601(
                dt.datetime.fromisoformat(report["tested_at"].replace("Z", "+00:00")))}}
        return {
            "config": {
                "version": row.version,
                "max_active_sessions": row.max_active_sessions,
                "updated_at": utc_iso8601(row.updated_at),
                "updated_by": row.updated_by,
            },
            "deployment": {
                "admission_ceiling": config.sessions.admission_ceiling,
                "effective_ceiling": config.effective_admission_ceiling,
                "active_pcm_budget_bytes": config.audio.active_pcm_budget_bytes,
                "retry_pcm_budget_bytes": config.retry.audio_budget_bytes,
                "asr_execution_slots": config.asr.execution_slots,
                "llm_max_inflight": config.llm.max_inflight,
            },
            "runtime": runtime,
            "validation": validation,
        }


@router.get("/capacity")
async def get_capacity(request: Request, ctx: AuthContext = Depends(require_admin)):
    try:
        return await _capacity_view(request)
    except SQLAlchemyError as exc:
        raise ApiError(503, "SERVICE_UNAVAILABLE", "容量数据暂时不可用", retryable=True) from exc


@router.patch("/capacity")
async def patch_capacity(
    request: Request, data: dict, ctx: AuthContext = Depends(require_admin)
):
    unknown = set(data) - {"version", "max_active_sessions"}
    if unknown:
        raise ApiError(
            422,
            "VALIDATION_ERROR",
            "包含未知字段",
            details={"fields": sorted(unknown)},
        )
    version = data.get("version")
    value = data.get("max_active_sessions")
    if isinstance(version, bool) or not isinstance(version, int) or not 1 <= version <= 2_147_483_647:
        raise ApiError(422, "VALIDATION_ERROR", "version 无效")
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 2_147_483_647:
        raise ApiError(422, "VALIDATION_ERROR", "max_active_sessions 无效")
    config = request.app.state.config
    if value > config.effective_admission_ceiling:
        raise ApiError(
            422,
            "VALIDATION_ERROR",
            "容量超过部署上限",
            details={"max_allowed": config.effective_admission_ceiling},
        )
    async with request.app.state.db.session_factory() as db:
        try:
            await request.app.state.capacity.lock(db)
        except CapacityError as exc:
            raise ApiError(503, "SERVICE_UNAVAILABLE", "容量配置暂时不可用", retryable=True) from exc
        actor = await db.scalar(select(User).where(User.id == ctx.user.id).with_for_update())
        if actor is None or actor.status != "active" or actor.role != "admin":
            raise ApiError(403, "ACCOUNT_DISABLED" if actor and actor.status != "active" else "FORBIDDEN", "管理员权限已失效")
        try:
            await request.app.state.capacity.update(db, version, value, ctx.user.id)
        except CapacityError as exc:
            if "version" in str(exc):
                current = await db.get(RuntimeCapacity, 1)
                raise ApiError(409, "VERSION_CONFLICT", "容量配置已变化",
                               details={"current_version": current.version if current else None}) from exc
            raise ApiError(
                422,
                "VALIDATION_ERROR",
                "容量值无效",
                details={"max_allowed": config.effective_admission_ceiling},
            ) from exc
        await db.commit()
    return await _capacity_view(request)
