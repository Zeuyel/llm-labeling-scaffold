from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260713_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enum(name: str, values: tuple[str, ...]) -> sa.Enum:
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
    principal_type = _enum("principal_type", ("user", "service"))
    role_name = _enum("role_name", ("viewer", "annotator", "experimenter", "admin"))
    idempotency_state = _enum("idempotency_state", ("pending", "succeeded", "failed"))
    audit_actor_type = _enum("audit_actor_type", ("principal", "system"))
    audit_channel = _enum("audit_channel", ("panel", "mcp", "api", "cli", "worker", "system"))
    migration_status = _enum("migration_status", ("running", "succeeded", "failed"))
    json_document = _json_document()

    op.create_table(
        "principals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("issuer", sa.String(length=255), nullable=False),
        sa.Column("subject", sa.String(length=512), nullable=False),
        sa.Column("principal_type", principal_type, nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("email_snapshot", sa.String(length=320), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("length(trim(issuer)) > 0", name="ck_principals_issuer_not_blank"),
        sa.CheckConstraint("length(trim(subject)) > 0", name="ck_principals_subject_not_blank"),
        sa.PrimaryKeyConstraint("id", name="pk_principals"),
        sa.UniqueConstraint("issuer", "subject", name="uq_principals_issuer_subject"),
    )
    op.create_index("ix_principals_type_active", "principals", ["principal_type", "is_active"])

    op.create_table(
        "workspaces",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=63), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("length(trim(slug)) > 0", name="ck_workspaces_slug_not_blank"),
        sa.CheckConstraint("length(trim(name)) > 0", name="ck_workspaces_name_not_blank"),
        sa.PrimaryKeyConstraint("id", name="pk_workspaces"),
        sa.UniqueConstraint("slug", name="uq_workspaces_slug"),
    )

    op.create_table(
        "tasks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("task_key", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_by_principal_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("length(trim(task_key)) > 0", name="ck_tasks_task_key_not_blank"),
        sa.CheckConstraint("length(trim(name)) > 0", name="ck_tasks_name_not_blank"),
        sa.ForeignKeyConstraint(
            ["created_by_principal_id"],
            ["principals.id"],
            name="fk_tasks_created_by_principal_id_principals",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_tasks_workspace_id_workspaces",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tasks"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_tasks_workspace_id_id"),
        sa.UniqueConstraint("task_key", name="uq_tasks_task_key"),
    )
    op.create_table(
        "role_bindings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("role", role_name, nullable=False),
        sa.Column("created_by_principal_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["created_by_principal_id"],
            ["principals.id"],
            name="fk_role_bindings_created_by_principal_id_principals",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name="fk_role_bindings_principal_id_principals",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_role_bindings_workspace_id_workspaces",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "task_id"],
            ["tasks.workspace_id", "tasks.id"],
            name="fk_role_bindings_workspace_task",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_role_bindings"),
    )
    op.create_index(
        "uq_role_bindings_workspace_principal",
        "role_bindings",
        ["workspace_id", "principal_id"],
        unique=True,
        postgresql_where=sa.text("task_id IS NULL"),
        sqlite_where=sa.text("task_id IS NULL"),
    )
    op.create_index(
        "uq_role_bindings_task_principal",
        "role_bindings",
        ["workspace_id", "task_id", "principal_id"],
        unique=True,
        postgresql_where=sa.text("task_id IS NOT NULL"),
        sqlite_where=sa.text("task_id IS NOT NULL"),
    )
    op.create_index(
        "ix_role_bindings_principal_workspace",
        "role_bindings",
        ["principal_id", "workspace_id"],
    )

    op.create_table(
        "idempotency_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("actor_principal_id", sa.Uuid(), nullable=False),
        sa.Column("caller_principal_id", sa.Uuid(), nullable=False),
        sa.Column("operation", sa.String(length=255), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=128), nullable=False),
        sa.Column("state", idempotency_state, server_default=sa.text("'pending'"), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_body", json_document, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("length(trim(operation)) > 0", name="ck_idempotency_records_operation_not_blank"),
        sa.CheckConstraint(
            "length(idempotency_key_hash) = 64",
            name="ck_idempotency_records_key_hash_sha256",
        ),
        sa.ForeignKeyConstraint(
            ["actor_principal_id"],
            ["principals.id"],
            name="fk_idempotency_records_actor_principal_id_principals",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["caller_principal_id"],
            ["principals.id"],
            name="fk_idempotency_records_caller_principal_id_principals",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_idempotency_records_workspace_id_workspaces",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_idempotency_records"),
        sa.UniqueConstraint(
            "workspace_id",
            "operation",
            "idempotency_key_hash",
            name="uq_idempotency_records_workspace_operation_key_hash",
        ),
    )
    op.create_index(
        "ix_idempotency_records_workspace_state",
        "idempotency_records",
        ["workspace_id", "state"],
    )
    op.create_index(
        "ix_idempotency_records_actor_caller",
        "idempotency_records",
        ["workspace_id", "actor_principal_id", "caller_principal_id"],
    )
    op.create_index("ix_idempotency_records_expires_at", "idempotency_records", ["expires_at"])

    op.create_table(
        "workspace_settings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("setting_key", sa.String(length=255), nullable=False),
        sa.Column("setting_value", json_document, nullable=False),
        sa.Column("updated_by_principal_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("length(trim(setting_key)) > 0", name="ck_workspace_settings_setting_key_not_blank"),
        sa.ForeignKeyConstraint(
            ["updated_by_principal_id"],
            ["principals.id"],
            name="fk_workspace_settings_updated_by_principal_id_principals",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_workspace_settings_workspace_id_workspaces",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_workspace_settings"),
        sa.UniqueConstraint("workspace_id", "setting_key", name="uq_workspace_settings_workspace_key"),
    )

    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("actor_type", audit_actor_type, nullable=False),
        sa.Column("actor_principal_id", sa.Uuid(), nullable=True),
        sa.Column("caller_principal_id", sa.Uuid(), nullable=True),
        sa.Column("channel", audit_channel, nullable=False),
        sa.Column("actor_issuer", sa.String(length=255), nullable=False),
        sa.Column("actor_subject", sa.String(length=512), nullable=False),
        sa.Column("actor_display_name", sa.String(length=255), nullable=True),
        sa.Column("actor_email_snapshot", sa.String(length=320), nullable=True),
        sa.Column("event_type", sa.String(length=255), nullable=False),
        sa.Column("resource_type", sa.String(length=255), nullable=True),
        sa.Column("resource_id", sa.String(length=255), nullable=True),
        sa.Column("request_id", sa.String(length=255), nullable=True),
        sa.Column("details", json_document, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("length(trim(actor_issuer)) > 0", name="ck_audit_events_actor_issuer_not_blank"),
        sa.CheckConstraint("length(trim(actor_subject)) > 0", name="ck_audit_events_actor_subject_not_blank"),
        sa.CheckConstraint("length(trim(event_type)) > 0", name="ck_audit_events_event_type_not_blank"),
        sa.ForeignKeyConstraint(
            ["actor_principal_id"],
            ["principals.id"],
            name="fk_audit_events_actor_principal_id_principals",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["caller_principal_id"],
            ["principals.id"],
            name="fk_audit_events_caller_principal_id_principals",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_audit_events_workspace_id_workspaces",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_events"),
    )
    op.create_index(
        "ix_audit_events_workspace_occurred",
        "audit_events",
        ["workspace_id", "occurred_at"],
    )
    op.create_index(
        "ix_audit_events_actor_occurred",
        "audit_events",
        ["actor_principal_id", "occurred_at"],
    )
    op.create_index(
        "ix_audit_events_caller_occurred",
        "audit_events",
        ["caller_principal_id", "occurred_at"],
    )
    op.create_index(
        "ix_audit_events_resource",
        "audit_events",
        ["workspace_id", "resource_type", "resource_id"],
    )

    op.create_table(
        "migration_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("command", sa.String(length=64), nullable=False),
        sa.Column("target_revision", sa.String(length=255), nullable=False),
        sa.Column("applied_revision", sa.String(length=255), nullable=True),
        sa.Column("status", migration_status, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_migration_runs"),
    )
    op.create_index("ix_migration_runs_started_at", "migration_runs", ["started_at"])

    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            CREATE FUNCTION lls_reject_audit_event_mutation()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                RAISE EXCEPTION 'audit_events is append-only' USING ERRCODE = '55000';
            END;
            $$
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_audit_events_append_only
            BEFORE UPDATE OR DELETE ON audit_events
            FOR EACH ROW EXECUTE FUNCTION lls_reject_audit_event_mutation()
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_audit_events_append_only_truncate
            BEFORE TRUNCATE ON audit_events
            FOR EACH STATEMENT EXECUTE FUNCTION lls_reject_audit_event_mutation()
            """
        )
    elif op.get_bind().dialect.name == "sqlite":
        op.execute(
            """
            CREATE TRIGGER trg_audit_events_append_only_update
            BEFORE UPDATE ON audit_events
            BEGIN
                SELECT RAISE(ABORT, 'audit_events is append-only');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_audit_events_append_only_delete
            BEFORE DELETE ON audit_events
            BEGIN
                SELECT RAISE(ABORT, 'audit_events is append-only');
            END
            """
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_audit_events_append_only_truncate ON audit_events")
        op.execute("DROP TRIGGER IF EXISTS trg_audit_events_append_only ON audit_events")
        op.execute("DROP FUNCTION IF EXISTS lls_reject_audit_event_mutation()")
    elif op.get_bind().dialect.name == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS trg_audit_events_append_only_update")
        op.execute("DROP TRIGGER IF EXISTS trg_audit_events_append_only_delete")

    op.drop_index("ix_migration_runs_started_at", table_name="migration_runs")
    op.drop_table("migration_runs")
    op.drop_index("ix_audit_events_resource", table_name="audit_events")
    op.drop_index("ix_audit_events_caller_occurred", table_name="audit_events")
    op.drop_index("ix_audit_events_actor_occurred", table_name="audit_events")
    op.drop_index("ix_audit_events_workspace_occurred", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_table("workspace_settings")
    op.drop_index("ix_idempotency_records_expires_at", table_name="idempotency_records")
    op.drop_index("ix_idempotency_records_actor_caller", table_name="idempotency_records")
    op.drop_index("ix_idempotency_records_workspace_state", table_name="idempotency_records")
    op.drop_table("idempotency_records")
    op.drop_index("ix_role_bindings_principal_workspace", table_name="role_bindings")
    op.drop_index("uq_role_bindings_task_principal", table_name="role_bindings")
    op.drop_index("uq_role_bindings_workspace_principal", table_name="role_bindings")
    op.drop_table("role_bindings")
    op.drop_table("tasks")
    op.drop_table("workspaces")
    op.drop_index("ix_principals_type_active", table_name="principals")
    op.drop_table("principals")

    if op.get_bind().dialect.name == "postgresql":
        for name in (
            "migration_status",
            "audit_channel",
            "audit_actor_type",
            "idempotency_state",
            "role_name",
            "principal_type",
        ):
            postgresql.ENUM(name=name).drop(op.get_bind(), checkfirst=True)
