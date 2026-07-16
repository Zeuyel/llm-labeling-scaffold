from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
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
    Numeric,
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
    AllocationAssignmentRole,
    AllocationDatasetState,
    AllocationManifestKind,
    AllocationPhase,
    AllocationPlanLifecycle,
    AllocationStrategy,
    AllocationWorkspaceMode,
    AnnotatorCohortState,
    AnnotatorMappingState,
    AnnotatorVerificationState,
    ArgillaBindingState,
    AuditActorType,
    AuditChannel,
    CollectionDisposition,
    IdempotencyState,
    MigrationStatus,
    PrincipalType,
    Role,
    TaskMaterializationState,
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
        ForeignKeyConstraint(
            ["workspace_id", "id", "current_revision_id"],
            ["task_revisions.workspace_id", "task_revisions.task_id", "task_revisions.id"],
            name="fk_tasks_current_revision_id_task_revisions",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        UniqueConstraint("workspace_id", "id", name="uq_tasks_workspace_id_id"),
        UniqueConstraint("task_key", name="uq_tasks_task_key"),
        CheckConstraint("length(trim(task_key)) > 0", name="task_key_not_blank"),
        CheckConstraint("length(trim(name)) > 0", name="name_not_blank"),
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
    current_revision_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
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
        ForeignKey("principals.id", ondelete="RESTRICT"),
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


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "operation",
            "idempotency_key_hash",
            name="uq_idempotency_records_workspace_operation_key_hash",
        ),
        CheckConstraint("length(trim(operation)) > 0", name="operation_not_blank"),
        CheckConstraint("length(idempotency_key_hash) = 64", name="key_hash_sha256"),
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
    idempotency_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
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


