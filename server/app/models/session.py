from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, UuidPk, utcnow

ACTIVE_STATES = ("created", "streaming", "finalizing", "refining")
TERMINAL_STATES = ("ready", "empty", "failed", "cancelled")


class VoiceSession(Base, UuidPk):
    __tablename__ = "sessions"

    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    device_id: Mapped[str] = mapped_column(
        String(36), nullable=False
    )
    created_login_sid: Mapped[str] = mapped_column(String(36), nullable=False)
    client_request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    request_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="dictation")
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="created")
    current_attempt_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    current_result_id: Mapped[str | None] = mapped_column(String(36))
    dictionary_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    learning_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    received_samples: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    audio_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    end_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    content_cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_asr_failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_cache_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="none"
    )
    error_code: Mapped[str | None] = mapped_column(String(64))
    asr_hints: Mapped[list | None] = mapped_column(JSON)
    llm_hints: Mapped[list | None] = mapped_column(JSON)
    # V2: whether ASR actually received the hint terms (cloud ASR: false,
    # FR-02-11) and the recording limit fixed when the session was created.
    asr_hints_applied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True,
                                                    server_default=text("true"))
    max_record_seconds: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        ForeignKeyConstraint(["user_id", "device_id"],
            ["devices.user_id", "devices.id"], name="fk_session_device_tenant", ondelete="RESTRICT"),
        ForeignKeyConstraint(["user_id", "created_login_sid"],
            ["login_sessions.user_id", "login_sessions.id"], name="fk_session_login_tenant", ondelete="RESTRICT"),
        ForeignKeyConstraint(["user_id", "id", "current_result_id"],
            ["results.user_id", "results.session_id", "results.id"],
            name="fk_session_current_result", ondelete="RESTRICT", use_alter=True,
            deferrable=True, initially="DEFERRED"),
        UniqueConstraint("user_id", "id", name="uq_sessions_user_id"),
        UniqueConstraint("user_id", "client_request_id", name="uq_sessions_client_request"),
        CheckConstraint("mode='dictation'", name="ck_sessions_mode"),
        CheckConstraint(
            "state in ('created','streaming','finalizing','refining','ready','empty','failed','cancelled')",
            name="ck_sessions_state",
        ),
        CheckConstraint("received_samples >= 0 and received_samples <= 5760000",
                        name="ck_sessions_samples"),
        CheckConstraint("current_attempt_no >= 1", name="ck_sessions_attempt_no"),
        Index("ix_sessions_user_created", "user_id", "created_at", "id"),
        Index("ix_sessions_state", "state"),
        Index("ix_sessions_first_terminal", "first_terminal_at"),
        Index(
            "uq_sessions_active_user",
            "user_id",
            unique=True,
            postgresql_where=text(
                "state in ('created','streaming','finalizing','refining')"
            ),
        ),
    )

    attempts: Mapped[list["SessionAttempt"]] = relationship(
        back_populates="session", order_by="SessionAttempt.attempt_no"
    )


class SessionAttempt(Base, UuidPk):
    __tablename__ = "session_attempts"

    user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    source_attempt_no: Mapped[int | None] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    raw_text: Mapped[str | None] = mapped_column(Text)
    asr_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    refine_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    stop_to_result_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # V2 §3.8 diagnostics: ASR provider used and cloud recovery counters.
    asr_provider_id: Mapped[str | None] = mapped_column(String(64))
    asr_reconnects: Mapped[int | None] = mapped_column(Integer)
    asr_resend_ms: Mapped[int | None] = mapped_column(BigInteger)

    __table_args__ = (
        UniqueConstraint("user_id", "session_id", "attempt_no", name="uq_attempt_user_session_no"),
        ForeignKeyConstraint(
            ["user_id", "session_id"],
            ["sessions.user_id", "sessions.id"],
            name="fk_attempt_session",
            ondelete="RESTRICT",
        ),
        CheckConstraint("kind in ('initial','asr','refine')", name="ck_attempt_kind"),
        CheckConstraint(
            "state in ('running','ready','empty','failed','cancelled')", name="ck_attempt_state"
        ),
        CheckConstraint("attempt_no >= 1", name="ck_attempt_no"),
    )

    session: Mapped[VoiceSession] = relationship(back_populates="attempts")


class SessionResult(Base, UuidPk):
    __tablename__ = "results"

    user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_text: Mapped[str | None] = mapped_column(Text)
    refined_text: Mapped[str | None] = mapped_column(Text)
    review_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    review_reason: Mapped[str | None] = mapped_column(String(64))
    text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    dictionary_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    char_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    stop_to_result_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    content_cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("user_id", "session_id", "id", name="uq_result_user_session_id"),
        UniqueConstraint("user_id", "session_id", "attempt_no", name="uq_result_attempt"),
        ForeignKeyConstraint(
            ["user_id", "session_id", "attempt_no"],
            ["session_attempts.user_id", "session_attempts.session_id", "session_attempts.attempt_no"],
            name="fk_result_attempt",
            ondelete="RESTRICT",
        ),
        CheckConstraint("review_required or review_reason is null", name="ck_result_review_reason"),
        CheckConstraint("char_count >= 0", name="ck_result_char_count"),
    )


class Insertion(Base, UuidPk):
    __tablename__ = "insertions"

    user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False)
    result_id: Mapped[str] = mapped_column(String(36), nullable=False)
    device_id: Mapped[str] = mapped_column(String(36), nullable=False)
    text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    review_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="reserved")
    observable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    request_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    claimed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("user_id", "id", name="uq_insertion_user_id"),
        UniqueConstraint("user_id", "session_id", "id", name="uq_insertion_user_session_id"),
        ForeignKeyConstraint(
            ["user_id", "session_id", "result_id"],
            ["results.user_id", "results.session_id", "results.id"],
            name="fk_insertion_result",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["user_id", "device_id"],
            ["devices.user_id", "devices.id"],
            name="fk_insertion_device",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status in ('reserved','inserted','failed','unverified')", name="ck_insertion_status"
        ),
        CheckConstraint(
            "observable=false or status='inserted'", name="ck_insertion_observable"
        ),
        CheckConstraint(
            "(status='reserved' and completed_at is null) or "
            "(status<>'reserved' and completed_at is not null)",
            name="ck_insertion_completed",
        ),
        CheckConstraint(
            "status<>'inserted' or error_code is null", name="ck_insertion_inserted_error"
        ),
        Index(
            "uq_insertion_active_result",
            "user_id",
            "result_id",
            unique=True,
            postgresql_where=text(
                "status in ('reserved','inserted','unverified')"
            ),
        ),
    )


class OperationRequest(Base, UuidPk):
    __tablename__ = "operation_requests"

    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    request_key: Mapped[str] = mapped_column(String(36), nullable=False)
    request_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id", "operation", "request_key", name="uq_operation_user_op_key"
        ),
    )
