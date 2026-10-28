"""Read-only startup identities captured before independent recovery writes.

No online API, persistent generation, model data, or input text is included.
The caller serializes use of the original advisory connection with its watcher.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import bindparam, text

from ..models.session import ACTIVE_STATES


@dataclass(frozen=True, order=True)
class RecoveryIdentity:
    user_id: str
    resource_id: str


@dataclass(frozen=True, order=True)
class SessionRecoveryIdentity:
    user_id: str
    resource_id: str
    attempt_no: int
    state: str
    retry_cache_status: str


@dataclass(frozen=True)
class RecoverySnapshot:
    learning: tuple[RecoveryIdentity, ...]
    sessions: tuple[SessionRecoveryIdentity, ...]
    calls: tuple[RecoveryIdentity, ...]
    ownership_checked: bool


class RecoveryOwnershipLost(RuntimeError):
    def __init__(self):
        super().__init__("INSTANCE_LOCK_LOST")


_SNAPSHOT = text("""
WITH owner AS MATERIALIZED (
    SELECT pg_backend_pid() AS actual_pid,
        (NOT CAST(:require_lock AS boolean) OR (
            pg_backend_pid() = CAST(:pid AS integer) AND EXISTS (
                SELECT 1 FROM pg_locks
                WHERE locktype='advisory' AND granted AND mode='ExclusiveLock'
                  AND pid=pg_backend_pid()
                  AND database=(SELECT oid FROM pg_database WHERE datname=current_database())
                  AND classid::bigint=:high AND objid::bigint=:low AND objsubid=1
            )
        )) AS owned
)
SELECT 'ownership'::text AS kind, NULL::text AS user_id,
       NULL::text AS resource_id, NULL::integer AS attempt_no,
       NULL::text AS state, NULL::text AS retry_cache_status, actual_pid, owned
FROM owner
UNION ALL
SELECT 'learning', e.user_id::text, e.id::text, NULL::integer,
       NULL::text, NULL::text, owner.actual_pid, owner.owned
FROM learning_events e CROSS JOIN owner
WHERE owner.owned AND e.status='pending'
UNION ALL
SELECT 'session', s.user_id::text, s.id::text, s.current_attempt_no,
       s.state::text, s.retry_cache_status::text, owner.actual_pid, owner.owned
FROM sessions s CROSS JOIN owner
WHERE owner.owned AND (s.state IN :active_states OR s.retry_cache_status='available')
UNION ALL
SELECT 'call', c.user_id::text, c.id::text, NULL::integer,
       NULL::text, NULL::text, owner.actual_pid, owner.owned
FROM model_calls c CROSS JOIN owner
WHERE owner.owned AND c.status='running'
ORDER BY kind, user_id, resource_id
""").bindparams(bindparam("active_states", expanding=True))


async def capture_recovery_snapshot(connection, *, backend_pid=None, key=None,
                                    require_lock=True) -> RecoverySnapshot:
    """Publish identities only after one statement and its transaction commit.

    A changed PID/lock, invalidated connection, query/commit failure or external
    cancellation cannot fall back to another connection or an unrestricted scan.
    Explicit development skip-lock still freezes IDs, without ownership proof.
    """
    if connection is None or connection.closed or connection.invalidated:
        raise RecoveryOwnershipLost()
    if require_lock and (backend_pid is None or key is None):
        raise RecoveryOwnershipLost()
    unsigned = (key or 0) % (1 << 64)
    try:
        async with connection.begin():
            rows = (await connection.execute(_SNAPSHOT, {
                "require_lock": require_lock, "pid": backend_pid,
                "high": unsigned >> 32, "low": unsigned & 0xffffffff,
                "active_states": ACTIVE_STATES,
            })).mappings().all()
            owners = [row for row in rows if row["kind"] == "ownership"]
            if (len(owners) != 1 or owners[0]["owned"] is not True
                    or (require_lock and owners[0]["actual_pid"] != backend_pid)):
                raise RecoveryOwnershipLost()
            learning, sessions, calls = [], [], []
            for row in rows:
                if row["owned"] is not True:
                    raise RecoveryOwnershipLost()
                if row["kind"] == "learning":
                    learning.append(RecoveryIdentity(row["user_id"], row["resource_id"]))
                elif row["kind"] == "session":
                    sessions.append(SessionRecoveryIdentity(row["user_id"], row["resource_id"],
                        row["attempt_no"], row["state"], row["retry_cache_status"]))
                elif row["kind"] == "call":
                    calls.append(RecoveryIdentity(row["user_id"], row["resource_id"]))
                elif row["kind"] != "ownership":
                    raise RuntimeError("RECOVERY_SNAPSHOT_INVALID")
            snapshot = RecoverySnapshot(tuple(learning), tuple(sessions), tuple(calls), require_lock)
        # Do not return inside the context: a failed COMMIT must not publish it.
        return snapshot
    except RecoveryOwnershipLost:
        raise
    except Exception as error:
        if require_lock:
            raise RecoveryOwnershipLost() from error
        raise