class TaskDraft(Base):
    __tablename__ = "task_drafts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "task_id"],
            ["tasks.workspace_id", "tasks.id"],
            name="fk_task_drafts_workspace_task",
            ondelete="CASCADE",
        ),
        UniqueConstraint("task_id", name="uq_task_drafts_task_id"),
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint("length(fingerprint) = 64", name="fingerprint_sha256"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    definition: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    rendered_task: Mapped[str] = mapped_column(Text, nullable=False)
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


class TaskRevision(Base):
    __tablename__ = "task_revisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "task_id"],
            ["tasks.workspace_id", "tasks.id"],
            name="fk_task_revisions_workspace_task",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "task_id", "previous_revision_id"],
            ["task_revisions.workspace_id", "task_revisions.task_id", "task_revisions.id"],
            name="fk_task_revisions_previous_revision",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "workspace_id",
            "task_id",
            "id",
            name="uq_task_revisions_workspace_task_id",
        ),
        UniqueConstraint(
            "task_id",
            "revision_number",
            name="uq_task_revisions_task_number",
        ),
        UniqueConstraint("idempotency_record_id", name="uq_task_revisions_idempotency_record_id"),
        CheckConstraint("revision_number > 0", name="revision_number_positive"),
        CheckConstraint("draft_version > 0", name="draft_version_positive"),
        CheckConstraint("length(draft_fingerprint) = 64", name="draft_fingerprint_sha256"),
        CheckConstraint("length(content_hash) = 64", name="content_hash_sha256"),
        CheckConstraint("length(trim(reason)) > 0", name="reason_not_blank"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    draft_version: Mapped[int] = mapped_column(Integer, nullable=False)
    draft_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    definition: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    rendered_task: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
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
    channel: Mapped[AuditChannel] = mapped_column(
        _enum_type(AuditChannel, "audit_channel"),
        nullable=False,
    )
    previous_revision_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_record_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("idempotency_records.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class TaskRevisionMaterialization(Base):
    __tablename__ = "task_revision_materializations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "task_id"],
            ["tasks.workspace_id", "tasks.id"],
            name="fk_task_revision_materializations_workspace_task",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "task_id", "revision_id"],
            ["task_revisions.workspace_id", "task_revisions.task_id", "task_revisions.id"],
            name="fk_task_revision_materializations_revision",
            ondelete="CASCADE",
        ),
        UniqueConstraint("revision_id", name="uq_task_revision_materializations_revision_id"),
        CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
        Index(
            "ix_task_revision_materializations_pending",
            "state",
            "available_at",
            "created_at",
        ),
        Index(
            "ix_task_revision_materializations_workspace_task",
            "workspace_id",
            "task_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    revision_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    state: Mapped[TaskMaterializationState] = mapped_column(
        _enum_type(TaskMaterializationState, "task_materialization_state"),
        nullable=False,
        default=TaskMaterializationState.PENDING,
        server_default=TaskMaterializationState.PENDING.value,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_by: Mapped[str | None] = mapped_column(String(255))
    last_error: Mapped[str | None] = mapped_column(Text)
    materialized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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


class ArgillaConnectionBinding(Base):
    __tablename__ = "argilla_connection_bindings"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_argilla_connection_bindings_workspace_id"),
        UniqueConstraint(
            "workspace_id",
            "connection_config_id",
            name="uq_argilla_connection_bindings_workspace_config",
        ),
        UniqueConstraint("idempotency_record_id", name="uq_argilla_connection_bindings_idempotency"),
        CheckConstraint("length(trim(connection_config_id)) > 0", name="config_id_not_blank"),
        CheckConstraint("length(trim(server_version)) > 0", name="server_version_not_blank"),
        CheckConstraint(
            "argilla_workspace_name_snapshot IS NULL "
            "OR length(trim(argilla_workspace_name_snapshot)) > 0",
            name="workspace_name_not_blank",
        ),
        CheckConstraint(
            "(argilla_workspace_id IS NULL AND argilla_workspace_name_snapshot IS NULL) OR "
            "(argilla_workspace_id IS NOT NULL AND argilla_workspace_name_snapshot IS NOT NULL)",
            name="remote_workspace_complete",
        ),
        Index("ix_argilla_connection_bindings_workspace_state", "workspace_id", "state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    connection_config_id: Mapped[str] = mapped_column(String(255), nullable=False)
    server_version: Mapped[str] = mapped_column(String(64), nullable=False)
    argilla_workspace_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    argilla_workspace_name_snapshot: Mapped[str | None] = mapped_column(String(255))
    state: Mapped[ArgillaBindingState] = mapped_column(
        _enum_type(ArgillaBindingState, "argilla_binding_state"),
        nullable=False,
        default=ArgillaBindingState.ACTIVE,
        server_default=ArgillaBindingState.ACTIVE.value,
    )
    idempotency_record_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "idempotency_records.id",
            name="fk_argilla_connection_bindings_idempotency",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ArgillaAnnotatorMapping(Base):
    __tablename__ = "argilla_annotator_mappings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "connection_binding_id"],
            ["argilla_connection_bindings.workspace_id", "argilla_connection_bindings.id"],
            name="fk_argilla_annotator_mappings_workspace_binding",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_argilla_annotator_mappings_workspace_id"),
        UniqueConstraint("idempotency_record_id", name="uq_argilla_annotator_mappings_idempotency"),
        CheckConstraint("length(trim(username_snapshot)) > 0", name="username_not_blank"),
        CheckConstraint(
            "(verification_state = 'verified' AND verified_by_principal_id IS NOT NULL "
            "AND verified_at IS NOT NULL) OR "
            "(verification_state <> 'verified' AND verified_at IS NULL)",
            name="verification_metadata",
        ),
        Index(
            "uq_argilla_annotator_mappings_binding_user",
            "connection_binding_id",
            "argilla_user_id",
            unique=True,
            postgresql_where=text("argilla_user_id IS NOT NULL"),
            sqlite_where=text("argilla_user_id IS NOT NULL"),
        ),
        Index(
            "uq_argilla_annotator_mappings_binding_principal",
            "connection_binding_id",
            "principal_id",
            unique=True,
            postgresql_where=text("principal_id IS NOT NULL"),
            sqlite_where=text("principal_id IS NOT NULL"),
        ),
        Index("ix_argilla_annotator_mappings_workspace_state", "workspace_id", "state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    connection_binding_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    principal_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("principals.id", ondelete="RESTRICT")
    )
    argilla_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    username_snapshot: Mapped[str] = mapped_column(String(255), nullable=False)
    personal_argilla_workspace_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    state: Mapped[AnnotatorMappingState] = mapped_column(
        _enum_type(AnnotatorMappingState, "annotator_mapping_state"),
        nullable=False,
        default=AnnotatorMappingState.ACTIVE,
        server_default=AnnotatorMappingState.ACTIVE.value,
    )
    verification_state: Mapped[AnnotatorVerificationState] = mapped_column(
        _enum_type(AnnotatorVerificationState, "annotator_verification_state"),
        nullable=False,
        default=AnnotatorVerificationState.UNVERIFIED,
        server_default=AnnotatorVerificationState.UNVERIFIED.value,
    )
    verified_by_principal_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("principals.id", name="fk_argilla_annotator_mappings_verifier", ondelete="RESTRICT"),
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    idempotency_record_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "idempotency_records.id",
            name="fk_argilla_annotator_mappings_idempotency",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class AnnotatorCohort(Base):
    __tablename__ = "annotator_cohorts"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_annotator_cohorts_workspace_id"),
        UniqueConstraint("workspace_id", "name", name="uq_annotator_cohorts_workspace_name"),
        UniqueConstraint("idempotency_record_id", name="uq_annotator_cohorts_idempotency"),
        CheckConstraint("length(trim(name)) > 0", name="name_not_blank"),
        Index("ix_annotator_cohorts_workspace_state", "workspace_id", "state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    state: Mapped[AnnotatorCohortState] = mapped_column(
        _enum_type(AnnotatorCohortState, "annotator_cohort_state"),
        nullable=False,
        default=AnnotatorCohortState.ACTIVE,
        server_default=AnnotatorCohortState.ACTIVE.value,
    )
    idempotency_record_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("idempotency_records.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class AnnotatorCohortRevision(Base):
    __tablename__ = "annotator_cohort_revisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "cohort_id"],
            ["annotator_cohorts.workspace_id", "annotator_cohorts.id"],
            name="fk_annotator_cohort_revisions_workspace_cohort",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "connection_binding_id"],
            ["argilla_connection_bindings.workspace_id", "argilla_connection_bindings.id"],
            name="fk_annotator_cohort_revisions_workspace_binding",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_annotator_cohort_revisions_workspace_id"),
        UniqueConstraint("cohort_id", "revision_number", name="uq_annotator_cohort_revisions_number"),
        UniqueConstraint("cohort_id", "fingerprint", name="uq_annotator_cohort_revisions_fingerprint"),
        UniqueConstraint("idempotency_record_id", name="uq_annotator_cohort_revisions_idempotency"),
        CheckConstraint("revision_number > 0", name="revision_number_positive"),
        CheckConstraint("member_count > 0", name="member_count_positive"),
        CheckConstraint("length(fingerprint) = 64", name="fingerprint_sha256"),
        CheckConstraint(
            "(sealed_at IS NULL AND connection_binding_id IS NULL) OR "
            "(sealed_at IS NOT NULL AND connection_binding_id IS NOT NULL)",
            name="seal_binding_complete",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    cohort_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    member_count: Mapped[int] = mapped_column(Integer, nullable=False)
    connection_binding_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    actor_principal_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("principals.id", ondelete="RESTRICT"), nullable=False
    )
    caller_principal_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("principals.id", ondelete="RESTRICT"), nullable=False
    )
    channel: Mapped[AuditChannel] = mapped_column(_enum_type(AuditChannel, "audit_channel"), nullable=False)
    idempotency_record_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "idempotency_records.id",
            name="fk_annotator_cohort_revisions_idempotency",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    sealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AnnotatorCohortMember(Base):
    __tablename__ = "annotator_cohort_members"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "cohort_revision_id"],
            ["annotator_cohort_revisions.workspace_id", "annotator_cohort_revisions.id"],
            name="fk_annotator_cohort_members_workspace_revision",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "annotator_mapping_id"],
            ["argilla_annotator_mappings.workspace_id", "argilla_annotator_mappings.id"],
            name="fk_annotator_cohort_members_workspace_mapping",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "cohort_revision_id", "annotator_mapping_id", name="uq_annotator_cohort_members_revision_mapping"
        ),
        CheckConstraint("default_capacity >= 0", name="default_capacity_nonnegative"),
        Index("ix_annotator_cohort_members_mapping", "workspace_id", "annotator_mapping_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    cohort_revision_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    annotator_mapping_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    default_capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AllocationPlan(Base):
    __tablename__ = "allocation_plans"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "connection_binding_id"],
            ["argilla_connection_bindings.workspace_id", "argilla_connection_bindings.id"],
            name="fk_allocation_plans_workspace_binding",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "task_id"],
            ["tasks.workspace_id", "tasks.id"],
            name="fk_allocation_plans_workspace_task",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "task_id", "task_revision_id"],
            ["task_revisions.workspace_id", "task_revisions.task_id", "task_revisions.id"],
            name="fk_allocation_plans_task_revision",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "cohort_revision_id"],
            ["annotator_cohort_revisions.workspace_id", "annotator_cohort_revisions.id"],
            name="fk_allocation_plans_cohort_revision",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_allocation_plans_workspace_id"),
        UniqueConstraint("workspace_id", "fingerprint", name="uq_allocation_plans_workspace_fingerprint"),
        UniqueConstraint("idempotency_record_id", name="uq_allocation_plans_idempotency"),
        CheckConstraint("length(trim(schema_version)) > 0", name="schema_version_not_blank"),
        CheckConstraint("length(task_revision_hash) = 64", name="task_revision_hash_sha256"),
        CheckConstraint("length(trim(source_manifest_id)) > 0", name="source_manifest_id_not_blank"),
        CheckConstraint("length(source_manifest_hash) = 64", name="source_manifest_hash_sha256"),
        CheckConstraint("length(trim(algorithm_version)) > 0", name="algorithm_version_not_blank"),
        CheckConstraint("length(input_fingerprint) = 64", name="input_fingerprint_sha256"),
        CheckConstraint("length(fingerprint) = 64", name="fingerprint_sha256"),
        Index("ix_allocation_plans_task_created", "workspace_id", "task_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    connection_binding_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    task_revision_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    task_revision_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_manifest_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_manifest_kind: Mapped[AllocationManifestKind] = mapped_column(
        _enum_type(AllocationManifestKind, "allocation_manifest_kind"), nullable=False
    )
    source_manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    cohort_revision_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    strategy: Mapped[AllocationStrategy] = mapped_column(
        _enum_type(AllocationStrategy, "allocation_strategy"), nullable=False
    )
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    seed: Mapped[Decimal] = mapped_column(Numeric(precision=1000, scale=0), nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(128), nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    capacity_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(JSON_DOCUMENT, nullable=False)
    qc_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    actor_principal_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("principals.id", ondelete="RESTRICT"), nullable=False
    )
    caller_principal_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("principals.id", ondelete="RESTRICT"), nullable=False
    )
    channel: Mapped[AuditChannel] = mapped_column(_enum_type(AuditChannel, "audit_channel"), nullable=False)
    idempotency_record_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("idempotency_records.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AllocationPlanState(Base):
    __tablename__ = "allocation_plan_states"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "plan_id"],
            ["allocation_plans.workspace_id", "allocation_plans.id"],
            name="fk_allocation_plan_states_workspace_plan",
            ondelete="CASCADE",
        ),
        UniqueConstraint("confirmation_idempotency_record_id", name="uq_allocation_plan_states_confirmation"),
        CheckConstraint(
            "(lifecycle_state = 'draft' AND confirmed_at IS NULL "
            "AND confirmed_by_principal_id IS NULL "
            "AND confirmed_via_caller_principal_id IS NULL "
            "AND confirmation_idempotency_record_id IS NULL) OR "
            "(lifecycle_state = 'confirmed' AND confirmed_at IS NOT NULL "
            "AND confirmed_by_principal_id IS NOT NULL "
            "AND confirmed_via_caller_principal_id IS NOT NULL "
            "AND confirmation_idempotency_record_id IS NOT NULL)",
            name="lifecycle_confirmation_metadata",
        ),
        Index("ix_allocation_plan_states_workspace_lifecycle", "workspace_id", "lifecycle_state"),
    )

    plan_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False
    )
    lifecycle_state: Mapped[AllocationPlanLifecycle] = mapped_column(
        _enum_type(AllocationPlanLifecycle, "allocation_plan_lifecycle"),
        nullable=False,
        default=AllocationPlanLifecycle.DRAFT,
        server_default=AllocationPlanLifecycle.DRAFT.value,
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_by_principal_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("principals.id", ondelete="RESTRICT")
    )
    confirmed_via_caller_principal_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("principals.id", name="fk_allocation_plan_states_confirmation_caller", ondelete="RESTRICT"),
    )
    confirmation_idempotency_record_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "idempotency_records.id",
            name="fk_allocation_plan_states_confirmation_idempotency",
            ondelete="RESTRICT",
        ),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class AllocationWorkspaceGroup(Base):
    __tablename__ = "allocation_workspace_groups"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "plan_id"],
            ["allocation_plans.workspace_id", "allocation_plans.id"],
            name="fk_allocation_workspace_groups_workspace_plan",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "assignee_mapping_id"],
            ["argilla_annotator_mappings.workspace_id", "argilla_annotator_mappings.id"],
            name="fk_allocation_workspace_groups_workspace_assignee",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("workspace_id", "plan_id", "id", name="uq_allocation_workspace_groups_scope_id"),
        UniqueConstraint("plan_id", "planner_workspace_group_id", name="uq_allocation_workspace_groups_plan_key"),
        CheckConstraint("length(trim(planner_workspace_group_id)) > 0", name="planner_key_not_blank"),
        CheckConstraint(
            "(mode = 'calibration' AND phase = 'calibration' AND assignee_mapping_id IS NULL) OR "
            "(mode = 'personal' AND phase = 'production' AND assignee_mapping_id IS NOT NULL) OR "
            "(mode = 'shared' AND phase = 'production' AND assignee_mapping_id IS NULL)",
            name="mode_phase_target",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    plan_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    planner_workspace_group_id: Mapped[str] = mapped_column(String(255), nullable=False)
    phase: Mapped[AllocationPhase] = mapped_column(_enum_type(AllocationPhase, "allocation_phase"), nullable=False)
    mode: Mapped[AllocationWorkspaceMode] = mapped_column(
        _enum_type(AllocationWorkspaceMode, "allocation_workspace_mode"), nullable=False
    )
    assignee_mapping_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    member_mapping_ids: Mapped[list[str]] = mapped_column(JSON_DOCUMENT, nullable=False)


class AllocationDatasetGroup(Base):
    __tablename__ = "allocation_dataset_groups"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "plan_id", "workspace_group_id"],
            [
                "allocation_workspace_groups.workspace_id",
                "allocation_workspace_groups.plan_id",
                "allocation_workspace_groups.id",
            ],
            name="fk_allocation_dataset_groups_workspace_group",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "assignee_mapping_id"],
            ["argilla_annotator_mappings.workspace_id", "argilla_annotator_mappings.id"],
            name="fk_allocation_dataset_groups_workspace_assignee",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("workspace_id", "plan_id", "id", name="uq_allocation_dataset_groups_scope_id"),
        UniqueConstraint("plan_id", "planner_dataset_group_id", name="uq_allocation_dataset_groups_plan_key"),
        CheckConstraint("length(trim(planner_dataset_group_id)) > 0", name="planner_key_not_blank"),
        CheckConstraint("min_submitted > 0", name="min_submitted_positive"),
        CheckConstraint("(assignee_mapping_id IS NULL) <> (pool_id IS NULL)", name="assignee_or_pool"),
        Index("ix_allocation_dataset_groups_plan_phase", "plan_id", "phase"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    plan_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    workspace_group_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    planner_dataset_group_id: Mapped[str] = mapped_column(String(255), nullable=False)
    phase: Mapped[AllocationPhase] = mapped_column(_enum_type(AllocationPhase, "allocation_phase"), nullable=False)
    min_submitted: Mapped[int] = mapped_column(Integer, nullable=False)
    assignee_mapping_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    pool_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))


