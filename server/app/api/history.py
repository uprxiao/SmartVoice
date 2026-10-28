from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import or_, select

from ..core.security import utcnow
from ..models import VoiceSession
from ..schemas.session import HistoryOut
from ..services.pagination import clamp_limit, decode_cursor, encode_cursor
from .deps import ApiError, AuthContext, require_auth

router = APIRouter(prefix="/api/v1", tags=["history"])


@router.get("/history", response_model=HistoryOut, response_model_exclude_unset=True)
async def history(
    request: Request,
    type: str = "all",
    cursor: str | None = None,
    limit: int | None = None,
    ctx: AuthContext = Depends(require_auth),
):
    if type not in ("all", "dictation"):
        raise ApiError(422, "VALIDATION_ERROR", "type 只支持 all 或 dictation")
    secret = request.app.state.config.idempotency_secret
    page_size = clamp_limit(limit)
    scope = f"history:{ctx.user.id}:{type}"
    now = utcnow()
    payload = decode_cursor(secret, scope, cursor) if cursor else None
    as_of = (
        dt.datetime.fromisoformat(payload["as_of"]) if payload else now
    )
    async with request.app.state.db.session_factory() as db:
        query = select(VoiceSession).where(
            VoiceSession.user_id == ctx.user.id,
            VoiceSession.content_cleared_at.is_(None),
            VoiceSession.created_at <= as_of,
        )
        if ctx.user.history_retention_days:
            cutoff = now - dt.timedelta(days=ctx.user.history_retention_days)
            query = query.where(
                or_(
                    VoiceSession.first_terminal_at.is_(None),
                    VoiceSession.first_terminal_at >= cutoff,
                )
            )
        if payload:
            last_created = dt.datetime.fromisoformat(payload["created_at"])
            last_id = payload["id"]
            query = query.where(
                or_(
                    VoiceSession.created_at < last_created,
                    (VoiceSession.created_at == last_created)
                    & (VoiceSession.id < last_id),
                )
            )
        rows = (
            await db.scalars(
                query.order_by(
                    VoiceSession.created_at.desc(), VoiceSession.id.desc()
                ).limit(page_size + 1)
            )
        ).all()
        has_more = len(rows) > page_size
        rows = rows[:page_size]
        next_cursor = None
        if has_more and rows:
            next_cursor = encode_cursor(
                secret,
                scope,
                {
                    "as_of": as_of.isoformat(),
                    "created_at": rows[-1].created_at.isoformat(),
                    "id": rows[-1].id,
                },
            )
        items = [
            await request.app.state.sessions.output(row.id, caller=ctx) for row in rows
        ]
        return {"items": items, "next_cursor": next_cursor}


@router.get("/stats/me")
async def stats_me(
    request: Request,
    date_from: dt.date = Query(alias="date_from"),
    date_to: dt.date = Query(alias="date_to"),
    user_id: str | None = Query(default=None),
    ctx: AuthContext = Depends(require_auth),
):
    if user_id is not None:
        raise ApiError(422, "VALIDATION_ERROR", "个人统计不接受 user_id")
    if date_to < date_from:
        raise ApiError(422, "VALIDATION_ERROR", "结束日期不能早于开始日期")
    if (date_to - date_from).days > 365:
        raise ApiError(422, "VALIDATION_ERROR", "统计区间最多 366 天")
    async with request.app.state.db.session_factory() as db:
        from ..services import stats_service

        return await stats_service.stats_me(db, ctx.user.id, date_from, date_to)
