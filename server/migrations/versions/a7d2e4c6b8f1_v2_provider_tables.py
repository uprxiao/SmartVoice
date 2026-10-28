"""V2 admin-managed provider tables

Revision ID: a7d2e4c6b8f1
Revises: 9f3b1c7d5a20
Create Date: 2026-10-28 00:30:00.000000

Global configuration (no user_id). Idempotent: tables are created only when
missing; the two provider_active rows are seeded with ``where not exists``
and verified afterwards.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a7d2e4c6b8f1"
down_revision: Union[str, None] = "9f3b1c7d5a20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if not _has_table("provider_configs"):
        op.create_table(
            "provider_configs",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("kind", sa.String(8), nullable=False),
            sa.Column("name", sa.String(64), nullable=False),
            sa.Column("type", sa.String(32), nullable=False),
            sa.Column("base_url", sa.String(512)),
            sa.Column("model_id", sa.String(128), nullable=False),
            sa.Column("params", sa.JSON(), nullable=False),
            sa.Column("secret_ciphertext", sa.LargeBinary()),
            sa.Column("secret_key_id", sa.String(32)),
            sa.Column("secret_last4", sa.String(4)),
            sa.Column("enabled", sa.Boolean(), nullable=False),
            sa.Column("last_test_at", sa.DateTime(timezone=True)),
            sa.Column("last_test_ok", sa.Boolean()),
            sa.Column("last_test_reason", sa.String(32)),
            sa.Column("version", sa.BigInteger(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_by", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT")),
            sa.UniqueConstraint("kind", "name", name="uq_provider_configs_kind_name"),
            sa.CheckConstraint("kind in ('asr','llm')", name="ck_provider_configs_kind"),
            sa.CheckConstraint("version >= 1", name="ck_provider_configs_version"),
            sa.CheckConstraint("(secret_ciphertext is null) = (secret_key_id is null)",
                               name="ck_provider_configs_secret_key"),
        )
    if not _has_table("provider_active"):
        op.create_table(
            "provider_active",
            sa.Column("kind", sa.String(8), primary_key=True),
            sa.Column("provider_id", sa.String(36), sa.ForeignKey("provider_configs.id", ondelete="RESTRICT")),
            sa.Column("confirmed_cloud_at", sa.DateTime(timezone=True)),
            sa.Column("confirmed_cloud_by", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT")),
            sa.Column("version", sa.BigInteger(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_by", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT")),
            sa.CheckConstraint("kind in ('asr','llm')", name="ck_provider_active_kind"),
        )
    if not _has_table("provider_audit"):
        op.create_table(
            "provider_audit",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("actor_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("action", sa.String(16), nullable=False),
            sa.Column("kind", sa.String(8), nullable=False),
            sa.Column("provider_id", sa.String(36)),
            sa.Column("changed_fields", sa.JSON(), nullable=False),
            sa.Column("result", sa.String(16), nullable=False),
            sa.Column("reason", sa.String(32)),
            sa.Column("seq", sa.BigInteger(), sa.Identity(), nullable=False),
            sa.CheckConstraint(
                "action in ('create','update','delete','test','activate','confirm_cloud')",
                name="ck_provider_audit_action"),
            sa.CheckConstraint("result in ('ok','rejected','failed')", name="ck_provider_audit_result"),
            sa.UniqueConstraint("seq", name="uq_provider_audit_seq"),
        )
        op.create_index("ix_provider_audit_at", "provider_audit", ["at", "seq"])
    for kind in ("asr", "llm"):
        op.execute(sa.text(
            "insert into provider_active (kind, provider_id, version, updated_at) "
            "select :kind, null, 1, now() where not exists (select 1 from provider_active where kind = :kind)"
        ).bindparams(kind=kind))
    rows = op.get_bind().execute(sa.text("select kind from provider_active order by kind")).scalars().all()
    if rows != ["asr", "llm"]:
        raise RuntimeError("provider_active must hold exactly one row per kind")


def downgrade() -> None:
    for table in ("provider_audit", "provider_active", "provider_configs"):
        if _has_table(table):
            op.drop_table(table)
