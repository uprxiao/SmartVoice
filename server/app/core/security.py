from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from .config import AuthConfig


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_uuid() -> str:
    return str(uuid.uuid4())


def normalize_email(email: str) -> str:
    return email.strip().lower()


def password_hasher(config: AuthConfig) -> PasswordHasher:
    return PasswordHasher(
        memory_cost=config.argon2_memory_kib,
        time_cost=config.argon2_time_cost,
        parallelism=config.argon2_parallelism,
        hash_len=32,
        salt_len=16,
    )


def hash_password(config: AuthConfig, password: str) -> str:
    return password_hasher(config).hash(password)


def verify_password(config: AuthConfig, password: str, encoded: str) -> bool:
    try:
        return password_hasher(config).verify(encoded, password)
    except VerifyMismatchError:
        return False
    except Exception:
        return False


def create_access_token(
    config: AuthConfig,
    secret: str,
    user_id: str,
    sid: str,
    role: str,
    ttl: timedelta | None = None,
) -> tuple[str, datetime]:
    now = utcnow()
    expires = now + (ttl if ttl is not None else timedelta(seconds=config.access_ttl_seconds))
    payload = {
        "sub": user_id,
        "sid": sid,
        "role": role,
        "iss": config.issuer,
        "aud": config.audience,
        "iat": int(now.timestamp()),
        "exp": int(expires.timestamp()),
        "jti": str(uuid.uuid4()),
    }
    token = jwt.encode(payload, secret, algorithm="HS256")
    return token, expires


def decode_access_token(config: AuthConfig, secret: str, token: str) -> dict:
    return jwt.decode(
        token,
        secret,
        algorithms=["HS256"],
        issuer=config.issuer,
        audience=config.audience,
        options={"require": ["exp", "iat", "sub", "sid", "iss", "aud"]},
    )


def new_refresh_token() -> tuple[str, bytes]:
    token = secrets.token_urlsafe(48)
    return token, hashlib.sha256(token.encode("utf-8")).digest()


def refresh_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def canonical_json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def idempotency_hash(secret: str, operation: str, payload: dict) -> bytes:
    message = operation.encode("utf-8") + b"\n" + canonical_json(payload)
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest()


def safe_equal(left: bytes, right: bytes) -> bool:
    return hmac.compare_digest(left, right)
