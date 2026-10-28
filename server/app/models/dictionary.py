from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
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
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, UuidPk, utcnow


class DictionaryState(Base):
    __tablename__ = "dictionary_state"

    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True
    )
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class DictionaryTerm(Base, UuidPk):
    __tablename__ = "dictionary_terms"

    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    term: Mapped[str] = mapped_column(String(64), nullable=False)
    norm: Mapped[str] = mapped_column(String(64), nullable=False)
    aliases: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    contexts: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    scope: Mapped[str] = mapped_column(String(32), nullable=False, default="default")
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        UniqueConstraint("user_id", "id", name="uq_terms_user_id"),
        CheckConstraint("source in ('manual','auto')", name="ck_terms_source"),
        CheckConstraint("scope='default'", name="ck_terms_scope"),
        CheckConstraint("version >= 1", name="ck_terms_version"),
        Index(
            "uq_terms_user_norm_scope_active",
            "user_id",
            "norm",
            "scope",
            unique=True,
            postgresql_where=text("deleted_at is null"),
        ),
        Index("ix_terms_user_updated", "user_id", "updated_at"),
    )


class DictionaryImport(Base, UuidPk):
    __tablename__ = "dictionary_imports"

    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    request_key: Mapped[str] = mapped_column(String(36), nullable=False)
    file_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    total_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="processing")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("user_id", "id", name="uq_import_user_id"),
        UniqueConstraint("user_id", "request_key", name="uq_import_user_key"),
        CheckConstraint("state in ('processing','completed','interrupted')", name="ck_import_state"),
    )


class DictionaryImportRow(Base, UuidPk):
    __tablename__ = "dictionary_import_rows"

    user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    import_id: Mapped[str] = mapped_column(String(36), nullable=False)
    row_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    term_id: Mapped[str | None] = mapped_column(String(36))
    error_code: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        ForeignKeyConstraint(["user_id", "term_id"],
            ["dictionary_terms.user_id", "dictionary_terms.id"], name="fk_import_row_term_tenant", ondelete="RESTRICT"),
        UniqueConstraint("user_id", "import_id", "row_no", name="uq_import_row_no"),
        ForeignKeyConstraint(
            ["user_id", "import_id"],
            ["dictionary_imports.user_id", "dictionary_imports.id"],
            name="fk_import_row_import",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status in ('created','skipped','conflict','invalid')", name="ck_import_row_status"
        ),
        CheckConstraint("row_no >= 2", name="ck_import_row_no"),
    )
