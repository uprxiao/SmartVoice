"""Constrain tenant-owned references and current result identity.

Revision ID: 8c741a2d9e30
Revises: 2b7c4f9a1d3e

Existing inconsistent rows intentionally fail the transaction. No content is
silently deleted or reassigned to make the constraint validation pass.
"""
from alembic import op
import sqlalchemy as sa

revision = "8c741a2d9e30"
down_revision = "2b7c4f9a1d3e"
branch_labels = None
depends_on = None

UNIQUES = (
    ("login_sessions", "uq_login_user_id", ["user_id", "id"]),
    ("learning_events", "uq_learning_user_id", ["user_id", "id"]),
)
FKS = (
    ("login_sessions", "fk_login_device_tenant", ["user_id", "device_id"], "devices", ["user_id", "id"]),
    ("sessions", "fk_session_device_tenant", ["user_id", "device_id"], "devices", ["user_id", "id"]),
    ("sessions", "fk_session_login_tenant", ["user_id", "created_login_sid"], "login_sessions", ["user_id", "id"]),
    ("sessions", "fk_session_current_result", ["user_id", "id", "current_result_id"], "results", ["user_id", "session_id", "id"]),
    ("learning_events", "fk_learning_session_insertion", ["user_id", "session_id", "insertion_id"], "insertions", ["user_id", "session_id", "id"]),
    ("learning_events", "fk_learning_term_tenant", ["user_id", "term_id"], "dictionary_terms", ["user_id", "id"]),
    ("learning_events", "fk_learning_expected_term_tenant", ["user_id", "expected_term_id"], "dictionary_terms", ["user_id", "id"]),
    ("dictionary_import_rows", "fk_import_row_term_tenant", ["user_id", "term_id"], "dictionary_terms", ["user_id", "id"]),
    ("model_calls", "fk_model_call_user", ["user_id"], "users", ["id"]),
    ("model_calls", "fk_model_call_attempt_tenant", ["user_id", "session_id", "attempt_no"], "session_attempts", ["user_id", "session_id", "attempt_no"]),
    ("model_calls", "fk_model_call_learning_tenant", ["user_id", "learning_event_id"], "learning_events", ["user_id", "id"]),
    ("session_contributions", "fk_contribution_session_tenant", ["user_id", "session_id"], "sessions", ["user_id", "id"]),
    ("usage_daily", "fk_usage_daily_user", ["user_id"], "users", ["id"]),
    ("model_usage_daily", "fk_model_usage_daily_user", ["user_id"], "users", ["id"]),
)


def upgrade() -> None:
    for table, name, columns in UNIQUES:
        existing = {item["name"]: item for item in sa.inspect(op.get_bind()).get_unique_constraints(table)}
        if name in existing:
            if existing[name]["column_names"] != columns:
                raise RuntimeError(f"Existing unique constraint differs: {table}.{name}")
        else:
            op.create_unique_constraint(name, table, columns)
    for table, name, columns, target, target_columns in FKS:
        existing = {item["name"]: item for item in sa.inspect(op.get_bind()).get_foreign_keys(table)}
        if name in existing:
            item = existing[name]
            if (item["constrained_columns"] != columns or item["referred_table"] != target
                    or item["referred_columns"] != target_columns):
                raise RuntimeError(f"Existing foreign key differs: {table}.{name}")
        else:
            extra = {"deferrable": True, "initially": "DEFERRED"} if name == "fk_session_current_result" else {}
            op.create_foreign_key(name, table, target, columns, target_columns,
                                  ondelete="RESTRICT", **extra)
    # Replace the weaker device references after tenant-aware validation succeeds.
    for table in ("sessions", "login_sessions"):
        for item in sa.inspect(op.get_bind()).get_foreign_keys(table):
            if (item["constrained_columns"] == ["device_id"]
                    and item["referred_table"] == "devices"
                    and item["referred_columns"] == ["id"]):
                op.drop_constraint(item["name"], table, type_="foreignkey")
    # Repair residual plaintext left by older cleanup logic on already-cleared
    # sessions. Restrict by the persisted cleared marker; never alter live text.
    op.execute(sa.text("""
        UPDATE session_attempts a SET raw_text = NULL
        FROM sessions s WHERE s.user_id = a.user_id AND s.id = a.session_id
        AND s.content_cleared_at IS NOT NULL AND a.raw_text IS NOT NULL
    """))
    op.execute(sa.text("""
        UPDATE sessions SET asr_hints = NULL, llm_hints = NULL
        WHERE content_cleared_at IS NOT NULL
        AND (asr_hints IS NOT NULL OR llm_hints IS NOT NULL)
    """))
    # A second inspection verifies creation, rather than trusting a no-op branch.
    for table, name, *_ in FKS:
        if name not in {item["name"] for item in sa.inspect(op.get_bind()).get_foreign_keys(table)}:
            raise RuntimeError(f"Missing foreign key after migration: {table}.{name}")


def downgrade() -> None:
    for table in ("sessions", "login_sessions"):
        name = f"{table}_device_id_fkey"
        if not any(item["constrained_columns"] == ["device_id"]
                   for item in sa.inspect(op.get_bind()).get_foreign_keys(table)):
            op.create_foreign_key(name, table, "devices", ["device_id"], ["id"], ondelete="RESTRICT")
    for table, name, *_ in reversed(FKS):
        if name in {item["name"] for item in sa.inspect(op.get_bind()).get_foreign_keys(table)}:
            op.drop_constraint(name, table, type_="foreignkey")
    for table, name, _ in reversed(UNIQUES):
        if name in {item["name"] for item in sa.inspect(op.get_bind()).get_unique_constraints(table)}:
            op.drop_constraint(name, table, type_="unique")
