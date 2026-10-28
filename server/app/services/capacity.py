from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import Config
from ..models import CapacityChange, RuntimeCapacity, VoiceSession
from ..models.session import ACTIVE_STATES


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CapacityError(ValueError):
    pass


class CapacityService:
    def __init__(self, config: Config, db):
        self.config = config
        self.db = db
        self._cached = config.sessions.initial_max_active

    async def ensure_initialized(self) -> None:
        async with self.db.session_factory() as db:
            existing = await db.get(RuntimeCapacity, 1)
            if existing is None:
                db.add(
                    RuntimeCapacity(
                        id=1,
                        max_active_sessions=self.config.sessions.initial_max_active,
                        version=1,
                    )
                )
                await db.commit()
            else:
                if existing.max_active_sessions > self.config.effective_admission_ceiling:
                    raise CapacityError(
                        "runtime capacity exceeds deployment ceiling; fix via admin"
                    )
                self._cached = existing.max_active_sessions

    async def current(self) -> int:
        async with self.db.session_factory() as db:
            row = await db.get(RuntimeCapacity, 1)
            if row is None:
                raise CapacityError("capacity row missing")
            self._cached = row.max_active_sessions
            return self._cached

    async def active_count(self, db: AsyncSession) -> int:
        return int(
            await db.scalar(
                select(func.count(VoiceSession.id)).where(
                    VoiceSession.state.in_(ACTIVE_STATES)
                )
            )
            or 0
        )

    async def lock(self, db: AsyncSession):
        row = await db.scalar(
            select(RuntimeCapacity).where(RuntimeCapacity.id == 1).with_for_update()
        )
        if row is None:
            raise CapacityError("capacity row missing")
        return row

    async def admit(self, db: AsyncSession, *, locked_capacity=None) -> None:
        """Reserve an admission slot inside the caller's transaction."""
        # create/retry already take this row before user/session locks. Reuse
        # that same transaction's locked row rather than issuing the lock twice.
        row = locked_capacity if locked_capacity is not None else await self.lock(db)
        active = await self.active_count(db)
        if active >= row.max_active_sessions:
            raise CapacityError("capacity exceeded")

    async def update(self, db: AsyncSession, version: int, value: int, admin_id: str) -> RuntimeCapacity:
        if value < 1 or value > self.config.effective_admission_ceiling:
            raise CapacityError("value outside deployment ceiling")
        row = await db.scalar(
            select(RuntimeCapacity).where(RuntimeCapacity.id == 1).with_for_update()
        )
        if row is None:
            raise CapacityError("capacity row missing")
        if row.version != version:
            raise CapacityError("version conflict")
        if row.max_active_sessions == value:
            # Same value at the same version is a no-op: no version bump and
            # no audit row.
            return row
        change = CapacityChange(
            actor_user_id=admin_id,
            old_max_active=row.max_active_sessions,
            new_max_active=value,
            from_version=row.version,
            to_version=row.version + 1,
        )
        row.max_active_sessions = value
        row.version += 1
        row.updated_by = admin_id
        row.updated_at = utcnow()
        db.add(change)
        self._cached = value
        return row
