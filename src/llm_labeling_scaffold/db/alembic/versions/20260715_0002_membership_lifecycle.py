"""Enforce membership lifecycle boundaries.

Revision ID: 20260715_0002
Revises: 20260713_0001
Create Date: 2026-07-15
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op


revision: str = "20260715_0002"
down_revision: str | None = "20260713_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        with op.batch_alter_table("role_bindings", recreate="always") as batch_op:
            batch_op.drop_constraint(
                "fk_role_bindings_principal_id_principals",
                type_="foreignkey",
            )
            batch_op.create_foreign_key(
                "fk_role_bindings_principal_id_principals",
                "principals",
                ["principal_id"],
                ["id"],
                ondelete="RESTRICT",
            )
        _create_sqlite_last_admin_triggers()
    elif dialect == "postgresql":
        op.drop_constraint(
            "fk_role_bindings_principal_id_principals",
            "role_bindings",
            type_="foreignkey",
        )
        op.create_foreign_key(
            "fk_role_bindings_principal_id_principals",
            "role_bindings",
            "principals",
            ["principal_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        _create_postgres_last_admin_trigger()


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS trg_role_bindings_last_workspace_admin_update")
        op.execute("DROP TRIGGER IF EXISTS trg_role_bindings_last_workspace_admin_delete")
        with op.batch_alter_table("role_bindings", recreate="always") as batch_op:
            batch_op.drop_constraint(
                "fk_role_bindings_principal_id_principals",
                type_="foreignkey",
            )
            batch_op.create_foreign_key(
                "fk_role_bindings_principal_id_principals",
                "principals",
                ["principal_id"],
                ["id"],
                ondelete="CASCADE",
            )
    elif dialect == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_role_bindings_last_workspace_admin ON role_bindings")
        op.execute("DROP FUNCTION IF EXISTS lls_enforce_last_workspace_admin()")
        op.drop_constraint(
            "fk_role_bindings_principal_id_principals",
            "role_bindings",
            type_="foreignkey",
        )
        op.create_foreign_key(
            "fk_role_bindings_principal_id_principals",
            "role_bindings",
            "principals",
            ["principal_id"],
            ["id"],
            ondelete="CASCADE",
        )


def _create_postgres_last_admin_trigger() -> None:
    op.execute(
        """
        CREATE FUNCTION lls_enforce_last_workspace_admin()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF OLD.task_id IS NOT NULL OR OLD.role::text <> 'admin' THEN
                IF TG_OP = 'DELETE' THEN
                    RETURN OLD;
                END IF;
                RETURN NEW;
            END IF;
            PERFORM 1 FROM workspaces WHERE id = OLD.workspace_id FOR UPDATE;
            IF NOT FOUND THEN
                IF TG_OP = 'DELETE' THEN
                    RETURN OLD;
                END IF;
                RETURN NEW;
            END IF;
            IF EXISTS (
                SELECT 1 FROM principals
                WHERE id = OLD.principal_id AND NOT is_active
            ) THEN
                IF TG_OP = 'DELETE' THEN
                    RETURN OLD;
                END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'UPDATE'
               AND NEW.workspace_id = OLD.workspace_id
               AND NEW.task_id IS NULL
               AND NEW.role::text = 'admin'
               AND EXISTS (
                   SELECT 1 FROM principals
                   WHERE id = NEW.principal_id AND is_active
               )
            THEN
                RETURN NEW;
            END IF;
            IF NOT EXISTS (
                SELECT 1
                FROM role_bindings AS binding
                JOIN principals AS principal ON principal.id = binding.principal_id
                WHERE binding.workspace_id = OLD.workspace_id
                  AND binding.task_id IS NULL
                  AND binding.role::text = 'admin'
                  AND binding.id <> OLD.id
                  AND principal.is_active
            ) THEN
                RAISE EXCEPTION 'the last workspace admin cannot be removed'
                    USING ERRCODE = '23000';
            END IF;
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_role_bindings_last_workspace_admin
        BEFORE UPDATE OR DELETE ON role_bindings
        FOR EACH ROW EXECUTE FUNCTION lls_enforce_last_workspace_admin()
        """
    )
    op.execute("REVOKE EXECUTE ON FUNCTION lls_enforce_last_workspace_admin() FROM PUBLIC")


def _create_sqlite_last_admin_triggers() -> None:
    op.execute(
        """
        CREATE TRIGGER trg_role_bindings_last_workspace_admin_update
        BEFORE UPDATE OF workspace_id, principal_id, task_id, role ON role_bindings
        WHEN OLD.task_id IS NULL
          AND OLD.role = 'admin'
          AND NOT EXISTS (
              SELECT 1 FROM principals
              WHERE id = OLD.principal_id AND is_active = 0
          )
          AND NOT (
              NEW.workspace_id = OLD.workspace_id
              AND NEW.task_id IS NULL
              AND NEW.role = 'admin'
              AND EXISTS (
                  SELECT 1 FROM principals
                  WHERE id = NEW.principal_id AND is_active = 1
              )
          )
          AND EXISTS (SELECT 1 FROM workspaces WHERE id = OLD.workspace_id)
          AND NOT EXISTS (
              SELECT 1
              FROM role_bindings AS binding
              JOIN principals AS principal ON principal.id = binding.principal_id
              WHERE binding.workspace_id = OLD.workspace_id
                AND binding.task_id IS NULL
                AND binding.role = 'admin'
                AND binding.id <> OLD.id
                AND principal.is_active = 1
          )
        BEGIN
            SELECT RAISE(ABORT, 'the last workspace admin cannot be removed');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_role_bindings_last_workspace_admin_delete
        BEFORE DELETE ON role_bindings
        WHEN OLD.task_id IS NULL
          AND OLD.role = 'admin'
          AND NOT EXISTS (
              SELECT 1 FROM principals
              WHERE id = OLD.principal_id AND is_active = 0
          )
          AND EXISTS (SELECT 1 FROM workspaces WHERE id = OLD.workspace_id)
          AND NOT EXISTS (
              SELECT 1
              FROM role_bindings AS binding
              JOIN principals AS principal ON principal.id = binding.principal_id
              WHERE binding.workspace_id = OLD.workspace_id
                AND binding.task_id IS NULL
                AND binding.role = 'admin'
                AND binding.id <> OLD.id
                AND principal.is_active = 1
          )
        BEGIN
            SELECT RAISE(ABORT, 'the last workspace admin cannot be removed');
        END
        """
    )
