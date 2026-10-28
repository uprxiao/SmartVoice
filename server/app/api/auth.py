from __future__ import annotations

import asyncio
from datetime import timedelta

from fastapi import APIRouter, Header, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..core.security import (
    create_access_token,
    hash_password,
    new_refresh_token,
    normalize_email,
    refresh_hash,
    utcnow,
    verify_password,
)
from ..models import Device, DictionaryState, LoginSession, User
from ..schemas.common import (
    DeviceIn,
    LoginIn,
    RefreshIn,
    RegisterIn,
    SettingsOut,
    TokenPair,
    UserOut,
)
from .deps import ApiError, AuthContext, db_session, enforce_rate_limit, require_auth

router = APIRouter(prefix="/api/auth", tags=["auth"])


def user_out(user: User) -> UserOut:
    return UserOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        status=user.status,
        locale=user.locale,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
    )


def settings_out(user: User) -> SettingsOut:
    return SettingsOut(
        history_retention_days=user.history_retention_days,
        learning_enabled=user.learning_enabled,
        locale=user.locale,
        version=user.settings_version,
    )


def validate_password(password: str) -> None:
    length = len(password)
    if length < 8 or length > 128:
        raise ApiError(422, "VALIDATION_ERROR", "密码长度必须为 8–128 个字符")


def validate_email(email: str) -> str:
    normalized = normalize_email(email)
    if len(normalized) > 254 or "@" not in normalized or normalized.startswith("@"):
        raise ApiError(422, "VALIDATION_ERROR", "邮箱格式无效")
    return normalized


