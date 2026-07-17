"""Persist and constrain task lifecycle state.

Revision ID: 20260717_0007
Revises: 20260717_0006
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260717_0007"
down_revision: str | None = "20260717_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LIFECYCLE_VALUES = ("active", "disabled", "archived")


def _lifecycle_enum() -> sa.TypeEngine:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        enum_type = postgresql.ENUM(
            *_LIFECYCLE_VALUES,
            name="task_lifecycle_state",
            create_type=False,
        )
        enum_type.create(bind, checkfirst=True)
        return enum_type
    return sa.String(length=8)


def upgrade() -> None:
    lifecycle_state = _lifecycle_enum()
    dialect = op.get_bind().dialect.name
    batch_kwargs = {"recreate": "always"} if dialect == "sqlite" else {}
    with op.batch_alter_table("tasks", **batch_kwargs) as batch_op:
        batch_op.add_column(
            sa.Column(
                "lifecycle_state",
                lifecycle_state,
                server_default=sa.text("'active'"),
                nullable=False,
            )
        )
        if dialect == "sqlite":
            batch_op.create_check_constraint(
                "ck_tasks_task_lifecycle_state",
                "lifecycle_state IN ('active', 'disabled', 'archived')",
            )
        batch_op.create_index(
            "ix_tasks_workspace_lifecycle",
            ["workspace_id", "lifecycle_state"],
        )

    if dialect == "postgresql":
        op.execute(
            """
            CREATE FUNCTION lls_enforce_task_lifecycle_transition()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                IF NEW.lifecycle_state::text = OLD.lifecycle_state::text
                   OR (OLD.lifecycle_state::text = 'active'
                       AND NEW.lifecycle_state::text IN ('disabled', 'archived'))
                   OR (OLD.lifecycle_state::text = 'disabled'
                       AND NEW.lifecycle_state::text IN ('active', 'archived'))
                   OR (OLD.lifecycle_state::text = 'archived'
                       AND NEW.lifecycle_state::text = 'active')
                THEN
                    RETURN NEW;
                END IF;
                RAISE EXCEPTION 'task lifecycle transition is invalid'
                    USING ERRCODE = '22000';
            END;
            $$
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_tasks_lifecycle_transition
            BEFORE UPDATE OF lifecycle_state ON tasks
            FOR EACH ROW EXECUTE FUNCTION lls_enforce_task_lifecycle_transition()
            """
        )
    elif dialect == "sqlite":
        op.execute(
            """
            CREATE TRIGGER trg_tasks_lifecycle_transition
            BEFORE UPDATE OF lifecycle_state ON tasks
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
    else:
        raise RuntimeError(f"unsupported task lifecycle dialect: {dialect}")


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_tasks_lifecycle_transition ON tasks")
        op.execute("DROP FUNCTION IF EXISTS lls_enforce_task_lifecycle_transition()")
    elif dialect == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS trg_tasks_lifecycle_transition")
    else:
        raise RuntimeError(f"unsupported task lifecycle dialect: {dialect}")

    batch_kwargs = {"recreate": "always"} if dialect == "sqlite" else {}
    with op.batch_alter_table("tasks", **batch_kwargs) as batch_op:
        if dialect == "sqlite":
            batch_op.drop_constraint("ck_tasks_task_lifecycle_state", type_="check")
        batch_op.drop_index("ix_tasks_workspace_lifecycle")
        batch_op.drop_column("lifecycle_state")

    if dialect == "postgresql":
        postgresql.ENUM(name="task_lifecycle_state").drop(op.get_bind(), checkfirst=True)
