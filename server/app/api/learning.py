from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Header, Request, Response
from sqlalchemy import select

from ..core.security import idempotency_hash
from ..models import Insertion, LearningEvent, SessionResult, VoiceSession
from ..schemas.session import CorrectionIn, EmptyIn
from ..services import dictionary_service as dictionary_svc
from ..services import learning_service
from ..services.idempotency import lock_user, require_key
from ..services.refiner_core import correction_guard
from .deps import ApiError, AuthContext, require_auth, require_desktop

router = APIRouter(tags=["learning"])


def _require_key(key: str | None) -> str:
    return require_key(key)


@router.post("/api/v1/corrections", status_code=202)
async def submit_correction(
    data: CorrectionIn,
    request: Request,
    response: Response,
    ctx: AuthContext = Depends(require_desktop),
):
    secret = request.app.state.config.idempotency_secret
    request_hash = idempotency_hash(
        secret,
        "learning.correction",
        {
            "session_id": data.session_id,
            "insertion_id": data.insertion_id,
            "correction_id": data.correction_id,
            "offset": data.offset,
            "before": data.before,
            "after": data.after,
            "left": data.left,
            "right": data.right,
        },
    )
    async with request.app.state.db.session_factory() as db:
        user = await lock_user(db, ctx.user.id)
        session = await db.scalar(
            select(VoiceSession).where(
                VoiceSession.id == data.session_id,
                VoiceSession.user_id == ctx.user.id,
            ).with_for_update()
        )
        if session is None:
            raise ApiError(404, "NOT_FOUND", "会话不存在")
        insertion = await db.scalar(
            select(Insertion).where(
                Insertion.user_id == ctx.user.id,
                Insertion.session_id == data.session_id,
                Insertion.id == data.insertion_id,
            )
        )
        if insertion is None:
            raise ApiError(404, "NOT_FOUND", "插入记录不存在")
        if insertion.device_id != ctx.device_id:
            raise ApiError(403, "FORBIDDEN", "插入资格属于其他设备")
        existing = await db.scalar(
            select(LearningEvent).where(
                LearningEvent.user_id == ctx.user.id,
                LearningEvent.correction_id == data.correction_id,
            )
        )
        if existing is not None:
            if existing.request_hash != request_hash:
                raise ApiError(409, "IDEMPOTENCY_CONFLICT", "相同 correction_id 对应了不同内容")
            response.status_code = 200
            return await learning_service.event_out(db, existing, replayed=True)

        from ..services.retention import content_expired

        if content_expired(session, user):
            raise ApiError(410, "CONTENT_EXPIRED", "结果正文已过期")
        if insertion.status != "inserted" or not insertion.observable:
            raise ApiError(409, "LEARNING_NOT_ALLOWED", "该插入尚不可学习")
        if not session.learning_enabled or not user.learning_enabled:
            raise ApiError(409, "LEARNING_NOT_ALLOWED", "学习已关闭")
        if not session.current_result_id:
            raise ApiError(410, "CONTENT_EXPIRED", "结果正文已过期")
        if session.current_result_id != insertion.result_id:
            raise ApiError(409, "LEARNING_NOT_ALLOWED", "插入结果已变化")
        result = await db.scalar(select(SessionResult).where(
            SessionResult.user_id == ctx.user.id,
            SessionResult.session_id == session.id,
            SessionResult.id == insertion.result_id,
        ))
        if result is None or result.content_cleared_at is not None:
            raise ApiError(410, "CONTENT_EXPIRED", "结果正文已过期")
        text = result.refined_text or ""
        end = data.offset + len(data.before)
        if end > len(text) or text[data.offset:end] != data.before:
            raise ApiError(422, "VALIDATION_ERROR", "修正位置与结果正文不匹配")
        if data.left and not text[: data.offset].endswith(data.left):
            raise ApiError(422, "VALIDATION_ERROR", "左侧上下文与结果正文不匹配")
        if data.right and not text[end:].startswith(data.right):
            raise ApiError(422, "VALIDATION_ERROR", "右侧上下文与结果正文不匹配")

        guard_reason = correction_guard(
            data.before, data.after, source=text, offset=data.offset,
        )
        await dictionary_svc.lock_state(db, ctx.user.id)
        expected_term = await dictionary_svc.find_by_norm(
            db, ctx.user.id, dictionary_svc.norm_of(data.after)
        )
        event = LearningEvent(
            user_id=ctx.user.id,
            session_id=data.session_id,
            insertion_id=data.insertion_id,
            correction_id=data.correction_id,
            request_hash=request_hash,
            offset=data.offset,
            before_text=data.before,
            after_text=data.after,
            left_context=data.left,
            right_context=data.right,
            status="ignored" if guard_reason else "pending",
            classification="ignore" if guard_reason else None,
            reason=guard_reason,
            expected_term_id=expected_term.id if expected_term else None,
            expected_term_version=expected_term.version if expected_term else None,
        )
        db.add(event)
        await db.commit()
        await db.refresh(event)
        if not guard_reason:
            learning_service.schedule(request.app, event.id, ctx.user.id)
        return await learning_service.event_out(db, event, replayed=False)


@router.get("/api/v1/learning-events/{event_id}")
async def get_event(
    event_id: str, request: Request, ctx: AuthContext = Depends(require_auth)
):
    async with request.app.state.db.session_factory() as db:
        event = await db.scalar(
            select(LearningEvent).where(
                LearningEvent.user_id == ctx.user.id, LearningEvent.id == event_id
            )
        )
        if event is None:
            raise ApiError(404, "NOT_FOUND", "学习事件不存在")
        return await learning_service.event_out(db, event)


@router.post("/api/v1/learning-events/{event_id}/confirm")
async def confirm_event(
    event_id: str,
    request: Request,
    body: EmptyIn | None = Body(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ctx: AuthContext = Depends(require_desktop),
):
    key = _require_key(idempotency_key)
    return await learning_service.confirm_event(request.app, event_id, ctx.user.id, key)


@router.post("/api/v1/learning-events/{event_id}/undo")
async def undo_event(
    event_id: str,
    request: Request,
    body: EmptyIn | None = Body(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ctx: AuthContext = Depends(require_desktop),
):
    key = _require_key(idempotency_key)
    return await learning_service.undo_event(request.app, event_id, ctx.user.id, key)
