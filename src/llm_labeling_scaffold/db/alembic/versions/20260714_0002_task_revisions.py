from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260714_0002"
down_revision: str | None = "20260713_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _existing_enum(name: str, values: tuple[str, ...]) -> sa.Enum:
    if op.get_bind().dialect.name == "postgresql":
        return postgresql.ENUM(*values, name=name, create_type=False)
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True)


def _new_enum(name: str, values: tuple[str, ...]) -> sa.Enum:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        enum_type = postgresql.ENUM(*values, name=name, create_type=False)
        enum_type.create(bind, checkfirst=True)
        return enum_type
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True)


def _json_document():
    if op.get_bind().dialect.name == "postgresql":
        return postgresql.JSONB()
    return sa.JSON()


def upgrade() -> None:
    audit_channel = _existing_enum(
        "audit_channel",
        ("panel", "mcp", "api", "cli", "worker", "system"),
    )
    materialization_state = _new_enum(
        "task_materialization_state",
        ("pending", "processing", "succeeded", "failed"),
    )
    json_document = _json_document()

    op.create_table(
        "task_drafts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("definition", json_document, nullable=False),
        sa.Column("rendered_task", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("length(fingerprint) = 64", name="ck_task_drafts_fingerprint_sha256"),
        sa.CheckConstraint("version > 0", name="ck_task_drafts_version_positive"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_task_drafts_workspace_id_workspaces",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "task_id"],
            ["tasks.workspace_id", "tasks.id"],
            name="fk_task_drafts_workspace_task",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_task_drafts"),
        sa.UniqueConstraint("task_id", name="uq_task_drafts_task_id"),
    )

    op.create_table(
        "task_revisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("draft_version", sa.Integer(), nullable=False),
        sa.Column("draft_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("definition", json_document, nullable=False),
        sa.Column("rendered_task", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor_principal_id", sa.Uuid(), nullable=False),
        sa.Column("caller_principal_id", sa.Uuid(), nullable=False),
        sa.Column("channel", audit_channel, nullable=False),
        sa.Column("previous_revision_id", sa.Uuid(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_record_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("length(content_hash) = 64", name="ck_task_revisions_content_hash_sha256"),
        sa.CheckConstraint(
            "length(draft_fingerprint) = 64",
            name="ck_task_revisions_draft_fingerprint_sha256",
        ),
        sa.CheckConstraint("draft_version > 0", name="ck_task_revisions_draft_version_positive"),
        sa.CheckConstraint("length(trim(reason)) > 0", name="ck_task_revisions_reason_not_blank"),
        sa.CheckConstraint("revision_number > 0", name="ck_task_revisions_revision_number_positive"),
        sa.ForeignKeyConstraint(
            ["actor_principal_id"],
            ["principals.id"],
            name="fk_task_revisions_actor_principal_id_principals",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["caller_principal_id"],
            ["principals.id"],
            name="fk_task_revisions_caller_principal_id_principals",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["idempotency_record_id"],
            ["idempotency_records.id"],
            name="fk_task_revisions_idempotency_record_id_idempotency_records",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_task_revisions_workspace_id_workspaces",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "task_id", "previous_revision_id"],
            ["task_revisions.workspace_id", "task_revisions.task_id", "task_revisions.id"],
            name="fk_task_revisions_previous_revision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "task_id"],
            ["tasks.workspace_id", "tasks.id"],
            name="fk_task_revisions_workspace_task",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_task_revisions"),
        sa.UniqueConstraint(
            "workspace_id",
            "task_id",
            "id",
            name="uq_task_revisions_workspace_task_id",
        ),
        sa.UniqueConstraint(
            "task_id",
            "revision_number",
            name="uq_task_revisions_task_number",
        ),
        sa.UniqueConstraint(
            "idempotency_record_id",
            name="uq_task_revisions_idempotency_record_id",
        ),
    )

    with op.batch_alter_table("tasks") as batch_op:
        batch_op.add_column(sa.Column("current_revision_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            "fk_tasks_current_revision_id_task_revisions",
            "task_revisions",
            ["current_revision_id"],
            ["id"],
            ondelete="RESTRICT",
        )

    op.create_table(
        "task_revision_materializations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("revision_id", sa.Uuid(), nullable=False),
        sa.Column("state", materialization_state, server_default=sa.text("'pending'"), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("'0'"), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_by", sa.String(length=255), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("materialized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_task_revision_materializations_attempt_count_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_task_revision_materializations_workspace_id_workspaces",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "task_id", "revision_id"],
            ["task_revisions.workspace_id", "task_revisions.task_id", "task_revisions.id"],
            name="fk_task_revision_materializations_revision",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "task_id"],
            ["tasks.workspace_id", "tasks.id"],
            name="fk_task_revision_materializations_workspace_task",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_task_revision_materializations"),
        sa.UniqueConstraint(
            "revision_id",
            name="uq_task_revision_materializations_revision_id",
        ),
    )
    op.create_index(
        "ix_task_revision_materializations_pending",
        "task_revision_materializations",
        ["state", "available_at", "created_at"],
    )

    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            CREATE FUNCTION lls_reject_task_revision_mutation()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                RAISE EXCEPTION 'task_revisions is immutable' USING ERRCODE = '55000';
            END;
            $$
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_task_revisions_immutable
            BEFORE UPDATE OR DELETE ON task_revisions
            FOR EACH ROW EXECUTE FUNCTION lls_reject_task_revision_mutation()
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_task_revisions_immutable_truncate
            BEFORE TRUNCATE ON task_revisions
            FOR EACH STATEMENT EXECUTE FUNCTION lls_reject_task_revision_mutation()
            """
        )
    elif op.get_bind().dialect.name == "sqlite":
        op.execute(
            """
            CREATE TRIGGER trg_task_revisions_immutable_update
            BEFORE UPDATE ON task_revisions
            BEGIN
                SELECT RAISE(ABORT, 'task_revisions is immutable');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_task_revisions_immutable_delete
            BEFORE DELETE ON task_revisions
            BEGIN
                SELECT RAISE(ABORT, 'task_revisions is immutable');
            END
            """
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_task_revisions_immutable_truncate ON task_revisions")
        op.execute("DROP TRIGGER IF EXISTS trg_task_revisions_immutable ON task_revisions")
        op.execute("DROP FUNCTION IF EXISTS lls_reject_task_revision_mutation()")
    elif op.get_bind().dialect.name == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS trg_task_revisions_immutable_update")
        op.execute("DROP TRIGGER IF EXISTS trg_task_revisions_immutable_delete")

    op.drop_index(
        "ix_task_revision_materializations_pending",
        table_name="task_revision_materializations",
    )
    op.drop_table("task_revision_materializations")
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_constraint(
            "fk_tasks_current_revision_id_task_revisions",
            type_="foreignkey",
        )
        batch_op.drop_column("current_revision_id")
    op.drop_table("task_revisions")
    op.drop_table("task_drafts")

    if op.get_bind().dialect.name == "postgresql":
        postgresql.ENUM(name="task_materialization_state").drop(op.get_bind(), checkfirst=True)
