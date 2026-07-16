"""Enforce sensitive JSON and idempotency completion context.

Revision ID: 20260716_0005
Revises: 20260716_0004
Create Date: 2026-07-16
"""

from __future__ import annotations

from collections.abc import Sequence
import os

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

from llm_labeling_scaffold.db.idempotency_boundary import (
    POSTGRES_IDEMPOTENCY_CREATE_FUNCTIONS,
    POSTGRES_IDEMPOTENCY_CREATE_TRIGGERS,
    POSTGRES_IDEMPOTENCY_DROP_FUNCTIONS,
    POSTGRES_IDEMPOTENCY_DROP_TRIGGERS,
    SQLITE_IDEMPOTENCY_CREATE_TRIGGERS,
    SQLITE_IDEMPOTENCY_DROP_TRIGGERS,
)
from llm_labeling_scaffold.db.sensitive_json import (
    POSTGRES_CREATE_SENSITIVE_JSON_FUNCTIONS,
    POSTGRES_DROP_SENSITIVE_JSON_FUNCTIONS,
    POSTGRES_DROP_SENSITIVE_JSON_TRIGGERS,
    POSTGRES_SENSITIVE_JSON_TRIGGERS,
    SQLITE_DROP_SENSITIVE_JSON_TRIGGERS,
    SQLITE_SENSITIVE_JSON_TRIGGERS,
)


