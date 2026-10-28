from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, UuidPk, utcnow


class LearningEvent(Base, UuidPk):
    __tablename__ = "learning_events"

    user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False)
    insertion_id: Mapped[str] = mapped_column(String(36), nullable=False)
    correction_id: Mapped[str] = mapped_column(String(36), nullable=False)
    request_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    offset: Mapped[int] = mapped_column("char_offset", Integer, nullable=False)
    before_text: Mapped[str | None] = mapped_column(String(64))
    after_text: Mapped[str | None] = mapped_column(String(64))
    left_context: Mapped[str | None] = mapped_column(String(200))
    right_context: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    reason: Mapped[str | None] = mapped_column(String(64))
    classification: Mapped[str | None] = mapped_column(String(16))
    ambiguous: Mapped[bool | None] = mapped_column(Boolean)
    evidence: Mapped[dict | None] = mapped_column(JSON)
    expected_term_id: Mapped[str | None] = mapped_column(String(36))
    expected_term_version: Mapped[int | None] = mapped_column(Integer)
    term_id: Mapped[str | None] = mapped_column(String(36))
    term_before: Mapped[dict | None] = mapped_column(JSON)
    after_version: Mapped[int | None] = mapped_column(Integer)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    undone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    content_cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    __table_args__ = (
        UniqueConstraint("user_id", "id", name="uq_learning_user_id"),
        ForeignKeyConstraint(["user_id", "session_id", "insertion_id"],
            ["insertions.user_id", "insertions.session_id", "insertions.id"],
            name="fk_learning_session_insertion", ondelete="RESTRICT"),
        ForeignKeyConstraint(["user_id", "term_id"],
            ["dictionary_terms.user_id", "dictionary_terms.id"], name="fk_learning_term_tenant", ondelete="RESTRICT"),
        ForeignKeyConstraint(["user_id", "expected_term_id"],
            ["dictionary_terms.user_id", "dictionary_terms.id"], name="fk_learning_expected_term_tenant", ondelete="RESTRICT"),
        UniqueConstraint("user_id", "correction_id", name="uq_learning_correction"),
        ForeignKeyConstraint(
            ["user_id", "session_id"],
            ["sessions.user_id", "sessions.id"],
            name="fk_learning_session",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["user_id", "insertion_id"],
            ["insertions.user_id", "insertions.id"],
            name="fk_learning_insertion",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status in ('pending','learned','suggested','ignored','failed')",
            name="ck_learning_status",
        ),
        CheckConstraint(
            "classification is null or classification in ('term','suggest','ignore')",
            name="ck_learning_classification",
        ),
        CheckConstraint("char_offset >= 0", name="ck_learning_offset"),
        Index("ix_learning_user_created", "user_id", "created_at"),
        Index("ix_learning_status", "status"),
    )