class AllocationDatasetGroupState(Base):
    __tablename__ = "allocation_dataset_group_states"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "plan_id", "dataset_group_id"],
            [
                "allocation_dataset_groups.workspace_id",
                "allocation_dataset_groups.plan_id",
                "allocation_dataset_groups.id",
            ],
            name="fk_allocation_dataset_group_states_group",
            ondelete="CASCADE",
        ),
        UniqueConstraint("argilla_dataset_id", name="uq_allocation_dataset_group_states_argilla_dataset"),
        CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
        CheckConstraint(
            "(argilla_workspace_id IS NULL AND argilla_dataset_id IS NULL) OR "
            "(argilla_workspace_id IS NOT NULL AND argilla_dataset_id IS NOT NULL)",
            name="remote_binding_complete",
        ),
        Index("ix_allocation_dataset_group_states_pending", "materialization_state", "updated_at"),
    )

    dataset_group_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    plan_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    materialization_state: Mapped[AllocationDatasetState] = mapped_column(
        _enum_type(AllocationDatasetState, "allocation_dataset_state"),
        nullable=False,
        default=AllocationDatasetState.PENDING,
        server_default=AllocationDatasetState.PENDING.value,
    )
    argilla_workspace_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    argilla_dataset_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    materialized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class AllocationAssignmentItem(Base):
    __tablename__ = "allocation_assignment_items"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "plan_id", "dataset_group_id"],
            [
                "allocation_dataset_groups.workspace_id",
                "allocation_dataset_groups.plan_id",
                "allocation_dataset_groups.id",
            ],
            name="fk_allocation_assignment_items_dataset_group",
            ondelete="CASCADE",
        ),
        UniqueConstraint("workspace_id", "plan_id", "id", name="uq_allocation_assignment_items_scope_id"),
        UniqueConstraint("dataset_group_id", "record_id", name="uq_allocation_assignment_items_group_record"),
        UniqueConstraint("dataset_group_id", "external_id", name="uq_allocation_assignment_items_group_external"),
        CheckConstraint("length(trim(record_id)) > 0", name="record_id_not_blank"),
        CheckConstraint("length(trim(external_id)) > 0", name="external_id_not_blank"),
        CheckConstraint("length(trim(batch_id)) > 0", name="batch_id_not_blank"),
        CheckConstraint("length(content_hash) = 64", name="content_hash_sha256"),
        Index("ix_allocation_assignment_items_plan_group", "plan_id", "dataset_group_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    plan_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    dataset_group_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    record_id: Mapped[str] = mapped_column(String(512), nullable=False)
    external_id: Mapped[str] = mapped_column(String(512), nullable=False)
    batch_id: Mapped[str] = mapped_column(String(255), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class AllocationRecordBinding(Base):
    __tablename__ = "allocation_record_bindings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "plan_id", "item_id"],
            [
                "allocation_assignment_items.workspace_id",
                "allocation_assignment_items.plan_id",
                "allocation_assignment_items.id",
            ],
            name="fk_allocation_record_bindings_item",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "(argilla_dataset_id IS NULL AND argilla_record_id IS NULL) OR "
            "(argilla_dataset_id IS NOT NULL AND argilla_record_id IS NOT NULL)",
            name="remote_binding_complete",
        ),
        CheckConstraint(
            "(argilla_record_id IS NULL AND bound_at IS NULL) OR "
            "(argilla_record_id IS NOT NULL AND bound_at IS NOT NULL)",
            name="bound_at_complete",
        ),
        Index(
            "uq_allocation_record_bindings_remote_record",
            "argilla_dataset_id",
            "argilla_record_id",
            unique=True,
            postgresql_where=text("argilla_record_id IS NOT NULL"),
            sqlite_where=text("argilla_record_id IS NOT NULL"),
        ),
    )

    item_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    argilla_dataset_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    argilla_record_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    bound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class AllocationAssignment(Base):
    __tablename__ = "allocation_assignments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "plan_id", "item_id"],
            [
                "allocation_assignment_items.workspace_id",
                "allocation_assignment_items.plan_id",
                "allocation_assignment_items.id",
            ],
            name="fk_allocation_assignments_item",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "assignee_mapping_id"],
            ["argilla_annotator_mappings.workspace_id", "argilla_annotator_mappings.id"],
            name="fk_allocation_assignments_workspace_assignee",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("workspace_id", "plan_id", "id", name="uq_allocation_assignments_scope_id"),
        UniqueConstraint("plan_id", "planner_assignment_id", name="uq_allocation_assignments_plan_key"),
        CheckConstraint("length(trim(planner_assignment_id)) > 0", name="planner_key_not_blank"),
        CheckConstraint("required_submissions > 0", name="required_submissions_positive"),
        CheckConstraint(
            "(assignee_mapping_id IS NOT NULL AND pool_id IS NULL "
            "AND required_submissions = 1 AND role IN ('primary', 'overlap', 'calibration')) OR "
            "(assignee_mapping_id IS NULL AND pool_id IS NOT NULL "
            "AND role IN ('shared', 'overlap'))",
            name="role_target_shape",
        ),
        Index(
            "uq_allocation_assignments_item_direct_assignee",
            "item_id",
            "assignee_mapping_id",
            unique=True,
            postgresql_where=text("assignee_mapping_id IS NOT NULL"),
            sqlite_where=text("assignee_mapping_id IS NOT NULL"),
        ),
        Index(
            "uq_allocation_assignments_item_pool",
            "item_id",
            unique=True,
            postgresql_where=text("assignee_mapping_id IS NULL"),
            sqlite_where=text("assignee_mapping_id IS NULL"),
        ),
        Index("ix_allocation_assignments_plan_assignee", "plan_id", "assignee_mapping_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    plan_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    planner_assignment_id: Mapped[str] = mapped_column(String(255), nullable=False)
    item_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    phase: Mapped[AllocationPhase] = mapped_column(_enum_type(AllocationPhase, "allocation_phase"), nullable=False)
    role: Mapped[AllocationAssignmentRole] = mapped_column(
        _enum_type(AllocationAssignmentRole, "allocation_assignment_role"), nullable=False
    )
    assignee_mapping_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    pool_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    required_submissions: Mapped[int] = mapped_column(Integer, nullable=False)
    gate_id: Mapped[str | None] = mapped_column(String(255))


class AllocationCollectionReceipt(Base):
    __tablename__ = "allocation_collection_receipts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "plan_id", "item_id"],
            [
                "allocation_assignment_items.workspace_id",
                "allocation_assignment_items.plan_id",
                "allocation_assignment_items.id",
            ],
            name="fk_allocation_collection_receipts_item",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "plan_id", "assignment_id"],
            [
                "allocation_assignments.workspace_id",
                "allocation_assignments.plan_id",
                "allocation_assignments.id",
            ],
            name="fk_allocation_collection_receipts_assignment",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "connection_binding_id"],
            ["argilla_connection_bindings.workspace_id", "argilla_connection_bindings.id"],
            name="fk_allocation_collection_receipts_binding",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "connection_binding_id",
            "argilla_response_id",
            name="uq_allocation_collection_receipts_remote_response",
        ),
        UniqueConstraint("idempotency_record_id", name="uq_allocation_collection_receipts_idempotency"),
        CheckConstraint("length(trim(response_status)) > 0", name="response_status_not_blank"),
        CheckConstraint("length(labels_hash) = 64", name="labels_hash_sha256"),
        CheckConstraint(
            "(disposition = 'accepted' AND assignment_id IS NOT NULL AND rejection_reason IS NULL) OR "
            "(disposition = 'quarantined' AND rejection_reason IS NOT NULL "
            "AND length(trim(rejection_reason)) > 0)",
            name="disposition_reason",
        ),
        Index("ix_allocation_collection_receipts_item_pulled", "item_id", "pulled_at"),
        Index("ix_allocation_collection_receipts_respondent_pulled", "argilla_respondent_id", "pulled_at"),
        Index(
            "uq_allocation_collection_receipts_accepted_respondent",
            "assignment_id",
            "argilla_respondent_id",
            unique=True,
            postgresql_where=text("disposition = 'accepted' AND assignment_id IS NOT NULL"),
            sqlite_where=text("disposition = 'accepted' AND assignment_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    item_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    assignment_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    connection_binding_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    argilla_dataset_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    argilla_record_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    argilla_response_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    argilla_respondent_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    response_status: Mapped[str] = mapped_column(String(64), nullable=False)
    disposition: Mapped[CollectionDisposition] = mapped_column(
        _enum_type(CollectionDisposition, "collection_disposition"), nullable=False
    )
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    labels_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    pulled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actor_principal_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("principals.id", ondelete="RESTRICT"), nullable=False
    )
    caller_principal_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("principals.id", name="fk_allocation_collection_receipts_caller", ondelete="RESTRICT"),
        nullable=False,
    )
    channel: Mapped[AuditChannel] = mapped_column(_enum_type(AuditChannel, "audit_channel"), nullable=False)
    idempotency_record_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "idempotency_records.id",
            name="fk_allocation_collection_receipts_idempotency",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
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