revision: str = "20260716_0005"
down_revision: str | None = "20260716_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHANNEL_VALUES = ("panel", "mcp", "api", "cli", "worker", "system")
_TASK_PERMISSIONS = (
    "task:read",
    "task:edit",
    "task:publish",
    "import:submit",
    "annotation:work",
    "annotation:review",
)
_WORKSPACE_PERMISSIONS = ("task:create", "audit:view", "workspace:manage")
_RUNTIME_TABLE_PRIVILEGES = (
    ("principals", "SELECT, INSERT"),
    ("workspaces", "SELECT, INSERT, UPDATE"),
    ("tasks", "SELECT, INSERT, UPDATE"),
    ("role_bindings", "SELECT, INSERT, UPDATE, DELETE"),
    ("idempotency_records", "SELECT, INSERT"),
    ("workspace_settings", "SELECT, INSERT, UPDATE"),
    ("task_drafts", "SELECT, INSERT, UPDATE"),
    ("task_revisions", "SELECT, INSERT"),
    ("task_revision_materializations", "SELECT, INSERT, UPDATE"),
    ("argilla_connection_bindings", "SELECT, INSERT, UPDATE"),
    ("argilla_annotator_mappings", "SELECT, INSERT, UPDATE"),
    ("annotator_cohorts", "SELECT, INSERT, UPDATE"),
    ("annotator_cohort_revisions", "SELECT, INSERT, UPDATE"),
    ("annotator_cohort_members", "SELECT, INSERT, UPDATE, DELETE"),
    ("allocation_plans", "SELECT, INSERT, UPDATE"),
    ("allocation_plan_states", "SELECT, UPDATE"),
    ("allocation_workspace_groups", "SELECT, INSERT, UPDATE"),
    ("allocation_dataset_groups", "SELECT, INSERT, UPDATE"),
    ("allocation_dataset_group_states", "SELECT, UPDATE"),
    ("allocation_assignment_items", "SELECT, INSERT"),
    ("allocation_record_bindings", "SELECT, UPDATE"),
    ("allocation_assignments", "SELECT, INSERT, UPDATE"),
    ("allocation_collection_receipts", "SELECT, INSERT"),
    ("audit_events", "SELECT, INSERT"),
)
_RUNTIME_FUNCTION_SIGNATURES = (
    "lls_canonical_sensitive_json_text(json)",
    "lls_complete_idempotency(uuid, uuid, uuid, uuid, text, text, text, text, text, uuid, text, integer, text, boolean, text)",
    "lls_sensitive_json_node_is_valid(json, integer)",
    "lls_sensitive_json_object_is_valid(json)",
    "lls_sensitive_json_string_is_safe(text)",
    "lls_validate_allocation_plan_graph(uuid)",
)


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        _upgrade_sqlite_schema()
        for statement in SQLITE_SENSITIVE_JSON_TRIGGERS:
            op.execute(statement)
        for statement in SQLITE_IDEMPOTENCY_CREATE_TRIGGERS:
            op.execute(statement)
    elif dialect == "postgresql":
        _drop_postgres_runtime_objects()
        _upgrade_postgres_schema()
        for statement in POSTGRES_CREATE_SENSITIVE_JSON_FUNCTIONS:
            op.execute(statement)
        for statement in POSTGRES_IDEMPOTENCY_CREATE_FUNCTIONS:
            op.execute(statement)
        _validate_existing_sensitive_json()
        for statement in POSTGRES_SENSITIVE_JSON_TRIGGERS:
            op.execute(statement)
        for statement in POSTGRES_IDEMPOTENCY_CREATE_TRIGGERS:
            op.execute(statement)
        _configure_postgres_runtime_roles()
    else:
        raise RuntimeError(f"unsupported sensitive JSON dialect: {dialect}")


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for statement in SQLITE_DROP_SENSITIVE_JSON_TRIGGERS:
            op.execute(statement)
        for statement in SQLITE_IDEMPOTENCY_DROP_TRIGGERS:
            op.execute(statement)
        _drop_sqlite_audit_append_only_triggers()
        with op.batch_alter_table("audit_events", recreate="always") as batch_op:
            batch_op.add_column(sa.Column("actor_email_snapshot", sa.String(length=320), nullable=True))
        _create_sqlite_audit_append_only_triggers()
    elif dialect == "postgresql":
        for statement in POSTGRES_DROP_SENSITIVE_JSON_TRIGGERS:
            op.execute(statement)
        for statement in POSTGRES_IDEMPOTENCY_DROP_TRIGGERS:
            op.execute(statement)
        for statement in POSTGRES_IDEMPOTENCY_DROP_FUNCTIONS:
            op.execute(statement)
        for statement in POSTGRES_DROP_SENSITIVE_JSON_FUNCTIONS:
            op.execute(statement)
        if "actor_email_snapshot" not in {
            column["name"] for column in inspect(op.get_bind()).get_columns("audit_events")
        }:
            op.add_column(
                "audit_events",
                sa.Column("actor_email_snapshot", sa.String(length=320), nullable=True),
            )
    else:
        raise RuntimeError(f"unsupported sensitive JSON dialect: {dialect}")


def _upgrade_sqlite_schema() -> None:
    _drop_sqlite_audit_append_only_triggers()
    existing_checks = {
        constraint["name"]
        for constraint in inspect(op.get_bind()).get_check_constraints("idempotency_records")
    }
    with op.batch_alter_table("idempotency_records", recreate="always") as batch_op:
        existing_columns = {
            column["name"] for column in inspect(op.get_bind()).get_columns("idempotency_records")
        }
        if "required_permission" not in existing_columns:
            batch_op.add_column(sa.Column("required_permission", sa.String(length=64), nullable=True))
        if "resource_type" not in existing_columns:
            batch_op.add_column(sa.Column("resource_type", sa.String(length=16), nullable=True))
        if "resource_id" not in existing_columns:
            batch_op.add_column(sa.Column("resource_id", sa.Uuid(), nullable=True))
        if "channel" not in existing_columns:
            batch_op.add_column(sa.Column("channel", sa.String(length=16), nullable=True))
        _create_idempotency_checks(batch_op, existing=existing_checks)
    audit_columns = {column["name"] for column in inspect(op.get_bind()).get_columns("audit_events")}
    if "actor_email_snapshot" in audit_columns:
        with op.batch_alter_table("audit_events", recreate="always") as batch_op:
            batch_op.drop_column("actor_email_snapshot")
    _create_sqlite_audit_append_only_triggers()


