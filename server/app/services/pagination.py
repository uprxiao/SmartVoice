from __future__ import annotations

import base64
import hashlib
import hmac
import json

from ..api.deps import ApiError


def _sign(secret: str, scope: str, body: bytes) -> bytes:
    return hmac.new(
        secret.encode("utf-8"), scope.encode("utf-8") + b"\n" + body, hashlib.sha256
    ).digest()[:16]


def encode_cursor(secret: str, scope: str, payload: dict) -> str:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return base64.urlsafe_b64encode(_sign(secret, scope, body) + body).decode("ascii").rstrip("=")


def decode_cursor(secret: str, scope: str, token: str) -> dict:
    if not token or len(token) > 2048:
        raise ApiError(422, "CURSOR_INVALID", "分页游标无效")
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        signature, body = raw[:16], raw[16:]
        if not hmac.compare_digest(signature, _sign(secret, scope, body)):
            raise ValueError("signature")
        value = json.loads(body)
        if not isinstance(value, dict):
            raise ValueError("shape")
        return value
    except Exception as exc:
        raise ApiError(422, "CURSOR_INVALID", "分页游标无效") from exc


def clamp_limit(limit: int | None) -> int:
    return 50 if limit is None else max(1, min(100, limit))
