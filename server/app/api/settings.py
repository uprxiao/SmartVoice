from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from ..models import User
from ..schemas.common import SettingsOut, SettingsPatchIn
from ..services import cleanup as cleanup_service
from .auth import settings_out
from .deps import ApiError, AuthContext, require_auth

router = APIRouter(prefix="/api/v1", tags=["settings"])


@router.get("/settings", response_model=SettingsOut)
async def get_settings(ctx: AuthContext = Depends(require_auth)):
    return settings_out(ctx.user)


@router.patch("/settings", response_model=SettingsOut)
async def patch_settings(
    data: SettingsPatchIn, request: Request, ctx: AuthContext = Depends(require_auth)
):
    async with request.app.state.db.session_factory() as db:
        user = await db.scalar(select(User).where(User.id == ctx.user.id).with_for_update()
                               .execution_options(populate_existing=True))
        if not user:
            raise ApiError(404, "NOT_FOUND", "用户不存在")
        if user.settings_version != data.version:
            raise ApiError(
                409,
                "VERSION_CONFLICT",
                "设置已变化，请刷新后重试",
                details={"current_version": user.settings_version},
            )
        retention = (
            data.history_retention_days
            if "history_retention_days" in data.model_fields_set
            else user.history_retention_days
        )
        if retention is not None and retention not in (7, 30, 90):
            raise ApiError(422, "VALIDATION_ERROR", "保留期限必须为 7/30/90 天或不设置")
        learning = (
            data.learning_enabled
            if "learning_enabled" in data.model_fields_set
            else user.learning_enabled
        )
        locale = (
            data.locale if "locale" in data.model_fields_set else user.locale
        )
        if locale not in ("zh-CN", "en"):
            raise ApiError(422, "VALIDATION_ERROR", "界面语言无效")
        previous_retention = user.history_retention_days
        user.history_retention_days = retention
        user.learning_enabled = bool(learning)
        user.locale = locale
        user.settings_version += 1
        # Policy and irreversible cleanup share the User -> Session transaction;
        # a simultaneous extension cannot revive content between two commits.
        if retention is not None and (
            previous_retention is None or retention < previous_retention
        ):
            await cleanup_service._cleanup_user(db, user, cleanup_service.utcnow())
        await db.commit()
        await db.refresh(user)
        return settings_out(user)