def _upgrade_postgres_schema() -> None:
    connection = op.get_bind()
    columns = {
        column["name"] for column in inspect(connection).get_columns("idempotency_records")
    }
    audit_channel = sa.Enum(
        *_CHANNEL_VALUES,
        name="audit_channel",
        native_enum=True,
        create_type=False,
    )
    if "required_permission" not in columns:
        op.add_column(
            "idempotency_records",
            sa.Column("required_permission", sa.String(length=64), nullable=True),
        )
    if "resource_type" not in columns:
        op.add_column(
            "idempotency_records",
            sa.Column("resource_type", sa.String(length=16), nullable=True),
        )
    if "resource_id" not in columns:
        op.add_column(
            "idempotency_records",
            sa.Column("resource_id", sa.Uuid(), nullable=True),
        )
    if "channel" not in columns:
        op.add_column(
            "idempotency_records",
            sa.Column("channel", audit_channel, nullable=True),
        )

    for table_name, column_name in (
        ("idempotency_records", "response_body"),
        ("workspace_settings", "setting_value"),
        ("audit_events", "details"),
    ):
        data_type = connection.scalar(
            sa.text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :table_name AND column_name = :column_name"
            ),
            {"table_name": table_name, "column_name": column_name},
        )
        if data_type == "jsonb":
            op.execute(
                sa.text(
                    f"ALTER TABLE {table_name} ALTER COLUMN {column_name} "
                    f"TYPE json USING {column_name}::text::json"
                )
            )

    _create_idempotency_checks(op, table_name="idempotency_records")
    audit_columns = {column["name"] for column in inspect(connection).get_columns("audit_events")}
    if "actor_email_snapshot" in audit_columns:
        op.drop_column("audit_events", "actor_email_snapshot")


def _create_idempotency_checks(
    target,
    *,
    table_name: str | None = None,
    existing: set[str] | None = None,
) -> None:
    task_permissions = ", ".join(f"'{value}'" for value in _TASK_PERMISSIONS)
    workspace_permissions = ", ".join(f"'{value}'" for value in _WORKSPACE_PERMISSIONS)
    context = (
        "(required_permission IS NULL AND resource_type IS NULL "
        "AND resource_id IS NULL AND channel IS NULL) "
        "OR ((resource_type = 'workspace' "
        "AND resource_id = workspace_id "
        f"AND required_permission IN ({workspace_permissions})) "
        "OR (resource_type = 'task' "
        f"AND required_permission IN ({task_permissions})))"
    )
    checks = (
        ("authorization_context", context),
        (
            "channel_valid",
            "channel IS NULL OR channel IN ('panel', 'mcp', 'api', 'cli', 'worker', 'system')",
        ),
        ("channel_not_system", "channel IS NULL OR channel <> 'system'"),
        (
            "response_state",
            "(required_permission IS NULL AND resource_type IS NULL "
            "AND resource_id IS NULL AND channel IS NULL) "
            "OR ((state = 'pending' AND response_status IS NULL AND response_body IS NULL) "
            "OR (state IN ('succeeded', 'failed') AND response_status IS NOT NULL))",
        ),
    )
    existing_names = set(existing or ())
    if table_name is not None:
        existing_names = {
            constraint["name"]
            for constraint in inspect(op.get_bind()).get_check_constraints(table_name)
        }
    for name, condition in checks:
        expected_name = f"ck_{table_name or 'idempotency_records'}_{name}"
        if name in existing_names or expected_name in existing_names:
            continue
        if table_name is None:
            target.create_check_constraint(name, condition)
        else:
            target.create_check_constraint(name, table_name, condition)


