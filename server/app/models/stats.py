from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, UuidPk, utcnow


class ModelCall(Base, UuidPk):
    __tablename__ = "model_calls"

    user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    session_id: Mapped[str | None] = mapped_column(String(36))
    attempt_no: Mapped[int | None] = mapped_column(Integer)
    learning_event_id: Mapped[str | None] = mapped_column(String(36))
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    purpose: Mapped[str] = mapped_column(String(16), nullable=False)
    call_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    error_code: Mapped[str | None] = mapped_column(String(64))
    elapsed_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    processed_audio_samples: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    prompt_tokens: Mapped[int | None] = mapped_column(BigInteger)
    completion_tokens: Mapped[int | None] = mapped_column(BigInteger)
    aggregated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # V2: admin-managed provider row (None for YAML-configured providers) and
    # cloud ASR stream metadata (segment count, internal reconnects).
    provider_config_id: Mapped[str | None] = mapped_column(String(36))
    segments: Mapped[int | None] = mapped_column(Integer)
    reconnects: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_model_call_user", ondelete="RESTRICT"),
        ForeignKeyConstraint(["user_id", "session_id", "attempt_no"],
            ["session_attempts.user_id", "session_attempts.session_id", "session_attempts.attempt_no"],
            name="fk_model_call_attempt_tenant", ondelete="RESTRICT"),
        ForeignKeyConstraint(["user_id", "learning_event_id"],
            ["learning_events.user_id", "learning_events.id"], name="fk_model_call_learning_tenant", ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["user_id", "session_id"],
            ["sessions.user_id", "sessions.id"],
            name="fk_model_call_session",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "purpose in ('asr_decode','asr_finish','refine','audit','classify')",
            name="ck_model_call_purpose",
        ),
        CheckConstraint(
            "status in ('running','succeeded','failed','cancelled','unknown')",
            name="ck_model_call_status",
        ),
        Index("ix_model_calls_user_started", "user_id", "started_at"),
        Index("ix_model_calls_provider_model_purpose", "provider", "model_id", "purpose", "started_at"),
    )


class SessionContribution(Base):
    __tablename__ = "session_contributions"

    user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    received_samples: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    counted_session: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    has_result: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    char_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    successful_audio_samples: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    fallback: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    inserted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    __table_args__ = (
        ForeignKeyConstraint(["user_id", "session_id"],
            ["sessions.user_id", "sessions.id"], name="fk_contribution_session_tenant", ondelete="RESTRICT"),
        CheckConstraint(
            "outcome in ('active','ready','failed','empty','cancelled')",
            name="ck_contribution_outcome",
        ),
        CheckConstraint("received_samples >= 0", name="ck_contribution_samples"),
        CheckConstraint("char_count >= 0", name="ck_contribution_chars"),
    )


class UsageDaily(Base):
    __tablename__ = "usage_daily"

    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", name="fk_usage_daily_user", ondelete="RESTRICT"), primary_key=True)
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    sessions: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    received_audio_samples: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    result_sessions: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    failed_sessions: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    empty_sessions: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    cancelled_sessions: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    chars: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    successful_audio_samples: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    fallback_sessions: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    inserted_sessions: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    learning_events: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class ModelUsageDaily(Base):
    __tablename__ = "model_usage_daily"

    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", name="fk_model_usage_daily_user", ondelete="RESTRICT"), primary_key=True)
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), primary_key=True)
    model_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    purpose: Mapped[str] = mapped_column(String(16), primary_key=True)
    calls: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    successes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    failures: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    unknown_calls: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    elapsed_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    processed_audio_samples: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    prompt_tokens_known: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    completion_tokens_known: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    prompt_tokens_unknown_calls: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    completion_tokens_unknown_calls: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
