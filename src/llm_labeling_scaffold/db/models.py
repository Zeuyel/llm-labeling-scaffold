from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    DDL,
    Enum as SqlEnum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    event,
    func,
    text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .enums import (
    AuditActorType,
    AuditChannel,
    IdempotencyState,
    MigrationStatus,
    PrincipalType,
    Role,
    TaskStatus,
)


JSON_DOCUMENT = JSON().with_variant(postgresql.JSONB(), "postgresql")


def _enum_type(enum_class: type, name: str) -> SqlEnum:
    return SqlEnum(
        enum_class,
        name=name,
        values_callable=lambda values: [value.value for value in values],
        validate_strings=True,
        create_constraint=True,
    )


class Principal(Base):
    __tablename__ = "principals"
    __table_args__ = (
        UniqueConstraint("issuer", "subject", name="uq_principals_issuer_subject"),
        CheckConstraint("length(trim(issuer)) > 0", name="issuer_not_blank"),
        CheckConstraint("length(trim(subject)) > 0", name="subject_not_blank"),
        Index("ix_principals_type_active", "principal_type", "is_active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    issuer: Mapped[str] = mapped_column(String(255), nullable=False)
    subject: Mapped[str] = mapped_column(String(512), nullable=False)
    principal_type: Mapped[PrincipalType] = mapped_column(
        _enum_type(PrincipalType, "principal_type"),
        nullable=False,
    )
    display_name: Mapped[str | None] = mapped_column(String(255))
    email_snapshot: Mapped[str | None] = mapped_column(String(320))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class Workspace(Base):
    __tablename__ = "workspaces"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_workspaces_slug"),
        CheckConstraint("length(trim(slug)) > 0", name="slug_not_blank"),
        CheckConstraint("length(trim(name)) > 0", name="name_not_blank"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(63), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_tasks_workspace_id_id"),
        UniqueConstraint("workspace_id", "task_key", name="uq_tasks_workspace_task_key"),
        CheckConstraint("length(trim(task_key)) > 0", name="task_key_not_blank"),
        CheckConstraint("length(trim(name)) > 0", name="name_not_blank"),
        Index("ix_tasks_workspace_status", "workspace_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    task_key: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[TaskStatus] = mapped_column(
        _enum_type(TaskStatus, "task_status"),
        nullable=False,
        default=TaskStatus.DRAFT,
        server_default=TaskStatus.DRAFT.value,
    )
    created_by_principal_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("principals.id", ondelete="RESTRICT"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class RoleBinding(Base):
    __tablename__ = "role_bindings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "task_id"],
            ["tasks.workspace_id", "tasks.id"],
            name="fk_role_bindings_workspace_task",
            ondelete="CASCADE",
        ),
        Index(
            "uq_role_bindings_workspace_principal",
            "workspace_id",
            "principal_id",
            unique=True,
            postgresql_where=text("task_id IS NULL"),
            sqlite_where=text("task_id IS NULL"),
        ),
        Index(
            "uq_role_bindings_task_principal",
            "workspace_id",
            "task_id",
            "principal_id",
            unique=True,
            postgresql_where=text("task_id IS NOT NULL"),
            sqlite_where=text("task_id IS NOT NULL"),
        ),
        Index("ix_role_bindings_principal_workspace", "principal_id", "workspace_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    principal_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("principals.id", ondelete="CASCADE"),
        nullable=False,
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    role: Mapped[Role] = mapped_column(_enum_type(Role, "role_name"), nullable=False)
    created_by_principal_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("principals.id", ondelete="RESTRICT"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class TaskRevision(Base):
    __tablename__ = "task_revisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "task_id"],
            ["tasks.workspace_id", "tasks.id"],
            name="fk_task_revisions_workspace_task",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "workspace_id",
            "task_id",
            "revision",
            name="uq_task_revisions_workspace_task_revision",
        ),
        CheckConstraint("revision > 0", name="revision_positive"),
        Index("ix_task_revisions_task_created", "workspace_id", "task_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    definition: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    created_by_principal_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("principals.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "operation",
            "idempotency_key",
            name="uq_idempotency_records_workspace_operation_key",
        ),
        CheckConstraint("length(trim(operation)) > 0", name="operation_not_blank"),
        CheckConstraint("length(trim(idempotency_key)) > 0", name="key_not_blank"),
        Index("ix_idempotency_records_workspace_state", "workspace_id", "state"),
        Index(
            "ix_idempotency_records_actor_caller",
            "workspace_id",
            "actor_principal_id",
            "caller_principal_id",
        ),
        Index("ix_idempotency_records_expires_at", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    actor_principal_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("principals.id", ondelete="RESTRICT"),
        nullable=False,
    )
    caller_principal_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("principals.id", ondelete="RESTRICT"),
        nullable=False,
    )
    operation: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[IdempotencyState] = mapped_column(
        _enum_type(IdempotencyState, "idempotency_state"),
        nullable=False,
        default=IdempotencyState.PENDING,
        server_default=IdempotencyState.PENDING.value,
    )
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkspaceSetting(Base):
    __tablename__ = "workspace_settings"
    __table_args__ = (
        UniqueConstraint("workspace_id", "setting_key", name="uq_workspace_settings_workspace_key"),
        CheckConstraint("length(trim(setting_key)) > 0", name="setting_key_not_blank"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    setting_key: Mapped[str] = mapped_column(String(255), nullable=False)
    setting_value: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    updated_by_principal_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("principals.id", ondelete="RESTRICT"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint("length(trim(actor_issuer)) > 0", name="actor_issuer_not_blank"),
        CheckConstraint("length(trim(actor_subject)) > 0", name="actor_subject_not_blank"),
        CheckConstraint("length(trim(event_type)) > 0", name="event_type_not_blank"),
        Index("ix_audit_events_workspace_occurred", "workspace_id", "occurred_at"),
        Index("ix_audit_events_actor_occurred", "actor_principal_id", "occurred_at"),
        Index("ix_audit_events_caller_occurred", "caller_principal_id", "occurred_at"),
        Index("ix_audit_events_resource", "workspace_id", "resource_type", "resource_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    actor_type: Mapped[AuditActorType] = mapped_column(
        _enum_type(AuditActorType, "audit_actor_type"),
        nullable=False,
    )
    actor_principal_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("principals.id", ondelete="RESTRICT"),
    )
    caller_principal_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("principals.id", ondelete="RESTRICT"),
    )
    channel: Mapped[AuditChannel] = mapped_column(
        _enum_type(AuditChannel, "audit_channel"),
        nullable=False,
    )
    actor_issuer: Mapped[str] = mapped_column(String(255), nullable=False)
    actor_subject: Mapped[str] = mapped_column(String(512), nullable=False)
    actor_display_name: Mapped[str | None] = mapped_column(String(255))
    actor_email_snapshot: Mapped[str | None] = mapped_column(String(320))
    event_type: Mapped[str] = mapped_column(String(255), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(255))
    resource_id: Mapped[str | None] = mapped_column(String(255))
    request_id: Mapped[str | None] = mapped_column(String(255))
    details: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class MigrationRun(Base):
    __tablename__ = "migration_runs"
    __table_args__ = (Index("ix_migration_runs_started_at", "started_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    command: Mapped[str] = mapped_column(String(64), nullable=False)
    target_revision: Mapped[str] = mapped_column(String(255), nullable=False)
    applied_revision: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[MigrationStatus] = mapped_column(
        _enum_type(MigrationStatus, "migration_status"),
        nullable=False,
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class ImmutableAuditEventError(RuntimeError):
    pass


event.listen(
    AuditEvent.__table__,
    "after_create",
    DDL(
        """
        CREATE TRIGGER trg_audit_events_append_only_update
        BEFORE UPDATE ON audit_events
        BEGIN
            SELECT RAISE(ABORT, 'audit_events is append-only');
        END
        """
    ).execute_if(dialect="sqlite"),
)
event.listen(
    AuditEvent.__table__,
    "after_create",
    DDL(
        """
        CREATE TRIGGER trg_audit_events_append_only_delete
        BEFORE DELETE ON audit_events
        BEGIN
            SELECT RAISE(ABORT, 'audit_events is append-only');
        END
        """
    ).execute_if(dialect="sqlite"),
)


@event.listens_for(AuditEvent, "before_update")
@event.listens_for(AuditEvent, "before_delete")
def _reject_audit_event_mutation(mapper, connection, target) -> None:
    raise ImmutableAuditEventError("audit events are append-only")
