"""Add one-time workspace email invitations.

Revision ID: 20260721_0008
Revises: 20260717_0007
Create Date: 2026-07-21
"""

from __future__ import annotations

from collections.abc import Sequence
import os

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from llm_labeling_scaffold.db.idempotency_boundary import POSTGRES_IDEMPOTENCY_CREATE_FUNCTIONS


revision: str = "20260721_0008"
down_revision: str | None = "20260717_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ROLE_VALUES = ("viewer", "annotator", "experimenter", "admin")
_STATUS_VALUES = ("pending", "claimed", "revoked", "expired")


def _role_enum() -> sa.TypeEngine:
    if op.get_bind().dialect.name == "postgresql":
        return postgresql.ENUM(*_ROLE_VALUES, name="role_name", create_type=False)
    return sa.Enum(*_ROLE_VALUES, name="role_name", native_enum=False, create_constraint=True)


def _status_enum() -> sa.TypeEngine:
    if op.get_bind().dialect.name == "postgresql":
        enum_type = postgresql.ENUM(*_STATUS_VALUES, name="workspace_invitation_status")
        enum_type.create(op.get_bind(), checkfirst=True)
        return postgresql.ENUM(
            *_STATUS_VALUES,
            name="workspace_invitation_status",
            create_type=False,
        )
    return sa.Enum(
        *_STATUS_VALUES,
        name="workspace_invitation_status",
        native_enum=False,
        create_constraint=True,
    )


def upgrade() -> None:
    op.create_table(
        "workspace_invitations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("role", _role_enum(), nullable=False),
        sa.Column(
            "status",
            _status_enum(),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by_principal_id", sa.Uuid(), nullable=False),
        sa.Column("claimed_principal_id", sa.Uuid(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("length(trim(email)) > 0", name="invitation_email_not_blank"),
        sa.CheckConstraint("length(trim(email)) <= 320", name="invitation_email_length"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_workspace_invitations_workspace_id_workspaces",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_principal_id"],
            ["principals.id"],
            name="fk_workspace_invitations_created_by_principal_id_principals",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["claimed_principal_id"],
            ["principals.id"],
            name="fk_workspace_invitations_claimed_principal_id_principals",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_workspace_invitations"),
        sa.UniqueConstraint(
            "workspace_id",
            "email",
            "status",
            name="uq_workspace_invitations_workspace_email_status",
        ),
    )
    op.create_index(
        "uq_workspace_invitations_pending_email",
        "workspace_invitations",
        ["workspace_id", "email"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
        sqlite_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_workspace_invitations_workspace_status_expiry",
        "workspace_invitations",
        ["workspace_id", "status", "expires_at"],
    )
    op.create_index(
        "ix_workspace_invitations_claimed_principal",
        "workspace_invitations",
        ["claimed_principal_id"],
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(POSTGRES_IDEMPOTENCY_CREATE_FUNCTIONS[4])
        _grant_runtime_table_privileges()


def downgrade() -> None:
    op.drop_index(
        "ix_workspace_invitations_claimed_principal",
        table_name="workspace_invitations",
    )
    op.drop_index(
        "ix_workspace_invitations_workspace_status_expiry",
        table_name="workspace_invitations",
    )
    op.drop_index(
        "uq_workspace_invitations_pending_email",
        table_name="workspace_invitations",
    )
    op.drop_table("workspace_invitations")
    if op.get_bind().dialect.name == "postgresql":
        postgresql.ENUM(name="workspace_invitation_status").drop(
            op.get_bind(),
            checkfirst=True,
        )


def _grant_runtime_table_privileges() -> None:
    app_role = os.environ.get("SCAFFOLD_POSTGRES_APP_USER", "").strip()
    if not app_role:
        raise RuntimeError("SCAFFOLD_POSTGRES_APP_USER is required for PostgreSQL migrations")
    role_literal = '"' + app_role.replace('"', '""') + '"'
    op.execute(
        sa.text(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.workspace_invitations TO {role_literal}"
        )
    )