class ImmutableTaskRevisionError(RuntimeError):
    pass


def _listen_sqlite_allocation_ddl(statement: str) -> None:
    event.listen(
        AllocationCollectionReceipt.__table__,
        "after_create",
        DDL(statement).execute_if(dialect="sqlite"),
    )


_listen_sqlite_allocation_ddl(
    """
    CREATE TRIGGER trg_argilla_connection_bindings_remote_freeze
    BEFORE UPDATE ON argilla_connection_bindings
    WHEN NEW.id IS NOT OLD.id
      OR NEW.workspace_id IS NOT OLD.workspace_id
      OR NEW.connection_config_id IS NOT OLD.connection_config_id
      OR NEW.created_at IS NOT OLD.created_at
      OR (
        OLD.argilla_workspace_id IS NOT NULL
        AND (
          NEW.argilla_workspace_id IS NOT OLD.argilla_workspace_id
          OR NEW.argilla_workspace_name_snapshot IS NOT OLD.argilla_workspace_name_snapshot
        )
      )
    BEGIN
        SELECT RAISE(ABORT, 'Argilla workspace binding is immutable');
    END
    """
)
_listen_sqlite_allocation_ddl(
    """
    CREATE TRIGGER trg_argilla_connection_bindings_bound_delete
    BEFORE DELETE ON argilla_connection_bindings
    WHEN OLD.argilla_workspace_id IS NOT NULL
    BEGIN
        SELECT RAISE(ABORT, 'bound Argilla connection cannot be deleted');
    END
    """
)
_listen_sqlite_allocation_ddl(
    """
    CREATE TRIGGER trg_argilla_annotator_mappings_remote_freeze
    BEFORE UPDATE ON argilla_annotator_mappings
    WHEN NEW.id IS NOT OLD.id
      OR NEW.workspace_id IS NOT OLD.workspace_id
      OR NEW.connection_binding_id IS NOT OLD.connection_binding_id
      OR NEW.principal_id IS NOT OLD.principal_id
      OR NEW.created_at IS NOT OLD.created_at
      OR (OLD.argilla_user_id IS NOT NULL AND NEW.argilla_user_id IS NOT OLD.argilla_user_id)
      OR (
        OLD.personal_argilla_workspace_id IS NOT NULL
        AND NEW.personal_argilla_workspace_id IS NOT OLD.personal_argilla_workspace_id
      )
    BEGIN
        SELECT RAISE(ABORT, 'Argilla annotator UUID binding is immutable');
    END
    """
)
_listen_sqlite_allocation_ddl(
    """
    CREATE TRIGGER trg_argilla_annotator_mappings_bound_delete
    BEFORE DELETE ON argilla_annotator_mappings
    WHEN OLD.argilla_user_id IS NOT NULL OR OLD.personal_argilla_workspace_id IS NOT NULL
    BEGIN
        SELECT RAISE(ABORT, 'bound Argilla annotator mapping cannot be deleted');
    END
    """
)

for _operation in ("INSERT", "UPDATE"):
    _member_update_guard = ""
    if _operation == "UPDATE":
        _member_update_guard = """
            SELECT RAISE(ABORT, 'cohort member identity is immutable')
            WHERE NEW.id IS NOT OLD.id
               OR NEW.workspace_id IS NOT OLD.workspace_id
               OR NEW.cohort_revision_id IS NOT OLD.cohort_revision_id
               OR NEW.annotator_mapping_id IS NOT OLD.annotator_mapping_id
               OR NEW.created_at IS NOT OLD.created_at;
        """
    _listen_sqlite_allocation_ddl(
        f"""
        CREATE TRIGGER trg_annotator_cohort_members_guard_{_operation.lower()}
        BEFORE {_operation} ON annotator_cohort_members
        BEGIN
            {_member_update_guard}
            SELECT RAISE(ABORT, 'sealed cohort members are immutable')
            WHERE EXISTS (
                SELECT 1
                FROM annotator_cohort_revisions revision
                WHERE revision.id = NEW.cohort_revision_id
                  AND revision.workspace_id = NEW.workspace_id
                  AND revision.sealed_at IS NOT NULL
            );
            SELECT RAISE(ABORT, 'cohort member must use the revision binding')
            WHERE EXISTS (
                SELECT 1
                FROM annotator_cohort_revisions revision
                JOIN argilla_annotator_mappings mapping
                  ON mapping.id = NEW.annotator_mapping_id
                 AND mapping.workspace_id = NEW.workspace_id
                WHERE revision.id = NEW.cohort_revision_id
                  AND revision.workspace_id = NEW.workspace_id
                  AND revision.connection_binding_id IS NOT NULL
                  AND mapping.connection_binding_id IS NOT revision.connection_binding_id
            );
        END
        """
    )

_listen_sqlite_allocation_ddl(
    """
    CREATE TRIGGER trg_annotator_cohort_members_guard_delete
    BEFORE DELETE ON annotator_cohort_members
    BEGIN
        SELECT RAISE(ABORT, 'sealed cohort members are immutable')
        WHERE EXISTS (
            SELECT 1
            FROM annotator_cohort_revisions revision
            WHERE revision.id = OLD.cohort_revision_id
              AND revision.workspace_id = OLD.workspace_id
              AND revision.sealed_at IS NOT NULL
        );
    END
    """
)

_listen_sqlite_allocation_ddl(
    """
    CREATE TRIGGER trg_annotator_cohort_revisions_unsealed_insert
    BEFORE INSERT ON annotator_cohort_revisions
    WHEN NEW.sealed_at IS NOT NULL OR NEW.connection_binding_id IS NOT NULL
    BEGIN
        SELECT RAISE(ABORT, 'cohort revision must be inserted unsealed');
    END
    """
)
_listen_sqlite_allocation_ddl(
    """
    CREATE TRIGGER trg_annotator_cohort_revisions_seal_update
    BEFORE UPDATE ON annotator_cohort_revisions
    BEGIN
        SELECT RAISE(ABORT, 'sealed cohort revision is immutable')
        WHERE OLD.sealed_at IS NOT NULL;
        SELECT RAISE(ABORT, 'only cohort sealing may update a revision')
        WHERE NEW.id IS NOT OLD.id
           OR NEW.workspace_id IS NOT OLD.workspace_id
           OR NEW.cohort_id IS NOT OLD.cohort_id
           OR NEW.revision_number IS NOT OLD.revision_number
           OR NEW.fingerprint IS NOT OLD.fingerprint
           OR NEW.member_count IS NOT OLD.member_count
           OR NEW.actor_principal_id IS NOT OLD.actor_principal_id
           OR NEW.caller_principal_id IS NOT OLD.caller_principal_id
           OR NEW.channel IS NOT OLD.channel
           OR NEW.idempotency_record_id IS NOT OLD.idempotency_record_id
           OR NEW.created_at IS NOT OLD.created_at
           OR OLD.sealed_at IS NOT NULL
           OR NEW.sealed_at IS NULL
           OR NEW.connection_binding_id IS NULL;
        SELECT RAISE(ABORT, 'cohort member count does not match sealed revision')
        WHERE (
            SELECT count(*) FROM annotator_cohort_members member
            WHERE member.cohort_revision_id = OLD.id
        ) <> OLD.member_count;
        SELECT RAISE(ABORT, 'cohort members must be bound users on one connection')
        WHERE EXISTS (
            SELECT 1
            FROM annotator_cohort_members member
            JOIN argilla_annotator_mappings mapping
              ON mapping.id = member.annotator_mapping_id
             AND mapping.workspace_id = member.workspace_id
            WHERE member.cohort_revision_id = OLD.id
              AND (
                mapping.connection_binding_id IS NOT NEW.connection_binding_id
                OR mapping.argilla_user_id IS NULL
              )
        );
    END
    """
)
_listen_sqlite_allocation_ddl(
    """
    CREATE TRIGGER trg_annotator_cohort_revisions_sealed_delete
    BEFORE DELETE ON annotator_cohort_revisions
    WHEN OLD.sealed_at IS NOT NULL
    BEGIN
        SELECT RAISE(ABORT, 'sealed cohort revision is immutable');
    END
    """
)

