"""`/api/admin/providers*` (V2 design §8.1). Admin role is re-checked per request."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request

from ..services.pagination import clamp_limit, decode_cursor, encode_cursor
from .deps import ApiError, AuthContext, require_admin

router = APIRouter(prefix="/api/admin/providers", tags=["admin-providers"])
CURSOR_SCOPE = "admin.providers.audit"


def _service(request: Request):
    service = getattr(request.app.state, "provider_service", None)
    if service is None:
        raise ApiError(503, "SERVICE_UNAVAILABLE", "服务正在启动", retryable=True)
    return service


def _body(payload: Any) -> dict:
    if not isinstance(payload, dict):
        raise ApiError(422, "VALIDATION_ERROR", "请求体必须是对象", details={"fields": []})
    return payload


@router.get("")
async def list_providers(request: Request, kind: str | None = Query(default=None),
                         ctx: AuthContext = Depends(require_admin)):
    return {"items": await _service(request).list(kind)}


@router.post("", status_code=201)
async def create_provider(request: Request, payload: Any = Body(...), ctx: AuthContext = Depends(require_admin)):
    return await _service(request).create(ctx.user.id, _body(payload))


@router.get("/active")
async def get_active(request: Request, ctx: AuthContext = Depends(require_admin)):
    return await _service(request).active()


@router.put("/active")
async def put_active(request: Request, payload: Any = Body(...), ctx: AuthContext = Depends(require_admin)):
    return await _service(request).activate(ctx.user.id, _body(payload))


@router.get("/audit")
async def get_audit(request: Request, limit: int | None = Query(default=None),
                    cursor: str | None = Query(default=None), ctx: AuthContext = Depends(require_admin)):
    secret = request.app.state.config.idempotency_secret
    after = None
    if cursor is not None:
        value = decode_cursor(secret, CURSOR_SCOPE, cursor).get("seq")
        if not isinstance(value, int) or isinstance(value, bool):
            raise ApiError(422, "CURSOR_INVALID", "分页游标无效")
        after = value
    items, last = await _service(request).audit(clamp_limit(limit), after)
    return {"items": items, "next_cursor": encode_cursor(secret, CURSOR_SCOPE, {"seq": last}) if last else None}


@router.patch("/{provider_id}")
async def patch_provider(provider_id: str, request: Request, payload: Any = Body(...),
                         ctx: AuthContext = Depends(require_admin)):
    return await _service(request).update(ctx.user.id, provider_id, _body(payload))


@router.delete("/{provider_id}", status_code=200)
async def delete_provider(provider_id: str, request: Request, ctx: AuthContext = Depends(require_admin)):
    await _service(request).delete(ctx.user.id, provider_id)
    return {"deleted": True, "id": provider_id}


@router.post("/{provider_id}/test")
async def test_provider(provider_id: str, request: Request, ctx: AuthContext = Depends(require_admin)):
    return await _service(request).test(ctx.user.id, provider_id)


