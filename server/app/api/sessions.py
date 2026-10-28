from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request, Response, WebSocket

from ..core.ws_denial import deny_websocket
from ..schemas.session import RetryIn, SessionCreateIn, SessionOut
from ..services.session_runtime import SessionError
from .deps import ApiError, AuthContext, authenticate, require_desktop

from ..services.idempotency import require_key

router = APIRouter(tags=["sessions"])


def _raise(exc: SessionError) -> None:
    raise ApiError(
        exc.status,
        exc.code,
        exc.message,
        details=exc.details,
        retryable=exc.details.get("retryable", False),
        headers=exc.headers,
    )


@router.post("/api/v1/sessions", response_model=SessionOut)
async def create_session(
    data: SessionCreateIn,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ctx: AuthContext = Depends(require_desktop),
):
    idempotency_key = require_key(idempotency_key)
    if idempotency_key != data.client_request_id:
        raise ApiError(
            422,
            "VALIDATION_ERROR",
            "Idempotency-Key 必须等于 client_request_id",
        )
    manager = request.app.state.sessions
    try:
        session, created = await manager.create(
            ctx,
            client_request_id=data.client_request_id,
            learning_enabled=data.learning_enabled,
        )
        result = await manager.output(session.id, caller=ctx)
    except SessionError as exc:
        _raise(exc)
        raise
    response.status_code = 201 if created else 200
    result["replayed"] = not created
    return result


@router.get("/api/v1/sessions/{session_id}", response_model=SessionOut)
async def get_session(
    session_id: str, request: Request, ctx: AuthContext = Depends(require_desktop)
):
    try:
        await request.app.state.sessions.get(session_id, ctx.user.id)
        return await request.app.state.sessions.output(session_id, caller=ctx)
    except SessionError as exc:
        _raise(exc)
        raise


@router.post("/api/v1/sessions/{session_id}/cancel", response_model=SessionOut)
async def cancel_session(
    session_id: str, request: Request, ctx: AuthContext = Depends(require_desktop)
):
    try:
        return await request.app.state.sessions.cancel(session_id, ctx.user.id, caller=ctx)
    except SessionError as exc:
        _raise(exc)
        raise


@router.post("/api/v1/sessions/{session_id}/retry", response_model=SessionOut)
async def retry_session(
    session_id: str,
    data: RetryIn,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ctx: AuthContext = Depends(require_desktop),
):
    idempotency_key = require_key(idempotency_key)
    try:
        body, created = await request.app.state.sessions.retry(
            ctx, session_id, data.retry_kind, idempotency_key
        )
    except SessionError as exc:
        _raise(exc)
        raise
    response.status_code = 202 if created else 200
    body["replayed"] = not created
    return body


async def websocket_session(websocket: WebSocket, session_id: str) -> None:
    config = websocket.app.state.config
    origin = websocket.headers.get("origin")
    if origin and origin not in config.security.ws_origins:
        await deny_websocket(websocket, 403, "FORBIDDEN", "不允许的 WebSocket 来源")
        return
    authorization = websocket.headers.get("authorization")
    try:
        ctx = await authenticate(websocket, authorization)
    except ApiError as exc:
        await deny_websocket(
            websocket, exc.status, exc.code, exc.message,
            retryable=exc.retryable, details=exc.details, headers=exc.headers,
        )
        return
    if ctx.client_type != "desktop" or not ctx.device_id:
        await deny_websocket(websocket, 403, "FORBIDDEN", "该操作只允许桌面客户端")
        return
    await websocket.app.state.sessions.handle_ws(websocket, session_id, ctx)