for _operation in ("INSERT", "UPDATE"):
    _listen_sqlite_allocation_ddl(
        f"""
        CREATE TRIGGER trg_allocation_plans_contract_{_operation.lower()}
        BEFORE {_operation} ON allocation_plans
        BEGIN
            SELECT RAISE(ABORT, 'allocation plan requires a sealed cohort on the same binding')
            WHERE NOT EXISTS (
                SELECT 1
                FROM annotator_cohort_revisions revision
                WHERE revision.id = NEW.cohort_revision_id
                  AND revision.workspace_id = NEW.workspace_id
                  AND revision.sealed_at IS NOT NULL
                  AND revision.connection_binding_id = NEW.connection_binding_id
            );
            SELECT RAISE(ABORT, 'allocation plan task revision hash mismatch')
            WHERE NOT EXISTS (
                SELECT 1
                FROM task_revisions revision
                WHERE revision.id = NEW.task_revision_id
                  AND revision.workspace_id = NEW.workspace_id
                  AND revision.task_id = NEW.task_id
                  AND revision.content_hash = NEW.task_revision_hash
            );
        END
        """
    )

_listen_sqlite_allocation_ddl(
    """
    CREATE TRIGGER trg_allocation_plans_create_state
    AFTER INSERT ON allocation_plans
    BEGIN
        INSERT INTO allocation_plan_states (plan_id, workspace_id, lifecycle_state)
        VALUES (NEW.id, NEW.workspace_id, 'draft');
    END
    """
)
_listen_sqlite_allocation_ddl(
    """
    CREATE TRIGGER trg_allocation_plan_states_draft_insert
    BEFORE INSERT ON allocation_plan_states
    WHEN NEW.lifecycle_state <> 'draft'
      OR NEW.confirmed_at IS NOT NULL
      OR NEW.confirmed_by_principal_id IS NOT NULL
      OR NEW.confirmed_via_caller_principal_id IS NOT NULL
      OR NEW.confirmation_idempotency_record_id IS NOT NULL
    BEGIN
        SELECT RAISE(ABORT, 'allocation plan state must be inserted as draft');
    END
    """
)
_listen_sqlite_allocation_ddl(
    """
    CREATE TRIGGER trg_allocation_plan_states_validate_confirm
    BEFORE UPDATE ON allocation_plan_states
    WHEN OLD.lifecycle_state = 'draft' AND NEW.lifecycle_state = 'confirmed'
    BEGIN
        SELECT RAISE(ABORT, 'allocation workspace group graph is invalid')
        WHERE EXISTS (
            SELECT 1
            FROM allocation_workspace_groups workspace_group
            JOIN allocation_plans plan
              ON plan.id = workspace_group.plan_id
             AND plan.workspace_id = workspace_group.workspace_id
            WHERE workspace_group.plan_id = OLD.plan_id
              AND workspace_group.assignee_mapping_id IS NOT NULL
              AND NOT EXISTS (
                SELECT 1
                FROM annotator_cohort_members member
                JOIN argilla_annotator_mappings mapping
                  ON mapping.id = member.annotator_mapping_id
                 AND mapping.workspace_id = member.workspace_id
                WHERE member.cohort_revision_id = plan.cohort_revision_id
                  AND member.annotator_mapping_id = workspace_group.assignee_mapping_id
                  AND mapping.connection_binding_id = plan.connection_binding_id
              )
        );
        SELECT RAISE(ABORT, 'allocation dataset group graph is invalid')
        WHERE EXISTS (
            SELECT 1 FROM allocation_dataset_groups candidate
            WHERE candidate.plan_id = OLD.plan_id
              AND NOT EXISTS (
                SELECT 1
                FROM allocation_workspace_groups workspace_group
                JOIN allocation_plans plan
                  ON plan.id = workspace_group.plan_id
                 AND plan.workspace_id = workspace_group.workspace_id
                JOIN annotator_cohort_revisions revision
                  ON revision.id = plan.cohort_revision_id
                 AND revision.workspace_id = plan.workspace_id
                WHERE workspace_group.id = candidate.workspace_group_id
                  AND workspace_group.plan_id = candidate.plan_id
                  AND workspace_group.workspace_id = candidate.workspace_id
                  AND workspace_group.phase = candidate.phase
                  AND (
                    (
                      workspace_group.mode = 'personal'
                      AND candidate.assignee_mapping_id IS workspace_group.assignee_mapping_id
                      AND candidate.pool_id IS NULL
                      AND candidate.min_submitted = 1
                    ) OR (
                      workspace_group.mode = 'shared'
                      AND candidate.assignee_mapping_id IS NULL
                      AND candidate.pool_id = revision.cohort_id
                      AND candidate.min_submitted <= revision.member_count
                    ) OR (
                      workspace_group.mode = 'calibration'
                      AND candidate.assignee_mapping_id IS NULL
                      AND candidate.pool_id = revision.cohort_id
                      AND candidate.min_submitted = revision.member_count
                    )
                  )
              )
        );
        SELECT RAISE(ABORT, 'allocation assignment graph is invalid')
        WHERE EXISTS (
            SELECT 1 FROM allocation_assignments candidate
            WHERE candidate.plan_id = OLD.plan_id
              AND NOT EXISTS (
                SELECT 1
                FROM allocation_assignment_items item
                JOIN allocation_dataset_groups dataset_group
                  ON dataset_group.id = item.dataset_group_id
                 AND dataset_group.plan_id = item.plan_id
                 AND dataset_group.workspace_id = item.workspace_id
                JOIN allocation_workspace_groups workspace_group
                  ON workspace_group.id = dataset_group.workspace_group_id
                 AND workspace_group.plan_id = dataset_group.plan_id
                 AND workspace_group.workspace_id = dataset_group.workspace_id
                JOIN allocation_plans plan
                  ON plan.id = item.plan_id
                 AND plan.workspace_id = item.workspace_id
                JOIN annotator_cohort_revisions revision
                  ON revision.id = plan.cohort_revision_id
                 AND revision.workspace_id = plan.workspace_id
                WHERE item.id = candidate.item_id
                  AND item.plan_id = candidate.plan_id
                  AND item.workspace_id = candidate.workspace_id
                  AND dataset_group.phase = candidate.phase
                  AND (
                    (
                      workspace_group.mode = 'personal'
                      AND dataset_group.assignee_mapping_id IS candidate.assignee_mapping_id
                      AND dataset_group.pool_id IS NULL
                      AND candidate.assignee_mapping_id IS NOT NULL
                      AND candidate.pool_id IS NULL
                      AND candidate.required_submissions = 1
                      AND candidate.role IN ('primary', 'overlap')
                      AND EXISTS (
                        SELECT 1
                        FROM annotator_cohort_members member
                        JOIN argilla_annotator_mappings mapping
                          ON mapping.id = member.annotator_mapping_id
                         AND mapping.workspace_id = member.workspace_id
                        WHERE member.cohort_revision_id = plan.cohort_revision_id
                          AND member.annotator_mapping_id = candidate.assignee_mapping_id
                          AND mapping.connection_binding_id = plan.connection_binding_id
                      )
                    ) OR (
                      workspace_group.mode = 'shared'
                      AND dataset_group.assignee_mapping_id IS NULL
                      AND dataset_group.pool_id IS candidate.pool_id
                      AND candidate.assignee_mapping_id IS NULL
                      AND candidate.pool_id = revision.cohort_id
                      AND candidate.required_submissions = dataset_group.min_submitted
                      AND (
                        (dataset_group.min_submitted = 1 AND candidate.role = 'shared')
                        OR (dataset_group.min_submitted > 1 AND candidate.role = 'overlap')
                      )
                    ) OR (
                      workspace_group.mode = 'calibration'
                      AND dataset_group.assignee_mapping_id IS NULL
                      AND dataset_group.pool_id = revision.cohort_id
                      AND candidate.assignee_mapping_id IS NOT NULL
                      AND candidate.pool_id IS NULL
                      AND candidate.required_submissions = 1
                      AND candidate.role = 'calibration'
                      AND EXISTS (
                        SELECT 1
                        FROM annotator_cohort_members member
                        JOIN argilla_annotator_mappings mapping
                          ON mapping.id = member.annotator_mapping_id
                         AND mapping.workspace_id = member.workspace_id
                        WHERE member.cohort_revision_id = plan.cohort_revision_id
                          AND member.annotator_mapping_id = candidate.assignee_mapping_id
                          AND mapping.connection_binding_id = plan.connection_binding_id
                      )
                    )
                  )
              )
        );
        SELECT RAISE(ABORT, 'allocation dataset state graph is incomplete')
        WHERE EXISTS (
            SELECT 1 FROM allocation_dataset_groups dataset_group
            WHERE dataset_group.plan_id = OLD.plan_id
              AND NOT EXISTS (
                SELECT 1 FROM allocation_dataset_group_states state
                WHERE state.dataset_group_id = dataset_group.id
                  AND state.plan_id = dataset_group.plan_id
                  AND state.workspace_id = dataset_group.workspace_id
              )
        );
        SELECT RAISE(ABORT, 'allocation dataset remote workspace graph is invalid')
        WHERE EXISTS (
            SELECT 1
            FROM allocation_dataset_group_states state
            JOIN allocation_dataset_groups dataset_group
              ON dataset_group.id = state.dataset_group_id
             AND dataset_group.plan_id = state.plan_id
             AND dataset_group.workspace_id = state.workspace_id
            JOIN allocation_workspace_groups workspace_group
              ON workspace_group.id = dataset_group.workspace_group_id
             AND workspace_group.plan_id = dataset_group.plan_id
             AND workspace_group.workspace_id = dataset_group.workspace_id
            JOIN allocation_plans plan
              ON plan.id = dataset_group.plan_id
             AND plan.workspace_id = dataset_group.workspace_id
            JOIN argilla_connection_bindings binding
              ON binding.id = plan.connection_binding_id
             AND binding.workspace_id = plan.workspace_id
            LEFT JOIN argilla_annotator_mappings mapping
              ON mapping.id = workspace_group.assignee_mapping_id
             AND mapping.workspace_id = workspace_group.workspace_id
             AND mapping.connection_binding_id = plan.connection_binding_id
            WHERE state.plan_id = OLD.plan_id
              AND state.argilla_workspace_id IS NOT NULL
              AND state.argilla_workspace_id IS NOT CASE
                WHEN workspace_group.mode = 'personal'
                  THEN mapping.personal_argilla_workspace_id
                ELSE binding.argilla_workspace_id
              END
        );
        SELECT RAISE(ABORT, 'allocation record binding graph is incomplete')
        WHERE EXISTS (
            SELECT 1 FROM allocation_assignment_items item
            WHERE item.plan_id = OLD.plan_id
              AND NOT EXISTS (
                SELECT 1 FROM allocation_record_bindings binding
                WHERE binding.item_id = item.id
                  AND binding.plan_id = item.plan_id
                  AND binding.workspace_id = item.workspace_id
              )
        );
        SELECT RAISE(ABORT, 'allocation remote record graph is invalid')
        WHERE EXISTS (
            SELECT 1
            FROM allocation_record_bindings binding
            JOIN allocation_assignment_items item
              ON item.id = binding.item_id
             AND item.plan_id = binding.plan_id
             AND item.workspace_id = binding.workspace_id
            JOIN allocation_dataset_group_states state
              ON state.dataset_group_id = item.dataset_group_id
             AND state.plan_id = item.plan_id
             AND state.workspace_id = item.workspace_id
            WHERE binding.plan_id = OLD.plan_id
              AND binding.argilla_record_id IS NOT NULL
              AND binding.argilla_dataset_id IS NOT state.argilla_dataset_id
        );
    END
    """
)
_listen_sqlite_allocation_ddl(
    """
    CREATE TRIGGER trg_allocation_plan_states_confirm_once
    BEFORE UPDATE ON allocation_plan_states
    WHEN OLD.lifecycle_state <> 'draft'
      OR NEW.lifecycle_state <> 'confirmed'
      OR NEW.plan_id IS NOT OLD.plan_id
      OR NEW.workspace_id IS NOT OLD.workspace_id
      OR NEW.created_at IS NOT OLD.created_at
      OR NEW.confirmed_at IS NULL
      OR NEW.confirmed_by_principal_id IS NULL
      OR NEW.confirmed_via_caller_principal_id IS NULL
      OR NEW.confirmation_idempotency_record_id IS NULL
    BEGIN
        SELECT RAISE(ABORT, 'allocation plan may only transition from draft to confirmed');
    END
    """
)
_listen_sqlite_allocation_ddl(
    """
    CREATE TRIGGER trg_allocation_plan_states_no_delete
    BEFORE DELETE ON allocation_plan_states
    WHEN EXISTS (
        SELECT 1 FROM allocation_plans plan
        WHERE plan.id = OLD.plan_id AND plan.workspace_id = OLD.workspace_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'allocation plan state cannot be deleted directly');
    END
    """
)

