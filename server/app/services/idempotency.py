from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..api.deps import ApiError
from ..core.security import canonical_json, idempotency_hash, safe_equal
from ..models import OperationRequest, User
from ..schemas.common import normalize_uuid


def require_key(key: str | None) -> str:
    try:
        return normalize_uuid(key)
    except ValueError as exc:
        raise ApiError(422, "VALIDATION_ERROR", "Idempotency-Key 必须为 UUID") from exc


async def begin(
    db: AsyncSession,
    secret: str,
    user_id: str,
    operation: str,
    key: str,
    payload: dict,
    target_type: str,
    target_id: str,
) -> tuple[bool, str]:
    """Reserve an idempotency key inside the caller's transaction.

    Returns ``(replayed, target_id)``. A replay yields the originally recorded
    target identity, so the caller returns its current state instead of
    performing the side effect again.
    """
    digest = idempotency_hash(secret, operation, payload)
    # The unique index arbitrates simultaneous requests inside the caller's
    # transaction. A conflicting uncommitted insert is waited on, never exposed
    # as an IntegrityError / 500 or left as a committed reservation on rollback.
    inserted_id = await db.scalar(
        insert(OperationRequest)
        .values(
            user_id=user_id, operation=operation, request_key=key,
            request_hash=digest, target_type=target_type, target_id=target_id,
        )
        .on_conflict_do_nothing(constraint="uq_operation_user_op_key")
        .returning(OperationRequest.target_id)
    )
    if inserted_id is not None:
        return False, inserted_id
    existing = await db.scalar(
        select(OperationRequest).where(
            OperationRequest.user_id == user_id,
            OperationRequest.operation == operation,
            OperationRequest.request_key == key,
        ).execution_options(populate_existing=True)
    )
    if existing is None:
        raise RuntimeError("Idempotency conflict has no committed record")
    # For operations addressing an existing resource, the path ID is part of
    # request identity even if a caller's JSON payload has no such field.
    creates_resource = operation in {
        "dictionary.term.create", "dictionary.import", "admin.user.create",
    }
    if (
        not safe_equal(existing.request_hash, digest)
        or existing.target_type != target_type
        or (not creates_resource and existing.target_id != target_id)
    ):
        raise ApiError(409, "IDEMPOTENCY_CONFLICT", "相同幂等键对应了不同请求")
    return True, existing.target_id


async def lock_user(db: AsyncSession, user_id: str) -> User:
    """First row lock for user-scoped writes; callers then lock session/terms.

    Capacity reservation, if needed, is acquired before this lock. No network
    or model call may run while this transaction is open.
    """
    user = await db.scalar(
        select(User).where(User.id == user_id).with_for_update()
        .execution_options(populate_existing=True)
    )
    if user is None:
        raise ApiError(404, "NOT_FOUND", "用户不存在")
    if user.status != "active":
        raise ApiError(403, "ACCOUNT_DISABLED", "账号已禁用")
    return user


def payload_digest(secret: str, operation: str, payload: dict) -> bytes:
    return idempotency_hash(secret, operation, payload)


def canonical(payload: dict) -> bytes:
    return canonical_json(payload)