def _validate_existing_sensitive_json() -> None:
    connection = op.get_bind()
    for table_name, column_name in (
        ("workspace_settings", "setting_value"),
        ("audit_events", "details"),
    ):
        invalid = connection.scalar(
            sa.text(
                f"SELECT count(*) FROM {table_name} "
                f"WHERE {column_name} IS NULL "
                f"OR NOT lls_sensitive_json_object_is_valid({column_name})"
            )
        )
        if invalid:
            raise RuntimeError(f"existing {table_name}.{column_name} violates sensitive JSON contract")


def _drop_postgres_runtime_objects() -> None:
    for statement in POSTGRES_DROP_SENSITIVE_JSON_TRIGGERS:
        op.execute(statement)
    for statement in POSTGRES_IDEMPOTENCY_DROP_TRIGGERS:
        op.execute(statement)
    for statement in POSTGRES_IDEMPOTENCY_DROP_FUNCTIONS:
        op.execute(statement)
    for statement in POSTGRES_DROP_SENSITIVE_JSON_FUNCTIONS:
        op.execute(statement)


def _configure_postgres_runtime_roles() -> None:
    app_role = os.environ.get("SCAFFOLD_POSTGRES_APP_USER", "").strip()
    if not app_role:
        raise RuntimeError("SCAFFOLD_POSTGRES_APP_USER is required for PostgreSQL migrations")

    connection = op.get_bind()
    if not connection.scalar(
        sa.text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_roles
                WHERE rolname = :app_role
                  AND rolname <> current_user
                  AND rolcanlogin
                  AND NOT rolsuper
                  AND NOT rolcreatedb
                  AND NOT rolcreaterole
                  AND NOT rolinherit
                  AND NOT rolreplication
                  AND NOT rolbypassrls
            )
            """
        ),
        {"app_role": app_role},
    ):
        raise RuntimeError(f"invalid PostgreSQL runtime app role: {app_role}")

    app_role_literal = "'" + app_role.replace("'", "''") + "'"
    function_signatures = ", ".join(
        "'" + signature.replace("'", "''") + "'"
        for signature in _RUNTIME_FUNCTION_SIGNATURES
    )
    table_privileges = ", ".join(
        "('" + table_name + "', '" + privileges + "')"
        for table_name, privileges in _RUNTIME_TABLE_PRIVILEGES
    )
    op.execute(
        f"""
        DO $$
        DECLARE
            role_name text;
            function_signature text;
            table_name text;
            privilege_list text;
            app_role_name text := {app_role_literal};
        BEGIN
            FOR table_name, privilege_list IN
                SELECT relation_name, privilege_list
                FROM (VALUES {table_privileges}) AS runtime(relation_name, privilege_list)
            LOOP
                EXECUTE format(
                    'GRANT %s ON TABLE public.%I TO %I',
                    privilege_list,
                    table_name,
                    app_role_name
                );
            END LOOP;
            FOREACH function_signature IN ARRAY ARRAY[{function_signatures}]
            LOOP
                EXECUTE format(
                    'REVOKE EXECUTE ON FUNCTION public.%s FROM PUBLIC',
                    function_signature
                );
                FOR role_name IN
                    SELECT rolname
                    FROM pg_roles
                    WHERE rolname <> current_user
                      AND rolname <> app_role_name
                      AND NOT rolsuper
                LOOP
                    EXECUTE format(
                        'REVOKE EXECUTE ON FUNCTION public.%s FROM %I',
                        function_signature,
                        role_name
                    );
                END LOOP;
                EXECUTE format(
                    'GRANT EXECUTE ON FUNCTION public.%s TO %I',
                    function_signature,
                    app_role_name
                );
            END LOOP;
            EXECUTE format(
                'REVOKE UPDATE, DELETE ON TABLE public.idempotency_records FROM %I',
                app_role_name
            );
        END
        $$
        """
    )


def _drop_sqlite_audit_append_only_triggers() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_audit_events_append_only_update")
    op.execute("DROP TRIGGER IF EXISTS trg_audit_events_append_only_delete")


def _create_sqlite_audit_append_only_triggers() -> None:
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