_child_parent_exists = {
    "allocation_workspace_groups": (
        "SELECT 1 FROM allocation_plans parent "
        "WHERE parent.id = OLD.plan_id AND parent.workspace_id = OLD.workspace_id"
    ),
    "allocation_dataset_groups": (
        "SELECT 1 FROM allocation_workspace_groups parent "
        "WHERE parent.id = OLD.workspace_group_id "
        "AND parent.plan_id = OLD.plan_id AND parent.workspace_id = OLD.workspace_id"
    ),
    "allocation_assignment_items": (
        "SELECT 1 FROM allocation_dataset_groups parent "
        "WHERE parent.id = OLD.dataset_group_id "
        "AND parent.plan_id = OLD.plan_id AND parent.workspace_id = OLD.workspace_id"
    ),
    "allocation_assignments": (
        "SELECT 1 FROM allocation_assignment_items parent "
        "WHERE parent.id = OLD.item_id "
        "AND parent.plan_id = OLD.plan_id AND parent.workspace_id = OLD.workspace_id"
    ),
}
for _table_name, _parent_exists in _child_parent_exists.items():
    _listen_sqlite_allocation_ddl(
        f"""
        CREATE TRIGGER trg_{_table_name}_confirmed_insert
        BEFORE INSERT ON {_table_name}
        WHEN EXISTS (
            SELECT 1 FROM allocation_plan_states state
            WHERE state.plan_id = NEW.plan_id
              AND state.lifecycle_state = 'confirmed'
        )
        BEGIN
            SELECT RAISE(ABORT, '{_table_name} is immutable after confirmation');
        END
        """
    )
    _listen_sqlite_allocation_ddl(
        f"""
        CREATE TRIGGER trg_{_table_name}_confirmed_update
        BEFORE UPDATE ON {_table_name}
        BEGIN
            SELECT RAISE(ABORT, '{_table_name} is immutable after confirmation')
            WHERE EXISTS (
                SELECT 1 FROM allocation_plan_states state
                WHERE state.plan_id IN (OLD.plan_id, NEW.plan_id)
                  AND state.lifecycle_state = 'confirmed'
            );
            SELECT RAISE(ABORT, '{_table_name} is immutable after insert');
        END
        """
    )
    _listen_sqlite_allocation_ddl(
        f"""
        CREATE TRIGGER trg_{_table_name}_confirmed_delete
        BEFORE DELETE ON {_table_name}
        BEGIN
            SELECT RAISE(ABORT, '{_table_name} is immutable after confirmation')
            WHERE EXISTS (
                SELECT 1 FROM allocation_plan_states state
                WHERE state.plan_id = OLD.plan_id
                  AND state.lifecycle_state = 'confirmed'
            );
            SELECT RAISE(ABORT, '{_table_name} cannot be deleted directly')
            WHERE EXISTS ({_parent_exists});
        END
        """
    )

_listen_sqlite_allocation_ddl(
    """
    CREATE TRIGGER trg_allocation_plans_confirmed_update
    BEFORE UPDATE ON allocation_plans
    BEGIN
        SELECT RAISE(ABORT, 'allocation_plans is immutable after confirmation')
        WHERE EXISTS (
            SELECT 1 FROM allocation_plan_states state
            WHERE state.plan_id IN (OLD.id, NEW.id)
              AND state.lifecycle_state = 'confirmed'
        );
        SELECT RAISE(ABORT, 'allocation plan is immutable after insert');
    END
    """
)
_listen_sqlite_allocation_ddl(
    """
    CREATE TRIGGER trg_allocation_plans_confirmed_delete
    BEFORE DELETE ON allocation_plans
    WHEN EXISTS (
        SELECT 1 FROM allocation_plan_states state
        WHERE state.plan_id = OLD.id AND state.lifecycle_state = 'confirmed'
    )
    BEGIN
        SELECT RAISE(ABORT, 'allocation_plans is immutable after confirmation');
    END
    """
)

