"""Admin-managed provider configuration (V2 design §6.1).

Global deployment configuration: no ``user_id`` tenant column. The audit
table stores field names only, never field values.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    JSON,
    LargeBinary,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, UuidPk, utcnow


class ProviderConfig(Base, UuidPk):
    __tablename__ = "provider_configs"

    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(512))
    model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    params: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    secret_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    secret_key_id: Mapped[str | None] = mapped_column(String(32))
    secret_last4: Mapped[str | None] = mapped_column(String(4))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_test_reason: Mapped[str | None] = mapped_column(String(32))
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id", ondelete="RESTRICT"))

    __table_args__ = (
        UniqueConstraint("kind", "name", name="uq_provider_configs_kind_name"),
        CheckConstraint("kind in ('asr','llm')", name="ck_provider_configs_kind"),
        CheckConstraint("version >= 1", name="ck_provider_configs_version"),
        CheckConstraint(
            "(secret_ciphertext is null) = (secret_key_id is null)", name="ck_provider_configs_secret_key")
    )


class ProviderActive(Base):
    __tablename__ = "provider_active"

    kind: Mapped[str] = mapped_column(String(8), primary_key=True)
    # None: no admin choice yet, the YAML configuration is used.
    provider_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("provider_configs.id", ondelete="RESTRICT"))
    confirmed_cloud_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_cloud_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id", ondelete="RESTRICT"))
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id", ondelete="RESTRICT"))

    __table_args__ = (CheckConstraint("kind in ('asr','llm')", name="ck_provider_active_kind"),)


class ProviderAudit(Base, UuidPk):
    __tablename__ = "provider_audit"

    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    actor_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    # Not a foreign key: the audit row outlives a deleted configuration.
    provider_id: Mapped[str | None] = mapped_column(String(36))
    changed_fields: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    result: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(32))
    # Monotonic order for newest-first cursor pagination.
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "action in ('create','update','delete','test','activate','confirm_cloud')",
            name="ck_provider_audit_action"),
        CheckConstraint("result in ('ok','rejected','failed')", name="ck_provider_audit_result"),
        UniqueConstraint("seq", name="uq_provider_audit_seq"),
        Index("ix_provider_audit_at", "at", "seq"),
    )