async def password_work(request: Request, function, *args):
    # Hashing/verification may consume 64 MiB each and must never block ASGI or
    # outlive a released permit when an HTTP client disconnects.
    async with request.app.state.password_slots:
        task = asyncio.create_task(asyncio.to_thread(function, request.app.state.config.auth, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
            if not task.cancelled():
                task.exception()
            raise


@router.post("/register", response_model=UserOut, status_code=201)
async def register(data: RegisterIn, request: Request):
    email = validate_email(data.email)
    validate_password(data.password)
    ip = request.client.host if request.client else "unknown"
    enforce_rate_limit(request.app.state.register_limiter.check(ip))
    encoded = await password_work(request, hash_password, data.password)
    async with request.app.state.db.session_factory() as db:
        if await db.scalar(select(User.id).where(User.email == email)):
            raise ApiError(409, "EMAIL_EXISTS", "该邮箱已注册")
        user = User(email=email, password_hash=encoded, role="user", status="active")
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
        return user_out(user)


@router.post("/login", response_model=TokenPair)
async def login(data: LoginIn, request: Request):
    email = validate_email(data.email)
    config = request.app.state.config
    if data.client_type not in ("desktop", "admin_web"):
        raise ApiError(422, "VALIDATION_ERROR", "客户端类型无效")
    if data.client_type == "desktop":
        if not data.device:
            raise ApiError(422, "VALIDATION_ERROR", "桌面登录必须提供设备信息")
        if data.device.platform not in ("macos", "windows"):
            raise ApiError(422, "VALIDATION_ERROR", "设备平台无效")
    elif data.device is not None:
        raise ApiError(422, "VALIDATION_ERROR", "管理后台登录不允许设备信息")
    limiter = request.app.state.login_limiter
    ip = request.client.host if request.client else "unknown"
    retry_after = limiter.check(ip, email)
    enforce_rate_limit(retry_after, "登录尝试过于频繁，请稍后再试")
    async with request.app.state.db.session_factory() as db:
        candidate = await db.scalar(select(User).where(User.email == email))
        user_id = candidate.id if candidate else None
        encoded = candidate.password_hash if candidate else request.app.state.dummy_password_hash
    valid = await password_work(request, verify_password, data.password, encoded)
    if not valid or user_id is None:
        raise ApiError(401, "AUTH_INVALID", "邮箱或密码错误")
    async with request.app.state.db.session_factory() as db:
        user = await db.scalar(select(User).where(User.id == user_id).with_for_update())
        # Recheck after the expensive operation: disable/password reset may have
        # committed while hashing, and must win against creating another sid.
        if user is None or user.password_hash != encoded:
            raise ApiError(401, "AUTH_INVALID", "邮箱或密码错误")
        if user.status != "active":
            raise ApiError(403, "ACCOUNT_DISABLED", "账号已禁用，请联系管理员")
        if data.client_type == "admin_web" and user.role != "admin":
            raise ApiError(403, "FORBIDDEN", "需要管理员权限")
        device = None
        if data.client_type == "desktop":
            device = await db.scalar(select(Device).where(Device.user_id == user.id,
                Device.installation_id == data.device.installation_id).with_for_update())
            now = utcnow()
            if device:
                device.name, device.platform = data.device.name, data.device.platform
                device.client_version, device.last_seen_at = data.device.client_version, now
            else:
                device = Device(user_id=user.id, installation_id=data.device.installation_id,
                    name=data.device.name, platform=data.device.platform,
                    client_version=data.device.client_version, last_seen_at=now)
                db.add(device)
                await db.flush()
            old = await db.scalars(select(LoginSession).where(LoginSession.user_id == user.id,
                LoginSession.device_id == device.id, LoginSession.revoked_at.is_(None))
                .order_by(LoginSession.id).with_for_update())
            for item in old:
                item.revoked_at = now
        refresh_token, digest = new_refresh_token()
        login_session = LoginSession(user_id=user.id, device_id=device.id if device else None,
            client_type=data.client_type, refresh_hash=digest,
            refresh_expires_at=utcnow() + timedelta(seconds=config.auth.refresh_ttl_seconds),
            last_used_at=utcnow())
        db.add(login_session)
        user.last_login_at = utcnow()
        await db.flush()
        access, _ = create_access_token(config.auth, config.jwt_secret, user.id, login_session.id, user.role)
        pair = TokenPair(access_token=access, refresh_token=refresh_token,
            expires_in=config.auth.access_ttl_seconds, refresh_expires_at=login_session.refresh_expires_at,
            user=user_out(user))
        await db.commit()
        return pair


@router.post("/refresh", response_model=TokenPair)
async def refresh(data: RefreshIn, request: Request):
    config = request.app.state.config
    digest = refresh_hash(data.refresh_token)
    async with request.app.state.db.session_factory() as db:
        candidate = await db.scalar(select(LoginSession).where(LoginSession.refresh_hash == digest))
        if candidate is None:
            raise ApiError(401, "AUTH_INVALID", "登录已过期，请重新登录")
        user_id, sid = candidate.user_id, candidate.id
    async with request.app.state.db.session_factory() as db:
        user = await db.scalar(select(User).where(User.id == user_id).with_for_update())
        login_session = await db.scalar(select(LoginSession).where(LoginSession.id == sid,
            LoginSession.user_id == user_id, LoginSession.refresh_hash == digest).with_for_update())
        if user is None or login_session is None:
            raise ApiError(401, "AUTH_INVALID", "登录已过期，请重新登录")
        if user.status != "active":
            raise ApiError(403, "ACCOUNT_DISABLED", "账号已禁用，请联系管理员")
        if login_session.revoked_at is not None or login_session.refresh_expires_at <= utcnow():
            raise ApiError(401, "AUTH_INVALID", "登录已过期，请重新登录")
        enforce_rate_limit(request.app.state.refresh_limiter.check(login_session.id))
        login_session.last_used_at = utcnow()
        access, _ = create_access_token(config.auth, config.jwt_secret, user.id, login_session.id, user.role)
        pair = TokenPair(access_token=access, refresh_token=data.refresh_token,
            expires_in=config.auth.access_ttl_seconds, refresh_expires_at=login_session.refresh_expires_at,
            user=user_out(user))
        await db.commit()
        return pair


@router.post("/logout", status_code=204)
async def logout(request: Request, authorization: str | None = Header(default=None)):
    ctx = await require_auth(request, authorization)
    async with request.app.state.db.session_factory() as db:
        await db.scalar(select(User.id).where(User.id == ctx.user.id).with_for_update())
        login_session = await db.scalar(select(LoginSession).where(
            LoginSession.id == ctx.sid, LoginSession.user_id == ctx.user.id).with_for_update())
        if login_session and login_session.revoked_at is None:
            login_session.revoked_at = utcnow()
            await db.commit()
    return None