for _operation in ("INSERT", "UPDATE"):
    _listen_sqlite_allocation_ddl(
        f"""
        CREATE TRIGGER trg_allocation_workspace_groups_contract_{_operation.lower()}
        BEFORE {_operation} ON allocation_workspace_groups
        BEGIN
            SELECT RAISE(ABORT, 'workspace group assignee must be a cohort member on the plan binding')
            WHERE NEW.assignee_mapping_id IS NOT NULL
              AND NOT EXISTS (
                SELECT 1
                FROM allocation_plans plan
                JOIN annotator_cohort_members member
                  ON member.cohort_revision_id = plan.cohort_revision_id
                 AND member.workspace_id = plan.workspace_id
                JOIN argilla_annotator_mappings mapping
                  ON mapping.id = member.annotator_mapping_id
                 AND mapping.workspace_id = member.workspace_id
                WHERE plan.id = NEW.plan_id
                  AND plan.workspace_id = NEW.workspace_id
                  AND member.annotator_mapping_id = NEW.assignee_mapping_id
                  AND mapping.connection_binding_id = plan.connection_binding_id
              );
        END
        """
    )

for _operation in ("INSERT", "UPDATE"):
    _listen_sqlite_allocation_ddl(
        f"""
        CREATE TRIGGER trg_allocation_dataset_groups_contract_{_operation.lower()}
        BEFORE {_operation} ON allocation_dataset_groups
        BEGIN
            SELECT RAISE(ABORT, 'allocation dataset group target is invalid')
            WHERE NOT EXISTS (
                SELECT 1
                FROM allocation_workspace_groups workspace_group
                JOIN allocation_plans plan
                  ON plan.id = workspace_group.plan_id
                 AND plan.workspace_id = workspace_group.workspace_id
                JOIN annotator_cohort_revisions revision
                  ON revision.id = plan.cohort_revision_id
                 AND revision.workspace_id = plan.workspace_id
                WHERE workspace_group.id = NEW.workspace_group_id
                  AND workspace_group.plan_id = NEW.plan_id
                  AND workspace_group.workspace_id = NEW.workspace_id
                  AND workspace_group.phase = NEW.phase
                  AND (
                    (
                      workspace_group.mode = 'personal'
                      AND NEW.assignee_mapping_id IS workspace_group.assignee_mapping_id
                      AND NEW.pool_id IS NULL
                      AND NEW.min_submitted = 1
                    ) OR (
                      workspace_group.mode = 'shared'
                      AND NEW.assignee_mapping_id IS NULL
                      AND NEW.pool_id = revision.cohort_id
                      AND NEW.min_submitted <= revision.member_count
                    ) OR (
                      workspace_group.mode = 'calibration'
                      AND NEW.assignee_mapping_id IS NULL
                      AND NEW.pool_id = revision.cohort_id
                      AND NEW.min_submitted = revision.member_count
                    )
                  )
            );
        END
        """
    )

_listen_sqlite_allocation_ddl(
    """
    CREATE TRIGGER trg_allocation_dataset_groups_create_state
    AFTER INSERT ON allocation_dataset_groups
    BEGIN
        INSERT INTO allocation_dataset_group_states (
            dataset_group_id, workspace_id, plan_id, materialization_state, attempt_count
        ) VALUES (NEW.id, NEW.workspace_id, NEW.plan_id, 'pending', 0);
    END
    """
)

for _operation in ("INSERT", "UPDATE"):
    _listen_sqlite_allocation_ddl(
        f"""
        CREATE TRIGGER trg_allocation_dataset_group_states_contract_{_operation.lower()}
        BEFORE {_operation} ON allocation_dataset_group_states
        BEGIN
            SELECT RAISE(ABORT, 'ready dataset group requires a remote binding')
            WHERE NEW.materialization_state = 'ready' AND NEW.argilla_dataset_id IS NULL;
            SELECT RAISE(ABORT, 'dataset group remote workspace does not match the plan binding')
            WHERE NEW.argilla_workspace_id IS NOT NULL
              AND NOT EXISTS (
                SELECT 1
                FROM allocation_dataset_groups dataset_group
                JOIN allocation_workspace_groups workspace_group
                  ON workspace_group.id = dataset_group.workspace_group_id
                 AND workspace_group.plan_id = dataset_group.plan_id
                 AND workspace_group.workspace_id = dataset_group.workspace_id
                JOIN allocation_plans plan
                  ON plan.id = dataset_group.plan_id
                 AND plan.workspace_id = dataset_group.workspace_id
                JOIN argilla_connection_bindings binding
                  ON binding.id = plan.connection_binding_id
                 AND binding.workspace_id = plan.workspace_id
                LEFT JOIN argilla_annotator_mappings mapping
                  ON mapping.id = workspace_group.assignee_mapping_id
                 AND mapping.workspace_id = workspace_group.workspace_id
                 AND mapping.connection_binding_id = plan.connection_binding_id
                WHERE dataset_group.id = NEW.dataset_group_id
                  AND dataset_group.plan_id = NEW.plan_id
                  AND dataset_group.workspace_id = NEW.workspace_id
                  AND NEW.argilla_workspace_id IS CASE
                    WHEN workspace_group.mode = 'personal'
                      THEN mapping.personal_argilla_workspace_id
                    ELSE binding.argilla_workspace_id
                  END
              );
        END
        """
    )

_listen_sqlite_allocation_ddl(
    """
    CREATE TRIGGER trg_allocation_dataset_group_states_remote_freeze
    BEFORE UPDATE ON allocation_dataset_group_states
    WHEN NEW.dataset_group_id IS NOT OLD.dataset_group_id
      OR NEW.workspace_id IS NOT OLD.workspace_id
      OR NEW.plan_id IS NOT OLD.plan_id
      OR (
        OLD.argilla_dataset_id IS NOT NULL
        AND (
          NEW.argilla_workspace_id IS NOT OLD.argilla_workspace_id
          OR NEW.argilla_dataset_id IS NOT OLD.argilla_dataset_id
        )
      )
    BEGIN
        SELECT RAISE(ABORT, 'dataset group remote binding is immutable');
    END
    """
)
_listen_sqlite_allocation_ddl(
    """
    CREATE TRIGGER trg_allocation_dataset_group_states_bound_delete
    BEFORE DELETE ON allocation_dataset_group_states
    WHEN EXISTS (
        SELECT 1 FROM allocation_dataset_groups parent
        WHERE parent.id = OLD.dataset_group_id
          AND parent.plan_id = OLD.plan_id
          AND parent.workspace_id = OLD.workspace_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'dataset group state cannot be deleted directly');
    END
    """
)

_listen_sqlite_allocation_ddl(
    """
    CREATE TRIGGER trg_allocation_assignment_items_create_binding
    AFTER INSERT ON allocation_assignment_items
    BEGIN
        INSERT INTO allocation_record_bindings (item_id, workspace_id, plan_id)
        VALUES (NEW.id, NEW.workspace_id, NEW.plan_id);
    END
    """
)
_listen_sqlite_allocation_ddl(
    """
    CREATE TRIGGER trg_allocation_record_bindings_insert
    BEFORE INSERT ON allocation_record_bindings
    BEGIN
        SELECT RAISE(ABORT, 'record binding must be inserted without remote UUIDs')
        WHERE NEW.argilla_dataset_id IS NOT NULL
           OR NEW.argilla_record_id IS NOT NULL
           OR NEW.bound_at IS NOT NULL;
        SELECT RAISE(ABORT, 'record binding must match an existing assignment item')
        WHERE NOT EXISTS (
            SELECT 1 FROM allocation_assignment_items item
            WHERE item.id = NEW.item_id
              AND item.workspace_id = NEW.workspace_id
              AND item.plan_id = NEW.plan_id
        );
    END
    """
)
_listen_sqlite_allocation_ddl(
    """
    CREATE TRIGGER trg_allocation_record_bindings_contract
    BEFORE UPDATE ON allocation_record_bindings
    BEGIN
        SELECT RAISE(ABORT, 'record binding identity is immutable')
        WHERE NEW.item_id IS NOT OLD.item_id
           OR NEW.workspace_id IS NOT OLD.workspace_id
           OR NEW.plan_id IS NOT OLD.plan_id;
        SELECT RAISE(ABORT, 'record remote binding is immutable')
        WHERE OLD.argilla_record_id IS NOT NULL
          AND (
            NEW.argilla_dataset_id IS NOT OLD.argilla_dataset_id
            OR NEW.argilla_record_id IS NOT OLD.argilla_record_id
            OR NEW.bound_at IS NOT OLD.bound_at
          );
        SELECT RAISE(ABORT, 'record must bind to its dataset group remote dataset')
        WHERE NEW.argilla_record_id IS NOT NULL
          AND NOT EXISTS (
            SELECT 1
                FROM allocation_assignment_items item
                JOIN allocation_dataset_group_states state
              ON state.dataset_group_id = item.dataset_group_id
             AND state.plan_id = item.plan_id
             AND state.workspace_id = item.workspace_id
            WHERE item.id = NEW.item_id
              AND item.plan_id = NEW.plan_id
              AND item.workspace_id = NEW.workspace_id
              AND state.argilla_dataset_id = NEW.argilla_dataset_id
          );
    END
    """
)
_listen_sqlite_allocation_ddl(
    """
    CREATE TRIGGER trg_allocation_record_bindings_bound_delete
    BEFORE DELETE ON allocation_record_bindings
    WHEN EXISTS (
        SELECT 1 FROM allocation_assignment_items parent
        WHERE parent.id = OLD.item_id
          AND parent.plan_id = OLD.plan_id
          AND parent.workspace_id = OLD.workspace_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'record binding cannot be deleted directly');
    END
    """
)

