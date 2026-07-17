"""Add persisted task lifecycle state and database transition guards."""

from __future__ import annotations

from collections.abc import Sequence
import os

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260717_0007"
down_revision: str | None = "20260717_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TASK_LIFECYCLE_VALUES = ("active", "disabled", "archived")


def _enum() -> sa.Enum:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        enum_type = postgresql.ENUM(
            *_TASK_LIFECYCLE_VALUES,
            name="task_lifecycle",
            create_type=False,
        )
        enum_type.create(bind, checkfirst=True)
        return enum_type
    return sa.Enum(
        *_TASK_LIFECYCLE_VALUES,
        name="task_lifecycle",
        native_enum=False,
        create_constraint=True,
    )


def _postgres_guard_function() -> str:
    return """
    CREATE OR REPLACE FUNCTION lls_task_lifecycle_guard()
    RETURNS trigger
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog, public
    AS $$
    BEGIN
        IF TG_OP = 'INSERT' THEN
            IF NEW.lifecycle_state::text <> 'active' THEN
                RAISE EXCEPTION 'task must start as active'
                    USING ERRCODE = '55000';
            END IF;
        ELSIF NOT (
            NEW.lifecycle_state::text = OLD.lifecycle_state::text
            OR (OLD.lifecycle_state::text = 'active'
                AND NEW.lifecycle_state::text IN ('disabled', 'archived'))
            OR (OLD.lifecycle_state::text = 'disabled'
                AND NEW.lifecycle_state::text IN ('active', 'archived'))
            OR (OLD.lifecycle_state::text = 'archived'
                AND NEW.lifecycle_state::text = 'active')
        ) THEN
            RAISE EXCEPTION 'task lifecycle transition is invalid'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END;
    $$
    """


def _install_postgres_guard() -> None:
    op.execute(_postgres_guard_function())
    op.execute(
        """
        CREATE TRIGGER trg_tasks_lifecycle_guard
        BEFORE INSERT OR UPDATE ON tasks
        FOR EACH ROW EXECUTE FUNCTION lls_task_lifecycle_guard()
        """
    )
    app_role = os.environ.get("SCAFFOLD_POSTGRES_APP_USER", "").strip()
    if not app_role:
        raise RuntimeError("SCAFFOLD_POSTGRES_APP_USER is required for PostgreSQL migrations")
    role_identifier = '"' + app_role.replace('"', '""') + '"'
    op.execute("REVOKE EXECUTE ON FUNCTION public.lls_task_lifecycle_guard() FROM PUBLIC")
    op.execute(f"REVOKE EXECUTE ON FUNCTION public.lls_task_lifecycle_guard() FROM {role_identifier}")


def _install_sqlite_guard() -> None:
    op.execute(
        """
        CREATE TRIGGER trg_tasks_lifecycle_guard
        BEFORE INSERT ON tasks
        WHEN NEW.lifecycle_state <> 'active'
        BEGIN
            SELECT RAISE(ABORT, 'task must start as active');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_tasks_lifecycle_update_guard
        BEFORE UPDATE ON tasks
        WHEN NOT (
            NEW.lifecycle_state IS OLD.lifecycle_state
            OR (OLD.lifecycle_state = 'active' AND NEW.lifecycle_state IN ('disabled', 'archived'))
            OR (OLD.lifecycle_state = 'disabled' AND NEW.lifecycle_state IN ('active', 'archived'))
            OR (OLD.lifecycle_state = 'archived' AND NEW.lifecycle_state = 'active')
        )
        BEGIN
            SELECT RAISE(ABORT, 'task lifecycle transition is invalid');
        END
        """
    )


def upgrade() -> None:
    lifecycle_column = sa.Column(
        "lifecycle_state",
        _enum(),
        nullable=False,
        server_default=sa.text("'active'"),
    )
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("tasks", recreate="always", naming_convention={}) as batch_op:
            batch_op.add_column(lifecycle_column)
    else:
        op.add_column("tasks", lifecycle_column)
    op.create_index(
        "ix_tasks_workspace_lifecycle",
        "tasks",
        ["workspace_id", "lifecycle_state", "updated_at"],
    )
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        _install_postgres_guard()
    elif dialect == "sqlite":
        _install_sqlite_guard()
    else:
        raise RuntimeError(f"unsupported task lifecycle dialect: {dialect}")


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_tasks_lifecycle_guard ON tasks")
        op.execute("DROP FUNCTION IF EXISTS lls_task_lifecycle_guard()")
    elif dialect == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS trg_tasks_lifecycle_update_guard")
        op.execute("DROP TRIGGER IF EXISTS trg_tasks_lifecycle_guard")
    else:
        raise RuntimeError(f"unsupported task lifecycle dialect: {dialect}")

    op.drop_index("ix_tasks_workspace_lifecycle", table_name="tasks")
    if dialect == "sqlite":
        reflected = sa.Table("tasks", sa.MetaData(), autoload_with=op.get_bind())
        for constraint in list(reflected.constraints):
            if isinstance(constraint, sa.CheckConstraint) and "lifecycle_state" in str(constraint.sqltext):
                reflected.constraints.remove(constraint)
        with op.batch_alter_table("tasks", recreate="always", copy_from=reflected) as batch_op:
            batch_op.drop_column("lifecycle_state")
    else:
        op.drop_column("tasks", "lifecycle_state")
    if dialect == "postgresql":
        op.execute("DROP TYPE IF EXISTS task_lifecycle")
