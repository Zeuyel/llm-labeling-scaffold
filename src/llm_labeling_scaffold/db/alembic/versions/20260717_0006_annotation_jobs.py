"""Add the top-level annotation job control entity.

Revision ID: 20260717_0006
Revises: 20260716_0005
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence
import os

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260717_0006"
down_revision: str | None = "20260716_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


ENUMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "annotation_job_lifecycle",
        (
            "draft",
            "ready",
            "dispatching",
            "dispatched",
            "collecting",
            "completed",
            "failed",
            "archived",
        ),
    ),
    ("annotation_dispatch_state", ("pending", "running", "succeeded", "failed")),
    ("annotation_collection_state", ("pending", "running", "succeeded", "failed")),
)


def _enum(name: str, values: tuple[str, ...]) -> sa.Enum:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        enum_type = postgresql.ENUM(*values, name=name, create_type=False)
        enum_type.create(bind, checkfirst=True)
        return enum_type
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True)


def _create_table() -> None:
    enum_types = {name: _enum(name, values) for name, values in ENUMS}

    op.create_table(
        "annotation_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("task_revision_id", sa.Uuid(), nullable=False),
        sa.Column("source_manifest_id", sa.String(length=255), nullable=False),
        sa.Column("source_manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("connection_binding_id", sa.Uuid(), nullable=False),
        sa.Column("cohort_revision_id", sa.Uuid(), nullable=False),
        sa.Column("allocation_plan_id", sa.Uuid(), nullable=False),
        sa.Column("logical_job_id", sa.String(length=255), nullable=False),
        sa.Column("dataset_name", sa.String(length=255), nullable=False),
        sa.Column("remote_dataset_id", sa.Uuid(), nullable=True),
        sa.Column(
            "lifecycle_state",
            enum_types["annotation_job_lifecycle"],
            server_default=sa.text("'draft'"),
            nullable=False,
        ),
        sa.Column(
            "dispatch_state",
            enum_types["annotation_dispatch_state"],
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column(
            "collection_state",
            enum_types["annotation_collection_state"],
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("created_by_principal_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_record_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("length(trim(logical_job_id)) > 0", name="logical_job_id_not_blank"),
        sa.CheckConstraint("length(trim(dataset_name)) > 0", name="dataset_name_not_blank"),
        sa.CheckConstraint(
            "length(trim(source_manifest_id)) > 0",
            name="source_manifest_id_not_blank",
        ),
        sa.CheckConstraint("length(source_manifest_hash) = 64", name="source_manifest_hash_sha256"),
        sa.CheckConstraint(
            "remote_dataset_id IS NULL OR dispatch_state = 'succeeded'",
            name="remote_dataset_requires_dispatch",
        ),
        sa.CheckConstraint(
            "collection_state <> 'succeeded' OR dispatch_state = 'succeeded'",
            name="collection_requires_dispatch",
        ),
        sa.CheckConstraint(
            "lifecycle_state <> 'completed' OR "
            "(dispatch_state = 'succeeded' AND collection_state = 'succeeded')",
            name="completed_requires_collection",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_annotation_jobs_workspace_id_workspaces",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "task_id"],
            ["tasks.workspace_id", "tasks.id"],
            name="fk_annotation_jobs_workspace_task",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "task_id", "task_revision_id"],
            ["task_revisions.workspace_id", "task_revisions.task_id", "task_revisions.id"],
            name="fk_annotation_jobs_task_revision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "connection_binding_id"],
            ["argilla_connection_bindings.workspace_id", "argilla_connection_bindings.id"],
            name="fk_annotation_jobs_connection_binding",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "cohort_revision_id"],
            ["annotator_cohort_revisions.workspace_id", "annotator_cohort_revisions.id"],
            name="fk_annotation_jobs_cohort_revision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "allocation_plan_id"],
            ["allocation_plans.workspace_id", "allocation_plans.id"],
            name="fk_annotation_jobs_allocation_plan",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_principal_id"],
            ["principals.id"],
            name="fk_annotation_jobs_creator",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["idempotency_record_id"],
            ["idempotency_records.id"],
            name="fk_annotation_jobs_idempotency",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_annotation_jobs"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_annotation_jobs_workspace_id"),
        sa.UniqueConstraint(
            "workspace_id",
            "logical_job_id",
            name="uq_annotation_jobs_workspace_logical_id",
        ),
        sa.UniqueConstraint("remote_dataset_id", name="uq_annotation_jobs_remote_dataset_id"),
        sa.UniqueConstraint("idempotency_record_id", name="uq_annotation_jobs_idempotency"),
    )
    op.create_index(
        "ix_annotation_jobs_workspace_task_created",
        "annotation_jobs",
        ["workspace_id", "task_id", "created_at"],
    )
    op.create_index(
        "ix_annotation_jobs_workspace_lifecycle_updated",
        "annotation_jobs",
        ["workspace_id", "lifecycle_state", "updated_at"],
    )


def _annotation_job_guard_function() -> str:
    return """
    CREATE OR REPLACE FUNCTION lls_annotation_job_guard()
    RETURNS trigger
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog, public
    AS $$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'annotation jobs are not directly deletable'
                USING ERRCODE = '55000';
        END IF;

        IF TG_OP = 'INSERT' THEN
            IF NEW.lifecycle_state::text <> 'draft'
               OR NEW.dispatch_state::text <> 'pending'
               OR NEW.collection_state::text <> 'pending'
               OR NEW.remote_dataset_id IS NOT NULL
            THEN
                RAISE EXCEPTION 'annotation job must start as draft'
                    USING ERRCODE = '55000';
            END IF;
        ELSE
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
               OR NEW.task_id IS DISTINCT FROM OLD.task_id
               OR NEW.task_revision_id IS DISTINCT FROM OLD.task_revision_id
               OR NEW.source_manifest_id IS DISTINCT FROM OLD.source_manifest_id
               OR NEW.source_manifest_hash IS DISTINCT FROM OLD.source_manifest_hash
               OR NEW.connection_binding_id IS DISTINCT FROM OLD.connection_binding_id
               OR NEW.cohort_revision_id IS DISTINCT FROM OLD.cohort_revision_id
               OR NEW.allocation_plan_id IS DISTINCT FROM OLD.allocation_plan_id
               OR NEW.logical_job_id IS DISTINCT FROM OLD.logical_job_id
               OR NEW.dataset_name IS DISTINCT FROM OLD.dataset_name
               OR NEW.created_by_principal_id IS DISTINCT FROM OLD.created_by_principal_id
               OR NEW.idempotency_record_id IS DISTINCT FROM OLD.idempotency_record_id
               OR NEW.created_at IS DISTINCT FROM OLD.created_at
            THEN
                RAISE EXCEPTION 'annotation job identity is immutable'
                    USING ERRCODE = '55000';
            END IF;
            IF OLD.remote_dataset_id IS NOT NULL
               AND NEW.remote_dataset_id IS DISTINCT FROM OLD.remote_dataset_id
            THEN
                RAISE EXCEPTION 'annotation job remote dataset binding is immutable'
                    USING ERRCODE = '55000';
            END IF;
            IF NOT (
                NEW.lifecycle_state::text = OLD.lifecycle_state::text
                OR (OLD.lifecycle_state::text = 'draft' AND NEW.lifecycle_state::text IN ('ready', 'dispatching', 'failed'))
                OR (OLD.lifecycle_state::text = 'ready' AND NEW.lifecycle_state::text IN ('dispatching', 'failed'))
                OR (OLD.lifecycle_state::text = 'dispatching' AND NEW.lifecycle_state::text IN ('dispatched', 'failed'))
                OR (OLD.lifecycle_state::text = 'dispatched' AND NEW.lifecycle_state::text IN ('collecting', 'completed', 'failed'))
                OR (OLD.lifecycle_state::text = 'collecting' AND NEW.lifecycle_state::text IN ('completed', 'failed'))
                OR (OLD.lifecycle_state::text = 'failed' AND NEW.lifecycle_state::text IN ('ready', 'dispatching', 'collecting', 'archived'))
                OR (OLD.lifecycle_state::text = 'completed' AND NEW.lifecycle_state::text = 'archived')
            ) THEN
                RAISE EXCEPTION 'annotation job lifecycle transition is invalid'
                    USING ERRCODE = '55000';
            END IF;
        END IF;

        IF NOT EXISTS (
            SELECT 1
            FROM allocation_plans plan
            JOIN allocation_plan_states state
              ON state.plan_id = plan.id
             AND state.workspace_id = plan.workspace_id
            WHERE plan.id = NEW.allocation_plan_id
              AND plan.workspace_id = NEW.workspace_id
              AND plan.task_id = NEW.task_id
              AND plan.task_revision_id = NEW.task_revision_id
              AND plan.connection_binding_id = NEW.connection_binding_id
              AND plan.cohort_revision_id = NEW.cohort_revision_id
              AND plan.source_manifest_id = NEW.source_manifest_id
              AND plan.source_manifest_hash = NEW.source_manifest_hash
              AND state.lifecycle_state::text = 'confirmed'
        ) THEN
            RAISE EXCEPTION 'annotation job allocation plan binding is invalid'
                USING ERRCODE = '55000';
        END IF;

        IF TG_OP = 'INSERT' THEN
            IF NOT EXISTS (
                SELECT 1
                FROM idempotency_records record
                WHERE record.id = NEW.idempotency_record_id
                  AND record.workspace_id = NEW.workspace_id
                  AND record.actor_principal_id = NEW.created_by_principal_id
                  AND record.required_permission = 'annotation:review'
                  AND record.resource_type = 'task'
                  AND record.resource_id = NEW.task_id
                  AND record.state::text = 'pending'
            ) THEN
                RAISE EXCEPTION 'annotation job idempotency authorization is invalid'
                    USING ERRCODE = '55000';
            END IF;
        END IF;

        IF NOT (
            (NEW.lifecycle_state::text IN ('draft', 'ready')
                AND NEW.dispatch_state::text = 'pending'
                AND NEW.collection_state::text = 'pending'
                AND NEW.remote_dataset_id IS NULL)
            OR (NEW.lifecycle_state::text = 'dispatching'
                AND NEW.dispatch_state::text = 'running'
                AND NEW.collection_state::text = 'pending'
                AND NEW.remote_dataset_id IS NULL)
            OR (NEW.lifecycle_state::text = 'dispatched'
                AND NEW.dispatch_state::text = 'succeeded'
                AND NEW.collection_state::text IN ('pending', 'running', 'succeeded', 'failed')
                AND NEW.remote_dataset_id IS NOT NULL)
            OR (NEW.lifecycle_state::text = 'collecting'
                AND NEW.dispatch_state::text = 'succeeded'
                AND NEW.collection_state::text = 'running'
                AND NEW.remote_dataset_id IS NOT NULL)
            OR (NEW.lifecycle_state::text = 'completed'
                AND NEW.dispatch_state::text = 'succeeded'
                AND NEW.collection_state::text = 'succeeded'
                AND NEW.remote_dataset_id IS NOT NULL)
            OR (NEW.lifecycle_state::text = 'failed'
                AND (NEW.dispatch_state::text = 'failed' OR NEW.collection_state::text = 'failed'))
            OR NEW.lifecycle_state::text = 'archived'
        ) THEN
            RAISE EXCEPTION 'annotation job lifecycle/state mismatch'
                USING ERRCODE = '55000';
        END IF;

        RETURN NEW;
    END;
    $$
    """


def _install_postgres_triggers() -> None:
    op.execute(_annotation_job_guard_function())
    op.execute(
        """
        CREATE TRIGGER trg_annotation_jobs_guard
        BEFORE INSERT OR UPDATE OR DELETE ON annotation_jobs
        FOR EACH ROW EXECUTE FUNCTION lls_annotation_job_guard()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_annotation_jobs_no_truncate
        BEFORE TRUNCATE ON annotation_jobs
        FOR EACH STATEMENT EXECUTE FUNCTION lls_reject_allocation_truncate()
        """
    )


def _install_sqlite_triggers() -> None:
    op.execute(
        """
        CREATE TRIGGER trg_annotation_jobs_contract_insert
        BEFORE INSERT ON annotation_jobs
        BEGIN
            SELECT RAISE(ABORT, 'annotation job must start as draft')
            WHERE NEW.lifecycle_state <> 'draft'
               OR NEW.dispatch_state <> 'pending'
               OR NEW.collection_state <> 'pending'
               OR NEW.remote_dataset_id IS NOT NULL;
            SELECT RAISE(ABORT, 'annotation job allocation plan binding is invalid')
            WHERE NOT EXISTS (
                SELECT 1
                FROM allocation_plans plan
                JOIN allocation_plan_states state
                  ON state.plan_id = plan.id
                 AND state.workspace_id = plan.workspace_id
                WHERE plan.id = NEW.allocation_plan_id
                  AND plan.workspace_id = NEW.workspace_id
                  AND plan.task_id = NEW.task_id
                  AND plan.task_revision_id = NEW.task_revision_id
                  AND plan.connection_binding_id = NEW.connection_binding_id
                  AND plan.cohort_revision_id = NEW.cohort_revision_id
                  AND plan.source_manifest_id = NEW.source_manifest_id
                  AND plan.source_manifest_hash = NEW.source_manifest_hash
                  AND state.lifecycle_state = 'confirmed'
            );
            SELECT RAISE(ABORT, 'annotation job idempotency authorization is invalid')
            WHERE NOT EXISTS (
                SELECT 1
                FROM idempotency_records record
                WHERE record.id = NEW.idempotency_record_id
                  AND record.workspace_id = NEW.workspace_id
                  AND record.actor_principal_id = NEW.created_by_principal_id
                  AND record.required_permission = 'annotation:review'
                  AND record.resource_type = 'task'
                  AND record.resource_id = NEW.task_id
                  AND record.state = 'pending'
            );
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_annotation_jobs_contract_update
        BEFORE UPDATE ON annotation_jobs
        BEGIN
            SELECT RAISE(ABORT, 'annotation job identity is immutable')
            WHERE NEW.id IS NOT OLD.id
               OR NEW.workspace_id IS NOT OLD.workspace_id
               OR NEW.task_id IS NOT OLD.task_id
               OR NEW.task_revision_id IS NOT OLD.task_revision_id
               OR NEW.source_manifest_id IS NOT OLD.source_manifest_id
               OR NEW.source_manifest_hash IS NOT OLD.source_manifest_hash
               OR NEW.connection_binding_id IS NOT OLD.connection_binding_id
               OR NEW.cohort_revision_id IS NOT OLD.cohort_revision_id
               OR NEW.allocation_plan_id IS NOT OLD.allocation_plan_id
               OR NEW.logical_job_id IS NOT OLD.logical_job_id
               OR NEW.dataset_name IS NOT OLD.dataset_name
               OR NEW.created_by_principal_id IS NOT OLD.created_by_principal_id
               OR NEW.idempotency_record_id IS NOT OLD.idempotency_record_id
               OR NEW.created_at IS NOT OLD.created_at;
            SELECT RAISE(ABORT, 'annotation job remote dataset binding is immutable')
            WHERE OLD.remote_dataset_id IS NOT NULL
              AND NEW.remote_dataset_id IS NOT OLD.remote_dataset_id;
            SELECT RAISE(ABORT, 'annotation job lifecycle transition is invalid')
            WHERE NOT (
                NEW.lifecycle_state IS OLD.lifecycle_state
                OR (OLD.lifecycle_state = 'draft' AND NEW.lifecycle_state IN ('ready', 'dispatching', 'failed'))
                OR (OLD.lifecycle_state = 'ready' AND NEW.lifecycle_state IN ('dispatching', 'failed'))
                OR (OLD.lifecycle_state = 'dispatching' AND NEW.lifecycle_state IN ('dispatched', 'failed'))
                OR (OLD.lifecycle_state = 'dispatched' AND NEW.lifecycle_state IN ('collecting', 'completed', 'failed'))
                OR (OLD.lifecycle_state = 'collecting' AND NEW.lifecycle_state IN ('completed', 'failed'))
                OR (OLD.lifecycle_state = 'failed' AND NEW.lifecycle_state IN ('ready', 'dispatching', 'collecting', 'archived'))
                OR (OLD.lifecycle_state = 'completed' AND NEW.lifecycle_state = 'archived')
            );
            SELECT RAISE(ABORT, 'annotation job lifecycle/state mismatch')
            WHERE NOT (
                (NEW.lifecycle_state IN ('draft', 'ready')
                    AND NEW.dispatch_state = 'pending'
                    AND NEW.collection_state = 'pending'
                    AND NEW.remote_dataset_id IS NULL)
                OR (NEW.lifecycle_state = 'dispatching'
                    AND NEW.dispatch_state = 'running'
                    AND NEW.collection_state = 'pending'
                    AND NEW.remote_dataset_id IS NULL)
                OR (NEW.lifecycle_state = 'dispatched'
                    AND NEW.dispatch_state = 'succeeded'
                    AND NEW.collection_state IN ('pending', 'running', 'succeeded', 'failed')
                    AND NEW.remote_dataset_id IS NOT NULL)
                OR (NEW.lifecycle_state = 'collecting'
                    AND NEW.dispatch_state = 'succeeded'
                    AND NEW.collection_state = 'running'
                    AND NEW.remote_dataset_id IS NOT NULL)
                OR (NEW.lifecycle_state = 'completed'
                    AND NEW.dispatch_state = 'succeeded'
                    AND NEW.collection_state = 'succeeded'
                    AND NEW.remote_dataset_id IS NOT NULL)
                OR (NEW.lifecycle_state = 'failed'
                    AND (NEW.dispatch_state = 'failed' OR NEW.collection_state = 'failed'))
                OR NEW.lifecycle_state = 'archived'
            );
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_annotation_jobs_reject_delete
        BEFORE DELETE ON annotation_jobs
        BEGIN
            SELECT RAISE(ABORT, 'annotation jobs are not directly deletable');
        END
        """
    )


def _configure_postgres_runtime_role() -> None:
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
    role_identifier = '"' + app_role.replace('"', '""') + '"'
    for statement in (
        "REVOKE ALL PRIVILEGES ON TABLE public.annotation_jobs FROM PUBLIC",
        f"GRANT SELECT, INSERT, UPDATE ON TABLE public.annotation_jobs TO {role_identifier}",
        f"REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE public.annotation_jobs FROM {role_identifier}",
        "REVOKE EXECUTE ON FUNCTION public.lls_annotation_job_guard() FROM PUBLIC",
        f"GRANT EXECUTE ON FUNCTION public.lls_annotation_job_guard() TO {role_identifier}",
    ):
        op.execute(statement)


def upgrade() -> None:
    _create_table()
    if op.get_bind().dialect.name == "postgresql":
        _install_postgres_triggers()
        _configure_postgres_runtime_role()
    elif op.get_bind().dialect.name == "sqlite":
        _install_sqlite_triggers()
    else:
        raise RuntimeError(f"unsupported annotation job dialect: {op.get_bind().dialect.name}")


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_annotation_jobs_no_truncate ON annotation_jobs")
        op.execute("DROP TRIGGER IF EXISTS trg_annotation_jobs_guard ON annotation_jobs")
        op.execute("DROP FUNCTION IF EXISTS lls_annotation_job_guard()")
    elif dialect != "sqlite":
        raise RuntimeError(f"unsupported annotation job dialect: {dialect}")

    op.drop_index("ix_annotation_jobs_workspace_lifecycle_updated", table_name="annotation_jobs")
    op.drop_index("ix_annotation_jobs_workspace_task_created", table_name="annotation_jobs")
    op.drop_table("annotation_jobs")

    if dialect == "postgresql":
        for name, _values in reversed(ENUMS):
            op.execute(f"DROP TYPE IF EXISTS {name}")
