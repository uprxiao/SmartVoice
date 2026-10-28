from __future__ import annotations

from datetime import timedelta

from app.core.config import AuthConfig
from app.core.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    idempotency_hash,
    new_refresh_token,
    normalize_email,
    refresh_hash,
    utcnow,
    verify_password,
)


def test_normalize_email():
    assert normalize_email("  User@Example.COM ") == "user@example.com"


def test_password_hash_roundtrip():
    config = AuthConfig()
    digest = hash_password(config, "SmokePass123")
    assert digest.startswith("$argon2")
    assert verify_password(config, "SmokePass123", digest)
    assert not verify_password(config, "WrongPass123", digest)


def test_access_token_roundtrip_and_expiry():
    config = AuthConfig()
    secret = "test-secret-" + "x" * 32
    token, expires_at = create_access_token(
        config, secret, "user-id", "session-id", "user"
    )
    payload = decode_access_token(config, secret, token)
    assert payload["sub"] == "user-id"
    assert payload["sid"] == "session-id"
    assert payload["role"] == "user"
    assert expires_at > utcnow()

    expired, _ = create_access_token(
        config, secret, "user-id", "session-id", "user", ttl=timedelta(seconds=-5)
    )
    try:
        decode_access_token(config, secret, expired)
    except Exception:
        pass
    else:  # pragma: no cover
        raise AssertionError("expired token was accepted")


def test_idempotency_hash_is_stable_and_operation_scoped():
    assert idempotency_hash("k", "op", {"a": 1}) == idempotency_hash("k", "op", {"a": 1})
    assert idempotency_hash("k", "op", {"a": 1}) != idempotency_hash("k", "op2", {"a": 1})
    assert idempotency_hash("k", "op", {"a": 1}) != idempotency_hash("k2", "op", {"a": 1})


def test_refresh_tokens_are_unique_and_hashed():
    first, first_hash = new_refresh_token()
    second, second_hash = new_refresh_token()
    assert first != second
    assert first_hash == refresh_hash(first)
    assert first_hash != first
    assert first_hash != second_hash
