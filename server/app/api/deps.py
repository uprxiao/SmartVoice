from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import Header, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.security import decode_access_token, utcnow
from ..models import LoginSession, User


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict | None = None,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}
        self.headers = headers or {}


@dataclass
class AuthContext:
    user: User
    sid: str
    device_id: str | None
    client_type: str
    expires_at: datetime


def get_config(request: Request):
    return request.app.state.config


def get_state(request: Request):
    return request.app.state


async def db_session(request: Request) -> AsyncSession:
    async with request.app.state.db.session_factory() as session:
        yield session


def bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise ApiError(401, "AUTH_INVALID", "登录状态无效，请重新登录")
    return authorization[7:].strip()


async def authenticate(request: Request, authorization: str | None) -> AuthContext:
    config = request.app.state.config
    token = bearer_token(authorization)
    try:
        payload = decode_access_token(config.auth, config.jwt_secret, token)
    except Exception as exc:
        raise ApiError(401, "AUTH_INVALID", "登录状态无效，请重新登录") from exc
    sid = payload.get("sid")
    user_id = payload.get("sub")
    if not sid or not user_id:
        raise ApiError(401, "AUTH_INVALID", "登录状态无效，请重新登录")
    async with request.app.state.db.session_factory() as db:
        login = await db.get(LoginSession, sid)
        user = await db.get(User, user_id)
        if (
            not login
            or login.user_id != user_id
        ):
            raise ApiError(401, "AUTH_INVALID", "登录状态无效，请重新登录")
        if user is None:
            raise ApiError(401, "AUTH_INVALID", "登录状态无效，请重新登录")
        if user.status != "active":
            raise ApiError(403, "ACCOUNT_DISABLED", "账号已禁用，请联系管理员")
        if login.revoked_at is not None or login.refresh_expires_at <= utcnow():
            raise ApiError(401, "AUTH_INVALID", "登录状态无效，请重新登录")
        login.last_used_at = utcnow()
        await db.commit()
        expires_at = datetime.fromtimestamp(
            int(payload["exp"]), tz=timezone.utc
        )
        return AuthContext(
            user=user,
            sid=sid,
            device_id=login.device_id,
            client_type=login.client_type,
            expires_at=expires_at,
        )


def enforce_rate_limit(retry_after: int | None, message: str = "请求过于频繁，请稍后再试") -> None:
    if retry_after is not None:
        raise ApiError(429, "RATE_LIMITED", message, retryable=True,
                       details={"retry_after": retry_after},
                       headers={"Retry-After": str(retry_after)})


def enforce_authenticated_http_limit(request: Request, ctx: AuthContext) -> None:
    # authenticate() is shared with WS. Charge HTTP only, once per request,
    # after the route's role/client checks; do not trust X-Forwarded-For.
    if request.scope["type"] == "http" and not getattr(request.state, "user_rate_counted", False):
        enforce_rate_limit(request.app.state.authenticated_limiter.check(ctx.user.id))
        request.state.user_rate_counted = True


async def require_auth(
    request: Request, authorization: str | None = Header(default=None)
) -> AuthContext:
    ctx = await authenticate(request, authorization)
    enforce_authenticated_http_limit(request, ctx)
    return ctx


async def require_desktop(
    request: Request, authorization: str | None = Header(default=None)
) -> AuthContext:
    ctx = await authenticate(request, authorization)
    if ctx.client_type != "desktop" or not ctx.device_id:
        raise ApiError(403, "FORBIDDEN", "该操作只允许桌面客户端")
    enforce_authenticated_http_limit(request, ctx)
    return ctx


async def require_admin(
    request: Request, authorization: str | None = Header(default=None)
) -> AuthContext:
    ctx = await authenticate(request, authorization)
    if ctx.user.role != "admin":
        raise ApiError(403, "FORBIDDEN", "需要管理员权限")
    enforce_authenticated_http_limit(request, ctx)
    return ctx


def ensure_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
