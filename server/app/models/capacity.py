from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, new_uuid, utcnow


class RuntimeCapacity(Base):
    __tablename__ = "runtime_capacity"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    max_active_sessions: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
    updated_by: Mapped[str | None] = mapped_column(String(36))

    __table_args__ = (
        CheckConstraint("id=1", name="ck_capacity_singleton"),
        CheckConstraint("max_active_sessions >= 1", name="ck_capacity_positive"),
    )


class CapacityChange(Base):
    __tablename__ = "capacity_changes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    actor_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )
    old_max_active: Mapped[int] = mapped_column(Integer, nullable=False)
    new_max_active: Mapped[int] = mapped_column(Integer, nullable=False)
    from_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    to_version: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    __table_args__ = (
        CheckConstraint("old_max_active >= 1", name="ck_capacity_change_old_positive"),
        CheckConstraint("new_max_active >= 1", name="ck_capacity_change_new_positive"),
        CheckConstraint(
            "old_max_active <> new_max_active", name="ck_capacity_change_actual"
        ),
        CheckConstraint(
            "to_version = from_version + 1", name="ck_capacity_change_version_step"
        ),
    )