for _operation in ("INSERT", "UPDATE"):
    _listen_sqlite_allocation_ddl(
        f"""
        CREATE TRIGGER trg_allocation_assignments_contract_{_operation.lower()}
        BEFORE {_operation} ON allocation_assignments
        BEGIN
            SELECT RAISE(ABORT, 'allocation assignment phase or target is invalid')
            WHERE NOT EXISTS (
                SELECT 1
                FROM allocation_assignment_items item
                JOIN allocation_dataset_groups dataset_group
                  ON dataset_group.id = item.dataset_group_id
                 AND dataset_group.plan_id = item.plan_id
                 AND dataset_group.workspace_id = item.workspace_id
                JOIN allocation_workspace_groups workspace_group
                  ON workspace_group.id = dataset_group.workspace_group_id
                 AND workspace_group.plan_id = dataset_group.plan_id
                 AND workspace_group.workspace_id = dataset_group.workspace_id
                JOIN allocation_plans plan
                  ON plan.id = item.plan_id
                 AND plan.workspace_id = item.workspace_id
                JOIN annotator_cohort_revisions revision
                  ON revision.id = plan.cohort_revision_id
                 AND revision.workspace_id = plan.workspace_id
                WHERE item.id = NEW.item_id
                  AND item.plan_id = NEW.plan_id
                  AND item.workspace_id = NEW.workspace_id
                  AND dataset_group.phase = NEW.phase
                  AND (
                    (
                      workspace_group.mode = 'personal'
                      AND dataset_group.assignee_mapping_id IS NEW.assignee_mapping_id
                      AND dataset_group.pool_id IS NULL
                      AND NEW.assignee_mapping_id IS NOT NULL
                      AND NEW.pool_id IS NULL
                      AND NEW.required_submissions = 1
                      AND NEW.role IN ('primary', 'overlap')
                      AND EXISTS (
                        SELECT 1
                        FROM annotator_cohort_members member
                        JOIN argilla_annotator_mappings mapping
                          ON mapping.id = member.annotator_mapping_id
                         AND mapping.workspace_id = member.workspace_id
                        WHERE member.cohort_revision_id = plan.cohort_revision_id
                          AND member.annotator_mapping_id = NEW.assignee_mapping_id
                          AND mapping.connection_binding_id = plan.connection_binding_id
                      )
                    ) OR (
                      workspace_group.mode = 'shared'
                      AND dataset_group.assignee_mapping_id IS NULL
                      AND dataset_group.pool_id IS NEW.pool_id
                      AND NEW.assignee_mapping_id IS NULL
                      AND NEW.pool_id = revision.cohort_id
                      AND NEW.required_submissions = dataset_group.min_submitted
                      AND (
                        (dataset_group.min_submitted = 1 AND NEW.role = 'shared')
                        OR (dataset_group.min_submitted > 1 AND NEW.role = 'overlap')
                      )
                    ) OR (
                      workspace_group.mode = 'calibration'
                      AND dataset_group.assignee_mapping_id IS NULL
                      AND dataset_group.pool_id = revision.cohort_id
                      AND NEW.assignee_mapping_id IS NOT NULL
                      AND NEW.pool_id IS NULL
                      AND NEW.required_submissions = 1
                      AND NEW.role = 'calibration'
                      AND EXISTS (
                        SELECT 1
                        FROM annotator_cohort_members member
                        JOIN argilla_annotator_mappings mapping
                          ON mapping.id = member.annotator_mapping_id
                         AND mapping.workspace_id = member.workspace_id
                        WHERE member.cohort_revision_id = plan.cohort_revision_id
                          AND member.annotator_mapping_id = NEW.assignee_mapping_id
                          AND mapping.connection_binding_id = plan.connection_binding_id
                      )
                    )
                  )
            );
        END
        """
    )

_listen_sqlite_allocation_ddl(
    """
    CREATE TRIGGER trg_allocation_collection_receipts_contract
    BEFORE INSERT ON allocation_collection_receipts
    BEGIN
        SELECT RAISE(ABORT, 'allocation receipt must use the confirmed plan binding')
        WHERE NOT EXISTS (
            SELECT 1
            FROM allocation_plans plan
            JOIN allocation_plan_states state ON state.plan_id = plan.id
            WHERE plan.id = NEW.plan_id
              AND plan.workspace_id = NEW.workspace_id
              AND plan.connection_binding_id = NEW.connection_binding_id
              AND state.lifecycle_state = 'confirmed'
        );
        SELECT RAISE(ABORT, 'allocation receipt remote record binding is invalid')
        WHERE NOT EXISTS (
            SELECT 1
            FROM allocation_record_bindings binding
            WHERE binding.item_id = NEW.item_id
              AND binding.plan_id = NEW.plan_id
              AND binding.workspace_id = NEW.workspace_id
              AND binding.argilla_dataset_id = NEW.argilla_dataset_id
              AND binding.argilla_record_id = NEW.argilla_record_id
        );
        SELECT RAISE(ABORT, 'accepted receipt assignment or respondent is invalid')
        WHERE NEW.disposition = 'accepted'
          AND NOT EXISTS (
            SELECT 1
            FROM allocation_assignments assignment
            JOIN allocation_plans plan ON plan.id = assignment.plan_id
            JOIN annotator_cohort_revisions revision
              ON revision.id = plan.cohort_revision_id
             AND revision.workspace_id = plan.workspace_id
            WHERE assignment.id = NEW.assignment_id
              AND assignment.item_id = NEW.item_id
              AND assignment.plan_id = NEW.plan_id
              AND assignment.workspace_id = NEW.workspace_id
              AND (
                (
                  assignment.assignee_mapping_id IS NOT NULL
                  AND EXISTS (
                    SELECT 1 FROM argilla_annotator_mappings mapping
                    WHERE mapping.id = assignment.assignee_mapping_id
                      AND mapping.workspace_id = NEW.workspace_id
                      AND mapping.connection_binding_id = NEW.connection_binding_id
                      AND mapping.argilla_user_id = NEW.argilla_respondent_id
                  )
                ) OR (
                  assignment.assignee_mapping_id IS NULL
                  AND assignment.pool_id = revision.cohort_id
                  AND EXISTS (
                    SELECT 1
                    FROM annotator_cohort_members member
                    JOIN argilla_annotator_mappings mapping
                      ON mapping.id = member.annotator_mapping_id
                     AND mapping.workspace_id = member.workspace_id
                    WHERE member.cohort_revision_id = plan.cohort_revision_id
                      AND mapping.connection_binding_id = NEW.connection_binding_id
                      AND mapping.argilla_user_id = NEW.argilla_respondent_id
                  )
                )
              )
          );
    END
    """
)
_listen_sqlite_allocation_ddl(
    """
    CREATE TRIGGER trg_allocation_collection_receipts_append_only_update
    BEFORE UPDATE ON allocation_collection_receipts
    BEGIN
        SELECT RAISE(ABORT, 'allocation_collection_receipts is append-only');
    END
    """
)
_listen_sqlite_allocation_ddl(
    """
    CREATE TRIGGER trg_allocation_collection_receipts_append_only_delete
    BEFORE DELETE ON allocation_collection_receipts
    BEGIN
        SELECT RAISE(ABORT, 'allocation_collection_receipts is append-only');
    END
    """
)


event.listen(
    TaskRevision.__table__,
    "after_create",
    DDL(
        """
        CREATE TRIGGER trg_task_revisions_immutable_update
        BEFORE UPDATE ON task_revisions
        BEGIN
            SELECT RAISE(ABORT, 'task_revisions is immutable');
        END
        """
    ).execute_if(dialect="sqlite"),
)
event.listen(
    TaskRevision.__table__,
    "after_create",
    DDL(
        """
        CREATE TRIGGER trg_task_revisions_immutable_delete
        BEFORE DELETE ON task_revisions
        BEGIN
            SELECT RAISE(ABORT, 'task_revisions is immutable');
        END
        """
    ).execute_if(dialect="sqlite"),
)


event.listen(
    RoleBinding.__table__,
    "before_create",
    DDL(
        """
        CREATE OR REPLACE FUNCTION lls_enforce_last_workspace_admin()
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
    ).execute_if(dialect="postgresql"),
)
event.listen(
    RoleBinding.__table__,
    "after_create",
    DDL(
        """
        CREATE TRIGGER trg_role_bindings_last_workspace_admin
        BEFORE UPDATE OR DELETE ON role_bindings
        FOR EACH ROW EXECUTE FUNCTION lls_enforce_last_workspace_admin()
        """
    ).execute_if(dialect="postgresql"),
)
event.listen(
    RoleBinding.__table__,
    "after_create",
    DDL(
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
    ).execute_if(dialect="sqlite"),
)
event.listen(
    RoleBinding.__table__,
    "after_create",
    DDL(
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
    ).execute_if(dialect="sqlite"),
)


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


@event.listens_for(TaskRevision, "before_update")
@event.listens_for(TaskRevision, "before_delete")
def _reject_task_revision_mutation(mapper, connection, target) -> None:
    raise ImmutableTaskRevisionError("task revisions are immutable")
