from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, Timestamps, UuidPk, utcnow


class User(Base, UuidPk, Timestamps):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(254), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    locale: Mapped[str] = mapped_column(String(16), nullable=False, default="zh-CN")
    history_retention_days: Mapped[int | None] = mapped_column(Integer)
    learning_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    settings_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("role in ('user','admin')", name="ck_users_role"),
        CheckConstraint("status in ('active','disabled')", name="ck_users_status"),
        CheckConstraint(
            "history_retention_days is null or history_retention_days in (7,30,90)",
            name="ck_users_retention",
        ),
        CheckConstraint("locale in ('zh-CN','en')", name="ck_users_locale"),
    )

    devices: Mapped[list["Device"]] = relationship(back_populates="user")
    login_sessions: Mapped[list["LoginSession"]] = relationship(back_populates="user")


class Device(Base, UuidPk, Timestamps):
    __tablename__ = "devices"

    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    installation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    platform: Mapped[str] = mapped_column(String(16), nullable=False)
    client_version: Mapped[str] = mapped_column(String(32), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        UniqueConstraint("user_id", "installation_id", name="uq_devices_user_installation"),
        UniqueConstraint("user_id", "id", name="uq_devices_user_id"),
        CheckConstraint("platform in ('macos','windows')", name="ck_devices_platform"),
        Index("ix_devices_user_last_seen", "user_id", "last_seen_at", "id"),
    )

    user: Mapped[User] = relationship(back_populates="devices")


class LoginSession(Base, UuidPk):
    __tablename__ = "login_sessions"

    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    device_id: Mapped[str | None] = mapped_column(
        String(36)
    )
    client_type: Mapped[str] = mapped_column(String(16), nullable=False)
    refresh_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False, unique=True)
    refresh_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    __table_args__ = (
        UniqueConstraint("user_id", "id", name="uq_login_user_id"),
        ForeignKeyConstraint(["user_id", "device_id"],
            ["devices.user_id", "devices.id"], name="fk_login_device_tenant", ondelete="RESTRICT"),
        CheckConstraint("client_type in ('desktop','admin_web')", name="ck_login_client_type"),
        CheckConstraint(
            "(client_type='desktop' and device_id is not null) or "
            "(client_type='admin_web' and device_id is null)",
            name="ck_login_device_presence",
        ),
        Index("ix_login_user_device_revoked", "user_id", "device_id", "revoked_at"),
        Index("ix_login_refresh_expires", "refresh_expires_at"),
    )

    user: Mapped[User] = relationship(back_populates="login_sessions")
