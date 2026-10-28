from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from ..models import Device, User
from ..schemas.common import DeviceOut, DisplayNameIn, UserOut
from .auth import settings_out, user_out
from .deps import ApiError, AuthContext, require_auth

router = APIRouter(tags=["users"])


@router.get("/api/users/me")
async def me(request: Request, ctx: AuthContext = Depends(require_auth)):
    return {"user": user_out(ctx.user), "settings": settings_out(ctx.user)}


@router.patch("/api/users/me")
async def patch_me(
    data: DisplayNameIn, request: Request, ctx: AuthContext = Depends(require_auth)
):
    async with request.app.state.db.session_factory() as db:
        user = await db.get(User, ctx.user.id)
        if not user:
            raise ApiError(404, "NOT_FOUND", "用户不存在")
        value = data.display_name.strip() if data.display_name else None
        if value is not None and not (1 <= len(value) <= 64):
            raise ApiError(422, "VALIDATION_ERROR", "显示名长度必须为 1–64 个字符")
        user.display_name = value
        await db.commit()
        await db.refresh(user)
        return {"user": user_out(user)}


@router.get("/api/devices")
async def devices(request: Request, ctx: AuthContext = Depends(require_auth)):
    async with request.app.state.db.session_factory() as db:
        rows = (
            await db.scalars(
                select(Device)
                .where(Device.user_id == ctx.user.id)
                .order_by(Device.last_seen_at.desc(), Device.id.desc())
            )
        ).all()
        return {
            "items": [
                DeviceOut(
                    id=d.id,
                    installation_id=d.installation_id,
                    name=d.name,
                    platform=d.platform,
                    client_version=d.client_version,
                    last_seen_at=d.last_seen_at,
                    created_at=d.created_at,
                    is_current=d.id == ctx.device_id,
                )
                for d in rows
            ]
        }
