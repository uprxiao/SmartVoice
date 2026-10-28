"""capacity changes audit

Revision ID: 2b7c4f9a1d3e
Revises: 4d728107c202
Create Date: 2026-10-18 01:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "2b7c4f9a1d3e"
down_revision: Union[str, None] = "4d728107c202"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if _has_table("capacity_changes"):
        return
    op.create_table(
        "capacity_changes",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("actor_user_id", sa.String(length=36), nullable=False),
        sa.Column("old_max_active", sa.Integer(), nullable=False),
        sa.Column("new_max_active", sa.Integer(), nullable=False),
        sa.Column("from_version", sa.BigInteger(), nullable=False),
        sa.Column("to_version", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "old_max_active >= 1", name="ck_capacity_change_old_positive"
        ),
        sa.CheckConstraint(
            "new_max_active >= 1", name="ck_capacity_change_new_positive"
        ),
        sa.CheckConstraint(
            "old_max_active <> new_max_active", name="ck_capacity_change_actual"
        ),
        sa.CheckConstraint(
            "to_version = from_version + 1", name="ck_capacity_change_version_step"
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["users.id"], name="fk_capacity_changes_actor"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("to_version", name="uq_capacity_changes_to_version"),
    )


def downgrade() -> None:
    if _has_table("capacity_changes"):
        op.drop_table("capacity_changes")
