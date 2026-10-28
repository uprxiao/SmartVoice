"""V2 cloud ASR runtime columns and 360 s sample limit

Revision ID: 9f3b1c7d5a20
Revises: 8c741a2d9e30
Create Date: 2026-10-27 23:40:00.000000

Idempotent (AGENTS §5.2): every DDL step checks catalog metadata first and the
sample constraint is verified after the change.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "9f3b1c7d5a20"
down_revision: Union[str, None] = "8c741a2d9e30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

V2_SAMPLES = "received_samples >= 0 and received_samples <= 5760000"
V1_SAMPLES = "received_samples >= 0 and received_samples <= 2880000"

COLUMNS = (
    ("sessions", sa.Column("asr_hints_applied", sa.Boolean(), nullable=False, server_default=sa.text("true"))),
    ("sessions", sa.Column("max_record_seconds", sa.Integer(), nullable=True)),
    ("session_attempts", sa.Column("asr_provider_id", sa.String(length=64), nullable=True)),
    ("session_attempts", sa.Column("asr_reconnects", sa.Integer(), nullable=True)),
    ("session_attempts", sa.Column("asr_resend_ms", sa.BigInteger(), nullable=True)),
    ("model_calls", sa.Column("provider_config_id", sa.String(length=36), nullable=True)),
    ("model_calls", sa.Column("segments", sa.Integer(), nullable=True)),
    ("model_calls", sa.Column("reconnects", sa.Integer(), nullable=True)),
)


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def _sample_check() -> str | None:
    return op.get_bind().scalar(sa.text(
        "select pg_get_constraintdef(oid) from pg_constraint "
        "where conname = 'ck_sessions_samples' and conrelid = 'sessions'::regclass"))


def _set_sample_limit(expression: str, limit: int) -> None:
    current = _sample_check()
    if current is None or f"<= {limit}" not in current:
        if current is not None:
            op.execute("alter table sessions drop constraint ck_sessions_samples")
        op.execute(f"alter table sessions add constraint ck_sessions_samples check ({expression})")
    verified = _sample_check()
    if verified is None or f"<= {limit}" not in verified:
        raise RuntimeError("ck_sessions_samples was not applied")


def upgrade() -> None:
    for table, column in COLUMNS:
        if column.name not in _columns(table):
            op.add_column(table, column.copy())
    missing = [f"{t}.{c.name}" for t, c in COLUMNS if c.name not in _columns(t)]
    if missing:
        raise RuntimeError(f"columns not added: {missing}")
    _set_sample_limit(V2_SAMPLES, 5760000)


def downgrade() -> None:
    # Rows above the V1 limit cannot satisfy the V1 constraint; refuse rather
    # than silently truncating sample counts.
    over = op.get_bind().scalar(sa.text("select count(*) from sessions where received_samples > 2880000"))
    if over:
        raise RuntimeError("sessions exceed the V1 180 s limit; downgrade refused")
    _set_sample_limit(V1_SAMPLES, 2880000)
    for table, column in reversed(COLUMNS):
        if column.name in _columns(table):
            op.drop_column(table, column.name)
