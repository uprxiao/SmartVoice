from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from ..core.security import idempotency_hash
from ..models import Insertion, SessionResult, VoiceSession
from ..schemas.session import InsertionClaimIn, InsertionOut, InsertionReceiptIn
from ..services.retention import content_expired
from ..services.idempotency import lock_user
from .deps import ApiError, AuthContext, require_desktop

router = APIRouter(tags=["insertions"])

ERROR_CODES = {
    "TARGET_CHANGED",
    "TARGET_UNSUPPORTED",
    "PERMISSION_DENIED",
    "ZERO_WRITE",
    "VERIFY_FAILED",
    "CLIENT_INTERRUPTED",
}


def insertion_out(row: Insertion, replayed: bool = False) -> InsertionOut:
    return InsertionOut(
        insertion_id=row.id,
        session_id=row.session_id,
        result_id=row.result_id,
        status=row.status,
        observable=row.observable,
        error_code=row.error_code,
        claimed_at=row.claimed_at,
        completed_at=row.completed_at,
        replayed=replayed,
    )


async def _load_session(db, session_id: str, user_id: str, *, lock: bool = False) -> VoiceSession:
    query = select(VoiceSession).where(
        VoiceSession.id == session_id, VoiceSession.user_id == user_id
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    session = await db.scalar(query)
    if session is None:
        raise ApiError(404, "NOT_FOUND", "会话不存在")
    return session


@router.post(
    "/api/v1/sessions/{session_id}/insertions/claim",
    status_code=201,
    response_model=InsertionOut,
)
async def claim_insertion(
    session_id: str,
    data: InsertionClaimIn,
    request: Request,
    response: Response,
    ctx: AuthContext = Depends(require_desktop),
):
    secret = request.app.state.config.idempotency_secret
    request_hash = idempotency_hash(
        secret,
        "insertion.claim",
        {
            "session_id": session_id,
            "result_id": data.result_id,
            "insertion_id": data.insertion_id,
            "text_hash": data.text_hash,
            "review_confirmed": data.review_confirmed,
        },
    )
    async with request.app.state.db.session_factory() as db:
        user = await lock_user(db, ctx.user.id)
        session = await _load_session(db, session_id, ctx.user.id, lock=True)
        existing = await db.scalar(
            select(Insertion).where(
                Insertion.user_id == ctx.user.id,
                Insertion.id == data.insertion_id,
            )
        )
        if existing is not None:
            if existing.device_id != ctx.device_id:
                raise ApiError(403, "FORBIDDEN", "插入资格属于其他设备")
            if existing.session_id != session_id or existing.result_id != data.result_id:
                raise ApiError(409, "IDEMPOTENCY_CONFLICT", "相同 insertion_id 对应了不同请求")
            if existing.request_hash != request_hash:
                raise ApiError(409, "IDEMPOTENCY_CONFLICT", "相同 insertion_id 对应了不同请求")
            response.status_code = 200
            return insertion_out(existing, replayed=True)
        if content_expired(session, user):
            raise ApiError(410, "CONTENT_EXPIRED", "结果正文已过期")
        if session.state != "ready" or not session.current_result_id:
            raise ApiError(409, "STALE_RESULT", "结果尚未就绪")
        if session.current_result_id != data.result_id:
            raise ApiError(409, "STALE_RESULT", "结果已被新的重试替换")
        result = await db.scalar(select(SessionResult).where(
            SessionResult.user_id == ctx.user.id,
            SessionResult.session_id == session_id,
            SessionResult.id == data.result_id,
        ))
        if result is None or result.content_cleared_at is not None:
            raise ApiError(410, "CONTENT_EXPIRED", "结果正文已过期")
        active = await db.scalar(
            select(Insertion).where(
                Insertion.user_id == ctx.user.id,
                Insertion.result_id == data.result_id,
                Insertion.status.in_(("reserved", "inserted", "unverified")),
            )
        )
        if active is not None:
            raise ApiError(409, "INSERTION_ALREADY_CLAIMED", "该结果已被申请插入")
        if result.text_hash != data.text_hash:
            raise ApiError(409, "RESULT_HASH_MISMATCH", "结果内容已变化")
        if result.review_required and not data.review_confirmed:
            raise ApiError(409, "REVIEW_REQUIRED", "该结果需要用户确认后才能插入")
        claimed_id = await db.scalar(
            insert(Insertion).values(
                id=data.insertion_id, user_id=ctx.user.id, session_id=session_id,
                result_id=data.result_id, device_id=ctx.device_id,
                text_hash=data.text_hash, review_confirmed=data.review_confirmed,
                status="reserved", observable=False, request_hash=request_hash,
            ).on_conflict_do_nothing(index_elements=[Insertion.id]).returning(Insertion.id)
        )
        if claimed_id is None:
            # The same account is serialized by its User lock, so this ID is
            # owned by another tenant. Do not leak that tenant's registration.
            raise ApiError(404, "NOT_FOUND", "插入记录不存在")
        await db.commit()
        row = await db.scalar(select(Insertion).where(
            Insertion.user_id == ctx.user.id, Insertion.id == claimed_id,
        ))
        return insertion_out(row)


@router.get(
    "/api/v1/sessions/{session_id}/insertions/{insertion_id}",
    response_model=InsertionOut,
)
async def get_insertion(
    session_id: str,
    insertion_id: str,
    request: Request,
    ctx: AuthContext = Depends(require_desktop),
):
    async with request.app.state.db.session_factory() as db:
        session = await _load_session(db, session_id, ctx.user.id)
        if session is None:
            raise ApiError(404, "NOT_FOUND", "会话不存在")
        row = await db.scalar(
            select(Insertion).where(
                Insertion.user_id == ctx.user.id,
                Insertion.id == insertion_id,
                Insertion.session_id == session_id,
            )
        )
        if row is None:
            raise ApiError(404, "NOT_FOUND", "插入记录不存在")
        return insertion_out(row)


@router.post(
    "/api/v1/sessions/{session_id}/insertions/{insertion_id}/receipt",
    response_model=InsertionOut,
)
async def insertion_receipt(
    session_id: str,
    insertion_id: str,
    data: InsertionReceiptIn,
    request: Request,
    ctx: AuthContext = Depends(require_desktop),
):
    async with request.app.state.db.session_factory() as db:
        await lock_user(db, ctx.user.id)
        await _load_session(db, session_id, ctx.user.id, lock=True)
        row = await db.scalar(
            select(Insertion).where(
                Insertion.user_id == ctx.user.id,
                Insertion.id == insertion_id,
                Insertion.session_id == session_id,
            )
        )
        if row is None:
            raise ApiError(404, "NOT_FOUND", "插入记录不存在")
        if row.device_id != ctx.device_id:
            raise ApiError(403, "FORBIDDEN", "插入资格属于其他设备")
        if data.status == "inserted" and data.error_code is not None:
            raise ApiError(422, "VALIDATION_ERROR", "成功回执不能携带错误码")
        if data.status != "inserted" and data.error_code is None:
            raise ApiError(422, "VALIDATION_ERROR", "失败回执必须携带错误码")
        if data.error_code is not None and data.error_code not in ERROR_CODES:
            raise ApiError(422, "VALIDATION_ERROR", "错误码无效")
        if data.observable and data.status != "inserted":
            raise ApiError(422, "VALIDATION_ERROR", "只有成功插入才可观察")
        if row.status != "reserved":
            if (
                row.status == data.status
                and row.observable == data.observable
                and row.error_code == data.error_code
            ):
                return insertion_out(row, replayed=True)
            raise ApiError(409, "INSERTION_STATE_CONFLICT", "插入状态已确定")
        from datetime import datetime, timezone

        from ..services import stats_service

        # A terminal receipt is immutable. The common session lock also
        # serializes this transition against claim, retry and result replacement.
        changed = await db.execute(
            update(Insertion).where(
                Insertion.user_id == ctx.user.id,
                Insertion.session_id == session_id,
                Insertion.id == insertion_id,
                Insertion.status == "reserved",
            ).values(status=data.status, observable=data.observable,
                     error_code=data.error_code, completed_at=datetime.now(timezone.utc))
        )
        if changed.rowcount != 1:
            raise ApiError(409, "INSERTION_STATE_CONFLICT", "插入状态已确定")
        if data.status == "inserted":
            await stats_service.record_inserted(db, ctx.user.id, session_id)
        await db.commit()
        await db.refresh(row)
        return insertion_out(row)
