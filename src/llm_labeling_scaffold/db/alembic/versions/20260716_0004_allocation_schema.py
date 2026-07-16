from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260716_0004"
down_revision: str | None = "20260715_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


ENUMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("argilla_binding_state", ("active", "disabled")),
    ("annotator_mapping_state", ("active", "disabled")),
    ("annotator_verification_state", ("unverified", "verified", "rejected")),
    ("annotator_cohort_state", ("active", "archived")),
    (
        "allocation_strategy",
        ("shared_queue", "fixed_partition", "calibration_then_partition"),
    ),
    ("allocation_manifest_kind", ("sample", "batch")),
    ("allocation_phase", ("calibration", "production")),
    ("allocation_workspace_mode", ("calibration", "personal", "shared")),
    ("allocation_assignment_role", ("calibration", "primary", "overlap", "shared")),
    ("allocation_plan_lifecycle", ("draft", "confirmed")),
    ("allocation_dataset_state", ("pending", "materializing", "ready", "failed")),
    ("collection_disposition", ("accepted", "quarantined")),
)


def _enum(name: str, values: tuple[str, ...]) -> sa.Enum:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        enum_type = postgresql.ENUM(*values, name=name, create_type=False)
        enum_type.create(bind, checkfirst=True)
        return enum_type
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True)


def _existing_enum(name: str, values: tuple[str, ...]) -> sa.Enum:
    if op.get_bind().dialect.name == "postgresql":
        return postgresql.ENUM(*values, name=name, create_type=False)
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True)


def _json_document():
    if op.get_bind().dialect.name == "postgresql":
        return postgresql.JSONB()
    return sa.JSON()


def _create_tables() -> None:
    enum_types = {name: _enum(name, values) for name, values in ENUMS}
    audit_channel = _existing_enum(
        "audit_channel",
        ("panel", "mcp", "api", "cli", "worker", "system"),
    )
    json_document = _json_document()

    op.create_table(
        "argilla_connection_bindings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("connection_config_id", sa.String(length=255), nullable=False),
        sa.Column("server_version", sa.String(length=64), nullable=False),
        sa.Column("argilla_workspace_id", sa.Uuid(), nullable=True),
        sa.Column("argilla_workspace_name_snapshot", sa.String(length=255), nullable=True),
        sa.Column(
            "state",
            enum_types["argilla_binding_state"],
            server_default=sa.text("'active'"),
            nullable=False,
        ),
        sa.Column("idempotency_record_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "length(trim(connection_config_id)) > 0",
            name="config_id_not_blank",
        ),
        sa.CheckConstraint(
            "length(trim(server_version)) > 0",
            name="server_version_not_blank",
        ),
        sa.CheckConstraint(
            "argilla_workspace_name_snapshot IS NULL "
            "OR length(trim(argilla_workspace_name_snapshot)) > 0",
            name="workspace_name_not_blank",
        ),
        sa.CheckConstraint(
            "(argilla_workspace_id IS NULL AND argilla_workspace_name_snapshot IS NULL) OR "
            "(argilla_workspace_id IS NOT NULL AND argilla_workspace_name_snapshot IS NOT NULL)",
            name="remote_workspace_complete",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_argilla_connection_bindings_workspace_id_workspaces",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["idempotency_record_id"],
            ["idempotency_records.id"],
            name="fk_argilla_connection_bindings_idempotency",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_argilla_connection_bindings"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_argilla_connection_bindings_workspace_id"),
        sa.UniqueConstraint(
            "workspace_id",
            "connection_config_id",
            name="uq_argilla_connection_bindings_workspace_config",
        ),
        sa.UniqueConstraint(
            "idempotency_record_id",
            name="uq_argilla_connection_bindings_idempotency",
        ),
    )
    op.create_index(
        "ix_argilla_connection_bindings_workspace_state",
        "argilla_connection_bindings",
        ["workspace_id", "state"],
    )

    op.create_table(
        "argilla_annotator_mappings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("connection_binding_id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=True),
        sa.Column("argilla_user_id", sa.Uuid(), nullable=True),
        sa.Column("username_snapshot", sa.String(length=255), nullable=False),
        sa.Column("personal_argilla_workspace_id", sa.Uuid(), nullable=True),
        sa.Column(
            "state",
            enum_types["annotator_mapping_state"],
            server_default=sa.text("'active'"),
            nullable=False,
        ),
        sa.Column(
            "verification_state",
            enum_types["annotator_verification_state"],
            server_default=sa.text("'unverified'"),
            nullable=False,
        ),
        sa.Column("verified_by_principal_id", sa.Uuid(), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("idempotency_record_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "length(trim(username_snapshot)) > 0",
            name="username_not_blank",
        ),
        sa.CheckConstraint(
            "(verification_state = 'verified' AND verified_by_principal_id IS NOT NULL "
            "AND verified_at IS NOT NULL) OR "
            "(verification_state <> 'verified' AND verified_at IS NULL)",
            name="verification_metadata",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "connection_binding_id"],
            ["argilla_connection_bindings.workspace_id", "argilla_connection_bindings.id"],
            name="fk_argilla_annotator_mappings_workspace_binding",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name="fk_argilla_annotator_mappings_principal_id_principals",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["verified_by_principal_id"],
            ["principals.id"],
            name="fk_argilla_annotator_mappings_verifier",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["idempotency_record_id"],
            ["idempotency_records.id"],
            name="fk_argilla_annotator_mappings_idempotency",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_argilla_annotator_mappings"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_argilla_annotator_mappings_workspace_id"),
        sa.UniqueConstraint(
            "idempotency_record_id",
            name="uq_argilla_annotator_mappings_idempotency",
        ),
    )
    op.create_index(
        "uq_argilla_annotator_mappings_binding_user",
        "argilla_annotator_mappings",
        ["connection_binding_id", "argilla_user_id"],
        unique=True,
        postgresql_where=sa.text("argilla_user_id IS NOT NULL"),
        sqlite_where=sa.text("argilla_user_id IS NOT NULL"),
    )
    op.create_index(
        "uq_argilla_annotator_mappings_binding_principal",
        "argilla_annotator_mappings",
        ["connection_binding_id", "principal_id"],
        unique=True,
        postgresql_where=sa.text("principal_id IS NOT NULL"),
        sqlite_where=sa.text("principal_id IS NOT NULL"),
    )
    op.create_index(
        "ix_argilla_annotator_mappings_workspace_state",
        "argilla_annotator_mappings",
        ["workspace_id", "state"],
    )

    op.create_table(
        "annotator_cohorts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "state",
            enum_types["annotator_cohort_state"],
            server_default=sa.text("'active'"),
            nullable=False,
        ),
        sa.Column("idempotency_record_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("length(trim(name)) > 0", name="name_not_blank"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_annotator_cohorts_workspace_id_workspaces",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["idempotency_record_id"],
            ["idempotency_records.id"],
            name="fk_annotator_cohorts_idempotency_record_id_idempotency_records",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_annotator_cohorts"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_annotator_cohorts_workspace_id"),
        sa.UniqueConstraint("workspace_id", "name", name="uq_annotator_cohorts_workspace_name"),
        sa.UniqueConstraint("idempotency_record_id", name="uq_annotator_cohorts_idempotency"),
    )
    op.create_index(
        "ix_annotator_cohorts_workspace_state",
        "annotator_cohorts",
        ["workspace_id", "state"],
    )

    op.create_table(
        "annotator_cohort_revisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("cohort_id", sa.Uuid(), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("member_count", sa.Integer(), nullable=False),
        sa.Column("connection_binding_id", sa.Uuid(), nullable=True),
        sa.Column("actor_principal_id", sa.Uuid(), nullable=False),
        sa.Column("caller_principal_id", sa.Uuid(), nullable=False),
        sa.Column("channel", audit_channel, nullable=False),
        sa.Column("idempotency_record_id", sa.Uuid(), nullable=False),
        sa.Column("sealed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "revision_number > 0",
            name="revision_number_positive",
        ),
        sa.CheckConstraint(
            "member_count > 0",
            name="member_count_positive",
        ),
        sa.CheckConstraint(
            "length(fingerprint) = 64",
            name="fingerprint_sha256",
        ),
        sa.CheckConstraint(
            "(sealed_at IS NULL AND connection_binding_id IS NULL) OR "
            "(sealed_at IS NOT NULL AND connection_binding_id IS NOT NULL)",
            name="seal_binding_complete",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "cohort_id"],
            ["annotator_cohorts.workspace_id", "annotator_cohorts.id"],
            name="fk_annotator_cohort_revisions_workspace_cohort",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "connection_binding_id"],
            ["argilla_connection_bindings.workspace_id", "argilla_connection_bindings.id"],
            name="fk_annotator_cohort_revisions_workspace_binding",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_principal_id"],
            ["principals.id"],
            name="fk_annotator_cohort_revisions_actor_principal_id_principals",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["caller_principal_id"],
            ["principals.id"],
            name="fk_annotator_cohort_revisions_caller_principal_id_principals",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["idempotency_record_id"],
            ["idempotency_records.id"],
            name="fk_annotator_cohort_revisions_idempotency",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_annotator_cohort_revisions"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_annotator_cohort_revisions_workspace_id"),
        sa.UniqueConstraint("cohort_id", "revision_number", name="uq_annotator_cohort_revisions_number"),
        sa.UniqueConstraint("cohort_id", "fingerprint", name="uq_annotator_cohort_revisions_fingerprint"),
        sa.UniqueConstraint(
            "idempotency_record_id",
            name="uq_annotator_cohort_revisions_idempotency",
        ),
    )

    op.create_table(
        "annotator_cohort_members",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("cohort_revision_id", sa.Uuid(), nullable=False),
        sa.Column("annotator_mapping_id", sa.Uuid(), nullable=False),
        sa.Column("default_capacity", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "default_capacity >= 0",
            name="default_capacity_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "cohort_revision_id"],
            ["annotator_cohort_revisions.workspace_id", "annotator_cohort_revisions.id"],
            name="fk_annotator_cohort_members_workspace_revision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "annotator_mapping_id"],
            ["argilla_annotator_mappings.workspace_id", "argilla_annotator_mappings.id"],
            name="fk_annotator_cohort_members_workspace_mapping",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_annotator_cohort_members"),
        sa.UniqueConstraint(
            "cohort_revision_id",
            "annotator_mapping_id",
            name="uq_annotator_cohort_members_revision_mapping",
        ),
    )
    op.create_index(
        "ix_annotator_cohort_members_mapping",
        "annotator_cohort_members",
        ["workspace_id", "annotator_mapping_id"],
    )

    op.create_table(
        "allocation_plans",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("connection_binding_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("task_revision_id", sa.Uuid(), nullable=False),
        sa.Column("task_revision_hash", sa.String(length=64), nullable=False),
        sa.Column("source_manifest_id", sa.String(length=255), nullable=False),
        sa.Column("source_manifest_kind", enum_types["allocation_manifest_kind"], nullable=False),
        sa.Column("source_manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("cohort_revision_id", sa.Uuid(), nullable=False),
        sa.Column("strategy", enum_types["allocation_strategy"], nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("seed", sa.Numeric(precision=1000, scale=0), nullable=False),
        sa.Column("algorithm_version", sa.String(length=128), nullable=False),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("capacity_snapshot", json_document, nullable=False),
        sa.Column("qc_snapshot", json_document, nullable=False),
        sa.Column("actor_principal_id", sa.Uuid(), nullable=False),
        sa.Column("caller_principal_id", sa.Uuid(), nullable=False),
        sa.Column("channel", audit_channel, nullable=False),
        sa.Column("idempotency_record_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "length(trim(schema_version)) > 0",
            name="schema_version_not_blank",
        ),
        sa.CheckConstraint(
            "length(task_revision_hash) = 64",
            name="task_revision_hash_sha256",
        ),
        sa.CheckConstraint(
            "length(trim(source_manifest_id)) > 0",
            name="source_manifest_id_not_blank",
        ),
        sa.CheckConstraint(
            "length(source_manifest_hash) = 64",
            name="source_manifest_hash_sha256",
        ),
        sa.CheckConstraint(
            "length(trim(algorithm_version)) > 0",
            name="algorithm_version_not_blank",
        ),
        sa.CheckConstraint(
            "length(input_fingerprint) = 64",
            name="input_fingerprint_sha256",
        ),
        sa.CheckConstraint(
            "length(fingerprint) = 64",
            name="fingerprint_sha256",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "connection_binding_id"],
            ["argilla_connection_bindings.workspace_id", "argilla_connection_bindings.id"],
            name="fk_allocation_plans_workspace_binding",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "task_id"],
            ["tasks.workspace_id", "tasks.id"],
            name="fk_allocation_plans_workspace_task",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "task_id", "task_revision_id"],
            ["task_revisions.workspace_id", "task_revisions.task_id", "task_revisions.id"],
            name="fk_allocation_plans_task_revision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "cohort_revision_id"],
            ["annotator_cohort_revisions.workspace_id", "annotator_cohort_revisions.id"],
            name="fk_allocation_plans_cohort_revision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_principal_id"],
            ["principals.id"],
            name="fk_allocation_plans_actor_principal_id_principals",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["caller_principal_id"],
            ["principals.id"],
            name="fk_allocation_plans_caller_principal_id_principals",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["idempotency_record_id"],
            ["idempotency_records.id"],
            name="fk_allocation_plans_idempotency_record_id_idempotency_records",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_allocation_plans"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_allocation_plans_workspace_id"),
        sa.UniqueConstraint("workspace_id", "fingerprint", name="uq_allocation_plans_workspace_fingerprint"),
        sa.UniqueConstraint("idempotency_record_id", name="uq_allocation_plans_idempotency"),
    )
    op.create_index(
        "ix_allocation_plans_task_created",
        "allocation_plans",
        ["workspace_id", "task_id", "created_at"],
    )

    op.create_table(
        "allocation_plan_states",
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column(
            "lifecycle_state",
            enum_types["allocation_plan_lifecycle"],
            server_default=sa.text("'draft'"),
            nullable=False,
        ),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_by_principal_id", sa.Uuid(), nullable=True),
        sa.Column("confirmed_via_caller_principal_id", sa.Uuid(), nullable=True),
        sa.Column("confirmation_idempotency_record_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
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
        sa.ForeignKeyConstraint(
            ["workspace_id", "plan_id"],
            ["allocation_plans.workspace_id", "allocation_plans.id"],
            name="fk_allocation_plan_states_workspace_plan",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_allocation_plan_states_workspace_id_workspaces",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["confirmed_by_principal_id"],
            ["principals.id"],
            name="fk_allocation_plan_states_confirmed_by_principal_id_principals",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["confirmed_via_caller_principal_id"],
            ["principals.id"],
            name="fk_allocation_plan_states_confirmation_caller",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["confirmation_idempotency_record_id"],
            ["idempotency_records.id"],
            name="fk_allocation_plan_states_confirmation_idempotency",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("plan_id", name="pk_allocation_plan_states"),
        sa.UniqueConstraint(
            "confirmation_idempotency_record_id",
            name="uq_allocation_plan_states_confirmation",
        ),
    )
    op.create_index(
        "ix_allocation_plan_states_workspace_lifecycle",
        "allocation_plan_states",
        ["workspace_id", "lifecycle_state"],
    )

    op.create_table(
        "allocation_workspace_groups",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("planner_workspace_group_id", sa.String(length=255), nullable=False),
        sa.Column("phase", enum_types["allocation_phase"], nullable=False),
        sa.Column("mode", enum_types["allocation_workspace_mode"], nullable=False),
        sa.Column("assignee_mapping_id", sa.Uuid(), nullable=True),
        sa.Column("member_mapping_ids", json_document, nullable=False),
        sa.CheckConstraint(
            "length(trim(planner_workspace_group_id)) > 0",
            name="planner_key_not_blank",
        ),
        sa.CheckConstraint(
            "(mode = 'calibration' AND phase = 'calibration' AND assignee_mapping_id IS NULL) OR "
            "(mode = 'personal' AND phase = 'production' AND assignee_mapping_id IS NOT NULL) OR "
            "(mode = 'shared' AND phase = 'production' AND assignee_mapping_id IS NULL)",
            name="mode_phase_target",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "plan_id"],
            ["allocation_plans.workspace_id", "allocation_plans.id"],
            name="fk_allocation_workspace_groups_workspace_plan",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "assignee_mapping_id"],
            ["argilla_annotator_mappings.workspace_id", "argilla_annotator_mappings.id"],
            name="fk_allocation_workspace_groups_workspace_assignee",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_allocation_workspace_groups"),
        sa.UniqueConstraint(
            "workspace_id",
            "plan_id",
            "id",
            name="uq_allocation_workspace_groups_scope_id",
        ),
        sa.UniqueConstraint(
            "plan_id",
            "planner_workspace_group_id",
            name="uq_allocation_workspace_groups_plan_key",
        ),
    )

    op.create_table(
        "allocation_dataset_groups",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_group_id", sa.Uuid(), nullable=False),
        sa.Column("planner_dataset_group_id", sa.String(length=255), nullable=False),
        sa.Column("phase", enum_types["allocation_phase"], nullable=False),
        sa.Column("min_submitted", sa.Integer(), nullable=False),
        sa.Column("assignee_mapping_id", sa.Uuid(), nullable=True),
        sa.Column("pool_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "length(trim(planner_dataset_group_id)) > 0",
            name="planner_key_not_blank",
        ),
        sa.CheckConstraint(
            "min_submitted > 0",
            name="min_submitted_positive",
        ),
        sa.CheckConstraint(
            "(assignee_mapping_id IS NULL) <> (pool_id IS NULL)",
            name="assignee_or_pool",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "plan_id", "workspace_group_id"],
            [
                "allocation_workspace_groups.workspace_id",
                "allocation_workspace_groups.plan_id",
                "allocation_workspace_groups.id",
            ],
            name="fk_allocation_dataset_groups_workspace_group",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "assignee_mapping_id"],
            ["argilla_annotator_mappings.workspace_id", "argilla_annotator_mappings.id"],
            name="fk_allocation_dataset_groups_workspace_assignee",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_allocation_dataset_groups"),
        sa.UniqueConstraint(
            "workspace_id",
            "plan_id",
            "id",
            name="uq_allocation_dataset_groups_scope_id",
        ),
        sa.UniqueConstraint(
            "plan_id",
            "planner_dataset_group_id",
            name="uq_allocation_dataset_groups_plan_key",
        ),
    )
    op.create_index(
        "ix_allocation_dataset_groups_plan_phase",
        "allocation_dataset_groups",
        ["plan_id", "phase"],
    )

    op.create_table(
        "allocation_dataset_group_states",
        sa.Column("dataset_group_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column(
            "materialization_state",
            enum_types["allocation_dataset_state"],
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("argilla_workspace_id", sa.Uuid(), nullable=True),
        sa.Column("argilla_dataset_id", sa.Uuid(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("'0'"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("materialized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="attempt_count_nonnegative",
        ),
        sa.CheckConstraint(
            "(argilla_workspace_id IS NULL AND argilla_dataset_id IS NULL) OR "
            "(argilla_workspace_id IS NOT NULL AND argilla_dataset_id IS NOT NULL)",
            name="remote_binding_complete",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "plan_id", "dataset_group_id"],
            [
                "allocation_dataset_groups.workspace_id",
                "allocation_dataset_groups.plan_id",
                "allocation_dataset_groups.id",
            ],
            name="fk_allocation_dataset_group_states_group",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("dataset_group_id", name="pk_allocation_dataset_group_states"),
        sa.UniqueConstraint(
            "argilla_dataset_id",
            name="uq_allocation_dataset_group_states_argilla_dataset",
        ),
    )
    op.create_index(
        "ix_allocation_dataset_group_states_pending",
        "allocation_dataset_group_states",
        ["materialization_state", "updated_at"],
    )

    op.create_table(
        "allocation_assignment_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("dataset_group_id", sa.Uuid(), nullable=False),
        sa.Column("record_id", sa.String(length=512), nullable=False),
        sa.Column("external_id", sa.String(length=512), nullable=False),
        sa.Column("batch_id", sa.String(length=255), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "length(trim(record_id)) > 0",
            name="record_id_not_blank",
        ),
        sa.CheckConstraint(
            "length(trim(external_id)) > 0",
            name="external_id_not_blank",
        ),
        sa.CheckConstraint(
            "length(trim(batch_id)) > 0",
            name="batch_id_not_blank",
        ),
        sa.CheckConstraint(
            "length(content_hash) = 64",
            name="content_hash_sha256",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "plan_id", "dataset_group_id"],
            [
                "allocation_dataset_groups.workspace_id",
                "allocation_dataset_groups.plan_id",
                "allocation_dataset_groups.id",
            ],
            name="fk_allocation_assignment_items_dataset_group",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_allocation_assignment_items"),
        sa.UniqueConstraint(
            "workspace_id",
            "plan_id",
            "id",
            name="uq_allocation_assignment_items_scope_id",
        ),
        sa.UniqueConstraint(
            "dataset_group_id",
            "record_id",
            name="uq_allocation_assignment_items_group_record",
        ),
        sa.UniqueConstraint(
            "dataset_group_id",
            "external_id",
            name="uq_allocation_assignment_items_group_external",
        ),
    )
    op.create_index(
        "ix_allocation_assignment_items_plan_group",
        "allocation_assignment_items",
        ["plan_id", "dataset_group_id"],
    )

    op.create_table(
        "allocation_record_bindings",
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("argilla_dataset_id", sa.Uuid(), nullable=True),
        sa.Column("argilla_record_id", sa.Uuid(), nullable=True),
        sa.Column("bound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "(argilla_dataset_id IS NULL AND argilla_record_id IS NULL) OR "
            "(argilla_dataset_id IS NOT NULL AND argilla_record_id IS NOT NULL)",
            name="remote_binding_complete",
        ),
        sa.CheckConstraint(
            "(argilla_record_id IS NULL AND bound_at IS NULL) OR "
            "(argilla_record_id IS NOT NULL AND bound_at IS NOT NULL)",
            name="bound_at_complete",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "plan_id", "item_id"],
            [
                "allocation_assignment_items.workspace_id",
                "allocation_assignment_items.plan_id",
                "allocation_assignment_items.id",
            ],
            name="fk_allocation_record_bindings_item",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_allocation_record_bindings_workspace_id_workspaces",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("item_id", name="pk_allocation_record_bindings"),
    )
    op.create_index(
        "uq_allocation_record_bindings_remote_record",
        "allocation_record_bindings",
        ["argilla_dataset_id", "argilla_record_id"],
        unique=True,
        postgresql_where=sa.text("argilla_record_id IS NOT NULL"),
        sqlite_where=sa.text("argilla_record_id IS NOT NULL"),
    )

    op.create_table(
        "allocation_assignments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("planner_assignment_id", sa.String(length=255), nullable=False),
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("phase", enum_types["allocation_phase"], nullable=False),
        sa.Column("role", enum_types["allocation_assignment_role"], nullable=False),
        sa.Column("assignee_mapping_id", sa.Uuid(), nullable=True),
        sa.Column("pool_id", sa.Uuid(), nullable=True),
        sa.Column("required_submissions", sa.Integer(), nullable=False),
        sa.Column("gate_id", sa.String(length=255), nullable=True),
        sa.CheckConstraint(
            "length(trim(planner_assignment_id)) > 0",
            name="planner_key_not_blank",
        ),
        sa.CheckConstraint(
            "required_submissions > 0",
            name="required_submissions_positive",
        ),
        sa.CheckConstraint(
            "(assignee_mapping_id IS NOT NULL AND pool_id IS NULL "
            "AND required_submissions = 1 AND role IN ('primary', 'overlap', 'calibration')) OR "
            "(assignee_mapping_id IS NULL AND pool_id IS NOT NULL "
            "AND role IN ('shared', 'overlap'))",
            name="role_target_shape",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "plan_id", "item_id"],
            [
                "allocation_assignment_items.workspace_id",
                "allocation_assignment_items.plan_id",
                "allocation_assignment_items.id",
            ],
            name="fk_allocation_assignments_item",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "assignee_mapping_id"],
            ["argilla_annotator_mappings.workspace_id", "argilla_annotator_mappings.id"],
            name="fk_allocation_assignments_workspace_assignee",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_allocation_assignments"),
        sa.UniqueConstraint(
            "workspace_id",
            "plan_id",
            "id",
            name="uq_allocation_assignments_scope_id",
        ),
        sa.UniqueConstraint(
            "plan_id",
            "planner_assignment_id",
            name="uq_allocation_assignments_plan_key",
        ),
    )
    op.create_index(
        "uq_allocation_assignments_item_direct_assignee",
        "allocation_assignments",
        ["item_id", "assignee_mapping_id"],
        unique=True,
        postgresql_where=sa.text("assignee_mapping_id IS NOT NULL"),
        sqlite_where=sa.text("assignee_mapping_id IS NOT NULL"),
    )
    op.create_index(
        "uq_allocation_assignments_item_pool",
        "allocation_assignments",
        ["item_id"],
        unique=True,
        postgresql_where=sa.text("assignee_mapping_id IS NULL"),
        sqlite_where=sa.text("assignee_mapping_id IS NULL"),
    )
    op.create_index(
        "ix_allocation_assignments_plan_assignee",
        "allocation_assignments",
        ["plan_id", "assignee_mapping_id"],
    )

    op.create_table(
        "allocation_collection_receipts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("assignment_id", sa.Uuid(), nullable=True),
        sa.Column("connection_binding_id", sa.Uuid(), nullable=False),
        sa.Column("argilla_dataset_id", sa.Uuid(), nullable=False),
        sa.Column("argilla_record_id", sa.Uuid(), nullable=False),
        sa.Column("argilla_response_id", sa.Uuid(), nullable=False),
        sa.Column("argilla_respondent_id", sa.Uuid(), nullable=False),
        sa.Column("response_status", sa.String(length=64), nullable=False),
        sa.Column("disposition", enum_types["collection_disposition"], nullable=False),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("labels_hash", sa.String(length=64), nullable=False),
        sa.Column("pulled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_principal_id", sa.Uuid(), nullable=False),
        sa.Column("caller_principal_id", sa.Uuid(), nullable=False),
        sa.Column("channel", audit_channel, nullable=False),
        sa.Column("idempotency_record_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "length(trim(response_status)) > 0",
            name="response_status_not_blank",
        ),
        sa.CheckConstraint(
            "length(labels_hash) = 64",
            name="labels_hash_sha256",
        ),
        sa.CheckConstraint(
            "(disposition = 'accepted' AND assignment_id IS NOT NULL AND rejection_reason IS NULL) OR "
            "(disposition = 'quarantined' AND rejection_reason IS NOT NULL "
            "AND length(trim(rejection_reason)) > 0)",
            name="disposition_reason",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "plan_id", "item_id"],
            [
                "allocation_assignment_items.workspace_id",
                "allocation_assignment_items.plan_id",
                "allocation_assignment_items.id",
            ],
            name="fk_allocation_collection_receipts_item",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "plan_id", "assignment_id"],
            [
                "allocation_assignments.workspace_id",
                "allocation_assignments.plan_id",
                "allocation_assignments.id",
            ],
            name="fk_allocation_collection_receipts_assignment",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "connection_binding_id"],
            ["argilla_connection_bindings.workspace_id", "argilla_connection_bindings.id"],
            name="fk_allocation_collection_receipts_binding",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_allocation_collection_receipts_workspace_id_workspaces",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_principal_id"],
            ["principals.id"],
            name="fk_allocation_collection_receipts_actor_principal_id_principals",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["caller_principal_id"],
            ["principals.id"],
            name="fk_allocation_collection_receipts_caller",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["idempotency_record_id"],
            ["idempotency_records.id"],
            name="fk_allocation_collection_receipts_idempotency",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_allocation_collection_receipts"),
        sa.UniqueConstraint(
            "connection_binding_id",
            "argilla_response_id",
            name="uq_allocation_collection_receipts_remote_response",
        ),
        sa.UniqueConstraint(
            "idempotency_record_id",
            name="uq_allocation_collection_receipts_idempotency",
        ),
    )
    op.create_index(
        "ix_allocation_collection_receipts_item_pulled",
        "allocation_collection_receipts",
        ["item_id", "pulled_at"],
    )
    op.create_index(
        "ix_allocation_collection_receipts_respondent_pulled",
        "allocation_collection_receipts",
        ["argilla_respondent_id", "pulled_at"],
    )
    op.create_index(
        "uq_allocation_collection_receipts_accepted_respondent",
        "allocation_collection_receipts",
        ["assignment_id", "argilla_respondent_id"],
        unique=True,
        postgresql_where=sa.text("disposition = 'accepted' AND assignment_id IS NOT NULL"),
        sqlite_where=sa.text("disposition = 'accepted' AND assignment_id IS NOT NULL"),
    )


def _install_postgres_triggers() -> None:
    statements = (
        """
        CREATE FUNCTION lls_argilla_connection_binding_freeze()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF OLD.argilla_workspace_id IS NOT NULL THEN
                    RAISE EXCEPTION 'bound Argilla connection cannot be deleted' USING ERRCODE = '55000';
                END IF;
                RETURN OLD;
            END IF;
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
               OR NEW.connection_config_id IS DISTINCT FROM OLD.connection_config_id
               OR NEW.created_at IS DISTINCT FROM OLD.created_at
               OR (
                    OLD.argilla_workspace_id IS NOT NULL
                    AND (
                        NEW.argilla_workspace_id IS DISTINCT FROM OLD.argilla_workspace_id
                        OR NEW.argilla_workspace_name_snapshot
                           IS DISTINCT FROM OLD.argilla_workspace_name_snapshot
                    )
               ) THEN
                RAISE EXCEPTION 'Argilla workspace binding is immutable' USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$
        """,
        """
        CREATE FUNCTION lls_argilla_annotator_mapping_freeze()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF OLD.argilla_user_id IS NOT NULL OR OLD.personal_argilla_workspace_id IS NOT NULL THEN
                    RAISE EXCEPTION 'bound Argilla annotator mapping cannot be deleted'
                        USING ERRCODE = '55000';
                END IF;
                RETURN OLD;
            END IF;
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
               OR NEW.connection_binding_id IS DISTINCT FROM OLD.connection_binding_id
               OR NEW.principal_id IS DISTINCT FROM OLD.principal_id
               OR NEW.created_at IS DISTINCT FROM OLD.created_at
               OR (
                    OLD.argilla_user_id IS NOT NULL
                    AND NEW.argilla_user_id IS DISTINCT FROM OLD.argilla_user_id
               )
               OR (
                    OLD.personal_argilla_workspace_id IS NOT NULL
                    AND NEW.personal_argilla_workspace_id IS DISTINCT FROM OLD.personal_argilla_workspace_id
               ) THEN
                RAISE EXCEPTION 'Argilla annotator UUID binding is immutable' USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$
        """,
        """
        CREATE FUNCTION lls_cohort_member_guard()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            target_revision_id uuid;
            target_workspace_id uuid;
            revision_sealed_at timestamptz;
            revision_binding_id uuid;
            mapping_binding_id uuid;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                target_revision_id := OLD.cohort_revision_id;
                target_workspace_id := OLD.workspace_id;
            ELSE
                target_revision_id := NEW.cohort_revision_id;
                target_workspace_id := NEW.workspace_id;
            END IF;

            SELECT sealed_at, connection_binding_id
              INTO revision_sealed_at, revision_binding_id
              FROM annotator_cohort_revisions
             WHERE id = target_revision_id AND workspace_id = target_workspace_id
             FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'cohort revision does not exist' USING ERRCODE = '23503';
            END IF;
            IF revision_sealed_at IS NOT NULL THEN
                RAISE EXCEPTION 'sealed cohort members are immutable' USING ERRCODE = '55000';
            END IF;
            IF TG_OP = 'UPDATE' AND (
                NEW.id IS DISTINCT FROM OLD.id
                OR NEW.cohort_revision_id IS DISTINCT FROM OLD.cohort_revision_id
                OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
                OR NEW.annotator_mapping_id IS DISTINCT FROM OLD.annotator_mapping_id
                OR NEW.created_at IS DISTINCT FROM OLD.created_at
            ) THEN
                RAISE EXCEPTION 'cohort member identity is immutable' USING ERRCODE = '55000';
            END IF;
            IF TG_OP <> 'DELETE' AND revision_binding_id IS NOT NULL THEN
                SELECT connection_binding_id
                  INTO mapping_binding_id
                  FROM argilla_annotator_mappings
                 WHERE id = NEW.annotator_mapping_id AND workspace_id = NEW.workspace_id;
                IF mapping_binding_id IS DISTINCT FROM revision_binding_id THEN
                    RAISE EXCEPTION 'cohort member must use the revision binding' USING ERRCODE = '23514';
                END IF;
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$
        """,
        """
        CREATE FUNCTION lls_cohort_revision_guard()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            actual_member_count integer;
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.sealed_at IS NOT NULL OR NEW.connection_binding_id IS NOT NULL THEN
                    RAISE EXCEPTION 'cohort revision must be inserted unsealed' USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'DELETE' THEN
                IF OLD.sealed_at IS NOT NULL THEN
                    RAISE EXCEPTION 'sealed cohort revision is immutable' USING ERRCODE = '55000';
                END IF;
                RETURN OLD;
            END IF;
            IF OLD.sealed_at IS NOT NULL THEN
                RAISE EXCEPTION 'sealed cohort revision is immutable' USING ERRCODE = '55000';
            END IF;
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
               OR NEW.cohort_id IS DISTINCT FROM OLD.cohort_id
               OR NEW.revision_number IS DISTINCT FROM OLD.revision_number
               OR NEW.fingerprint IS DISTINCT FROM OLD.fingerprint
               OR NEW.member_count IS DISTINCT FROM OLD.member_count
               OR NEW.actor_principal_id IS DISTINCT FROM OLD.actor_principal_id
               OR NEW.caller_principal_id IS DISTINCT FROM OLD.caller_principal_id
               OR NEW.channel IS DISTINCT FROM OLD.channel
               OR NEW.idempotency_record_id IS DISTINCT FROM OLD.idempotency_record_id
               OR NEW.created_at IS DISTINCT FROM OLD.created_at
               OR NEW.sealed_at IS NULL
               OR NEW.connection_binding_id IS NULL THEN
                RAISE EXCEPTION 'only cohort sealing may update a revision' USING ERRCODE = '55000';
            END IF;

            SELECT count(*) INTO actual_member_count
              FROM annotator_cohort_members
             WHERE cohort_revision_id = OLD.id;
            IF actual_member_count <> OLD.member_count THEN
                RAISE EXCEPTION 'cohort member count does not match sealed revision' USING ERRCODE = '23514';
            END IF;
            IF EXISTS (
                SELECT 1
                  FROM annotator_cohort_members member
                  JOIN argilla_annotator_mappings mapping
                    ON mapping.id = member.annotator_mapping_id
                   AND mapping.workspace_id = member.workspace_id
                 WHERE member.cohort_revision_id = OLD.id
                   AND (
                        mapping.connection_binding_id IS DISTINCT FROM NEW.connection_binding_id
                        OR mapping.argilla_user_id IS NULL
                   )
            ) THEN
                RAISE EXCEPTION 'cohort members must be bound users on one connection' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$
        """,
        """
        CREATE FUNCTION lls_allocation_plan_contract()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            revision_binding_id uuid;
            revision_sealed_at timestamptz;
        BEGIN
            SELECT connection_binding_id, sealed_at
              INTO revision_binding_id, revision_sealed_at
              FROM annotator_cohort_revisions
             WHERE id = NEW.cohort_revision_id AND workspace_id = NEW.workspace_id
             FOR UPDATE;
            IF NOT FOUND OR revision_sealed_at IS NULL
               OR revision_binding_id IS DISTINCT FROM NEW.connection_binding_id THEN
                RAISE EXCEPTION 'allocation plan requires a sealed cohort on the same binding'
                    USING ERRCODE = '23514';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM task_revisions revision
                 WHERE revision.id = NEW.task_revision_id
                   AND revision.workspace_id = NEW.workspace_id
                   AND revision.task_id = NEW.task_id
                   AND revision.content_hash = NEW.task_revision_hash
            ) THEN
                RAISE EXCEPTION 'allocation plan task revision hash mismatch' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$
        """,
        """
        CREATE FUNCTION lls_allocation_plan_create_state()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            INSERT INTO allocation_plan_states (plan_id, workspace_id, lifecycle_state)
            VALUES (NEW.id, NEW.workspace_id, 'draft');
            RETURN NEW;
        END;
        $$
        """,
        """
        CREATE FUNCTION lls_validate_allocation_plan_graph(target_plan_id uuid)
        RETURNS void LANGUAGE plpgsql AS $$
        BEGIN
            IF EXISTS (
                SELECT 1
                  FROM allocation_workspace_groups workspace_group
                  JOIN allocation_plans plan
                    ON plan.id = workspace_group.plan_id
                   AND plan.workspace_id = workspace_group.workspace_id
                 WHERE workspace_group.plan_id = target_plan_id
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
            ) THEN
                RAISE EXCEPTION 'allocation workspace group graph is invalid' USING ERRCODE = '23514';
            END IF;

            IF EXISTS (
                SELECT 1 FROM allocation_dataset_groups candidate
                 WHERE candidate.plan_id = target_plan_id
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
                                    AND candidate.assignee_mapping_id
                                        IS NOT DISTINCT FROM workspace_group.assignee_mapping_id
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
            ) THEN
                RAISE EXCEPTION 'allocation dataset group graph is invalid' USING ERRCODE = '23514';
            END IF;

            IF EXISTS (
                SELECT 1 FROM allocation_assignments candidate
                 WHERE candidate.plan_id = target_plan_id
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
                                    AND dataset_group.assignee_mapping_id
                                        IS NOT DISTINCT FROM candidate.assignee_mapping_id
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
                                    AND dataset_group.pool_id IS NOT DISTINCT FROM candidate.pool_id
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
            ) THEN
                RAISE EXCEPTION 'allocation assignment graph is invalid' USING ERRCODE = '23514';
            END IF;

            IF EXISTS (
                SELECT 1 FROM allocation_dataset_groups dataset_group
                 WHERE dataset_group.plan_id = target_plan_id
                   AND NOT EXISTS (
                        SELECT 1 FROM allocation_dataset_group_states state
                         WHERE state.dataset_group_id = dataset_group.id
                           AND state.plan_id = dataset_group.plan_id
                           AND state.workspace_id = dataset_group.workspace_id
                   )
            ) THEN
                RAISE EXCEPTION 'allocation dataset state graph is incomplete' USING ERRCODE = '23514';
            END IF;

            IF EXISTS (
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
                 WHERE state.plan_id = target_plan_id
                   AND state.argilla_workspace_id IS NOT NULL
                   AND state.argilla_workspace_id IS DISTINCT FROM CASE
                        WHEN workspace_group.mode = 'personal'
                            THEN mapping.personal_argilla_workspace_id
                        ELSE binding.argilla_workspace_id
                   END
            ) THEN
                RAISE EXCEPTION 'allocation dataset remote workspace graph is invalid'
                    USING ERRCODE = '23514';
            END IF;

            IF EXISTS (
                SELECT 1 FROM allocation_assignment_items item
                 WHERE item.plan_id = target_plan_id
                   AND NOT EXISTS (
                        SELECT 1 FROM allocation_record_bindings binding
                         WHERE binding.item_id = item.id
                           AND binding.plan_id = item.plan_id
                           AND binding.workspace_id = item.workspace_id
                   )
            ) THEN
                RAISE EXCEPTION 'allocation record binding graph is incomplete' USING ERRCODE = '23514';
            END IF;

            IF EXISTS (
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
                 WHERE binding.plan_id = target_plan_id
                   AND binding.argilla_record_id IS NOT NULL
                   AND binding.argilla_dataset_id IS DISTINCT FROM state.argilla_dataset_id
            ) THEN
                RAISE EXCEPTION 'allocation remote record graph is invalid' USING ERRCODE = '23514';
            END IF;
        END;
        $$
        """,
        """
        CREATE FUNCTION lls_allocation_plan_state_guard()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.lifecycle_state <> 'draft'
                   OR NEW.confirmed_at IS NOT NULL
                   OR NEW.confirmed_by_principal_id IS NOT NULL
                   OR NEW.confirmed_via_caller_principal_id IS NOT NULL
                   OR NEW.confirmation_idempotency_record_id IS NOT NULL THEN
                    RAISE EXCEPTION 'allocation plan state must be inserted as draft' USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'DELETE' THEN
                IF EXISTS (
                    SELECT 1 FROM allocation_plans plan
                     WHERE plan.id = OLD.plan_id AND plan.workspace_id = OLD.workspace_id
                ) THEN
                    RAISE EXCEPTION 'allocation plan state cannot be deleted directly'
                        USING ERRCODE = '55000';
                END IF;
                RETURN OLD;
            END IF;
            IF OLD.lifecycle_state <> 'draft'
               OR NEW.lifecycle_state <> 'confirmed'
               OR NEW.plan_id IS DISTINCT FROM OLD.plan_id
               OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
               OR NEW.created_at IS DISTINCT FROM OLD.created_at
               OR NEW.confirmed_at IS NULL
               OR NEW.confirmed_by_principal_id IS NULL
               OR NEW.confirmed_via_caller_principal_id IS NULL
               OR NEW.confirmation_idempotency_record_id IS NULL THEN
                RAISE EXCEPTION 'allocation plan may only transition from draft to confirmed'
                    USING ERRCODE = '55000';
            END IF;
            PERFORM lls_validate_allocation_plan_graph(OLD.plan_id);
            RETURN NEW;
        END;
        $$
        """,
        """
        CREATE FUNCTION lls_confirmed_plan_child_guard()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            old_plan_id uuid;
            new_plan_id uuid;
        BEGIN
            IF TG_TABLE_NAME = 'allocation_plans' THEN
                IF TG_OP <> 'INSERT' THEN old_plan_id := OLD.id; END IF;
                IF TG_OP <> 'DELETE' THEN new_plan_id := NEW.id; END IF;
            ELSIF TG_TABLE_NAME = 'allocation_workspace_groups'
               OR TG_TABLE_NAME = 'allocation_dataset_groups'
               OR TG_TABLE_NAME = 'allocation_assignment_items'
               OR TG_TABLE_NAME = 'allocation_assignments' THEN
                IF TG_OP <> 'INSERT' THEN old_plan_id := OLD.plan_id; END IF;
                IF TG_OP <> 'DELETE' THEN new_plan_id := NEW.plan_id; END IF;
            ELSE
                RAISE EXCEPTION 'unsupported allocation plan child table %', TG_TABLE_NAME;
            END IF;

            IF TG_OP = 'DELETE'
               AND TG_TABLE_NAME <> 'allocation_plans'
               AND NOT EXISTS (
                    SELECT 1 FROM allocation_plans plan WHERE plan.id = old_plan_id
               ) THEN
                RETURN OLD;
            END IF;

            PERFORM 1
              FROM allocation_plan_states
             WHERE plan_id IN (old_plan_id, new_plan_id)
             ORDER BY plan_id
             FOR UPDATE;
            IF old_plan_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM allocation_plan_states WHERE plan_id = old_plan_id
            ) THEN
                RAISE EXCEPTION 'allocation plan state is missing' USING ERRCODE = '23503';
            END IF;
            IF new_plan_id IS NOT NULL AND new_plan_id IS DISTINCT FROM old_plan_id AND NOT EXISTS (
                SELECT 1 FROM allocation_plan_states WHERE plan_id = new_plan_id
            ) THEN
                RAISE EXCEPTION 'allocation plan state is missing' USING ERRCODE = '23503';
            END IF;
            IF EXISTS (
                SELECT 1 FROM allocation_plan_states
                 WHERE plan_id IN (old_plan_id, new_plan_id)
                   AND lifecycle_state = 'confirmed'
            ) THEN
                RAISE EXCEPTION '% is immutable after confirmation', TG_TABLE_NAME USING ERRCODE = '55000';
            END IF;

            IF TG_OP = 'UPDATE' THEN
                RAISE EXCEPTION '% is immutable after insert', TG_TABLE_NAME USING ERRCODE = '55000';
            END IF;
            IF TG_OP = 'DELETE' THEN
                IF TG_TABLE_NAME = 'allocation_workspace_groups' THEN
                    IF EXISTS (
                        SELECT 1 FROM allocation_plans parent
                         WHERE parent.id = OLD.plan_id AND parent.workspace_id = OLD.workspace_id
                    ) THEN
                        RAISE EXCEPTION 'allocation_workspace_groups cannot be deleted directly'
                            USING ERRCODE = '55000';
                    END IF;
                ELSIF TG_TABLE_NAME = 'allocation_dataset_groups' THEN
                    IF EXISTS (
                        SELECT 1 FROM allocation_workspace_groups parent
                         WHERE parent.id = OLD.workspace_group_id
                           AND parent.plan_id = OLD.plan_id
                           AND parent.workspace_id = OLD.workspace_id
                    ) THEN
                        RAISE EXCEPTION 'allocation_dataset_groups cannot be deleted directly'
                            USING ERRCODE = '55000';
                    END IF;
                ELSIF TG_TABLE_NAME = 'allocation_assignment_items' THEN
                    IF EXISTS (
                        SELECT 1 FROM allocation_dataset_groups parent
                         WHERE parent.id = OLD.dataset_group_id
                           AND parent.plan_id = OLD.plan_id
                           AND parent.workspace_id = OLD.workspace_id
                    ) THEN
                        RAISE EXCEPTION 'allocation_assignment_items cannot be deleted directly'
                            USING ERRCODE = '55000';
                    END IF;
                ELSIF TG_TABLE_NAME = 'allocation_assignments' THEN
                    IF EXISTS (
                        SELECT 1 FROM allocation_assignment_items parent
                         WHERE parent.id = OLD.item_id
                           AND parent.plan_id = OLD.plan_id
                           AND parent.workspace_id = OLD.workspace_id
                    ) THEN
                        RAISE EXCEPTION 'allocation_assignments cannot be deleted directly'
                            USING ERRCODE = '55000';
                    END IF;
                END IF;
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$
        """,
        """
        CREATE FUNCTION lls_workspace_group_contract()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.assignee_mapping_id IS NOT NULL AND NOT EXISTS (
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
            ) THEN
                RAISE EXCEPTION 'workspace group assignee must be a cohort member on the plan binding'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$
        """,
        """
        CREATE FUNCTION lls_dataset_group_contract()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (
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
                            AND NEW.assignee_mapping_id IS NOT DISTINCT FROM workspace_group.assignee_mapping_id
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
            ) THEN
                RAISE EXCEPTION 'allocation dataset group target is invalid' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$
        """,
        """
        CREATE FUNCTION lls_dataset_group_create_state()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            INSERT INTO allocation_dataset_group_states (
                dataset_group_id, workspace_id, plan_id, materialization_state, attempt_count
            ) VALUES (NEW.id, NEW.workspace_id, NEW.plan_id, 'pending', 0);
            RETURN NEW;
        END;
        $$
        """,
        """
        CREATE FUNCTION lls_dataset_group_state_guard()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF EXISTS (
                    SELECT 1 FROM allocation_dataset_groups parent
                     WHERE parent.id = OLD.dataset_group_id
                       AND parent.plan_id = OLD.plan_id
                       AND parent.workspace_id = OLD.workspace_id
                ) THEN
                    RAISE EXCEPTION 'dataset group state cannot be deleted directly'
                        USING ERRCODE = '55000';
                END IF;
                RETURN OLD;
            END IF;
            IF TG_OP = 'UPDATE' AND (
                NEW.dataset_group_id IS DISTINCT FROM OLD.dataset_group_id
                OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
                OR NEW.plan_id IS DISTINCT FROM OLD.plan_id
                OR (
                    OLD.argilla_dataset_id IS NOT NULL
                    AND (
                        NEW.argilla_workspace_id IS DISTINCT FROM OLD.argilla_workspace_id
                        OR NEW.argilla_dataset_id IS DISTINCT FROM OLD.argilla_dataset_id
                    )
                )
            ) THEN
                RAISE EXCEPTION 'dataset group remote binding is immutable' USING ERRCODE = '55000';
            END IF;
            IF NEW.materialization_state = 'ready' AND NEW.argilla_dataset_id IS NULL THEN
                RAISE EXCEPTION 'ready dataset group requires a remote binding' USING ERRCODE = '23514';
            END IF;
            IF NEW.argilla_workspace_id IS NOT NULL AND NOT EXISTS (
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
                   AND NEW.argilla_workspace_id IS NOT DISTINCT FROM CASE
                        WHEN workspace_group.mode = 'personal' THEN mapping.personal_argilla_workspace_id
                        ELSE binding.argilla_workspace_id
                   END
            ) THEN
                RAISE EXCEPTION 'dataset group remote workspace does not match the plan binding'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$
        """,
        """
        CREATE FUNCTION lls_assignment_item_create_binding()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            INSERT INTO allocation_record_bindings (item_id, workspace_id, plan_id)
            VALUES (NEW.id, NEW.workspace_id, NEW.plan_id);
            RETURN NEW;
        END;
        $$
        """,
        """
        CREATE FUNCTION lls_record_binding_guard()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.argilla_dataset_id IS NOT NULL
                   OR NEW.argilla_record_id IS NOT NULL
                   OR NEW.bound_at IS NOT NULL THEN
                    RAISE EXCEPTION 'record binding must be inserted without remote UUIDs'
                        USING ERRCODE = '23514';
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM allocation_assignment_items item
                     WHERE item.id = NEW.item_id
                       AND item.workspace_id = NEW.workspace_id
                       AND item.plan_id = NEW.plan_id
                ) THEN
                    RAISE EXCEPTION 'record binding must match an existing assignment item'
                        USING ERRCODE = '23503';
                END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'DELETE' THEN
                IF EXISTS (
                    SELECT 1 FROM allocation_assignment_items parent
                     WHERE parent.id = OLD.item_id
                       AND parent.plan_id = OLD.plan_id
                       AND parent.workspace_id = OLD.workspace_id
                ) THEN
                    RAISE EXCEPTION 'record binding cannot be deleted directly'
                        USING ERRCODE = '55000';
                END IF;
                RETURN OLD;
            END IF;
            IF NEW.item_id IS DISTINCT FROM OLD.item_id
               OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
               OR NEW.plan_id IS DISTINCT FROM OLD.plan_id THEN
                RAISE EXCEPTION 'record binding identity is immutable' USING ERRCODE = '55000';
            END IF;
            IF OLD.argilla_record_id IS NOT NULL AND (
                NEW.argilla_dataset_id IS DISTINCT FROM OLD.argilla_dataset_id
                OR NEW.argilla_record_id IS DISTINCT FROM OLD.argilla_record_id
                OR NEW.bound_at IS DISTINCT FROM OLD.bound_at
            ) THEN
                RAISE EXCEPTION 'record remote binding is immutable' USING ERRCODE = '55000';
            END IF;
            PERFORM 1
              FROM allocation_assignment_items item
              JOIN allocation_dataset_group_states state
                ON state.dataset_group_id = item.dataset_group_id
               AND state.plan_id = item.plan_id
               AND state.workspace_id = item.workspace_id
             WHERE item.id = NEW.item_id
               AND item.plan_id = NEW.plan_id
               AND item.workspace_id = NEW.workspace_id
             FOR UPDATE OF state;
            IF NEW.argilla_record_id IS NOT NULL AND NOT EXISTS (
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
            ) THEN
                RAISE EXCEPTION 'record must bind to its dataset group remote dataset'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$
        """,
        """
        CREATE FUNCTION lls_assignment_contract()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (
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
                            AND dataset_group.assignee_mapping_id IS NOT DISTINCT FROM NEW.assignee_mapping_id
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
                            AND dataset_group.pool_id IS NOT DISTINCT FROM NEW.pool_id
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
            ) THEN
                RAISE EXCEPTION 'allocation assignment phase or target is invalid' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$
        """,
        """
        CREATE FUNCTION lls_collection_receipt_guard()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                  FROM allocation_plans plan
                  JOIN allocation_plan_states state ON state.plan_id = plan.id
                 WHERE plan.id = NEW.plan_id
                   AND plan.workspace_id = NEW.workspace_id
                   AND plan.connection_binding_id = NEW.connection_binding_id
                   AND state.lifecycle_state = 'confirmed'
            ) THEN
                RAISE EXCEPTION 'allocation receipt must use the confirmed plan binding'
                    USING ERRCODE = '23514';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM allocation_record_bindings binding
                 WHERE binding.item_id = NEW.item_id
                   AND binding.plan_id = NEW.plan_id
                   AND binding.workspace_id = NEW.workspace_id
                   AND binding.argilla_dataset_id = NEW.argilla_dataset_id
                   AND binding.argilla_record_id = NEW.argilla_record_id
            ) THEN
                RAISE EXCEPTION 'allocation receipt remote record binding is invalid'
                    USING ERRCODE = '23514';
            END IF;
            IF NEW.disposition = 'accepted' AND NOT EXISTS (
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
            ) THEN
                RAISE EXCEPTION 'accepted receipt assignment or respondent is invalid'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$
        """,
        """
        CREATE FUNCTION lls_reject_allocation_receipt_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'allocation_collection_receipts is append-only' USING ERRCODE = '55000';
        END;
        $$
        """,
        """
        CREATE FUNCTION lls_reject_allocation_truncate()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'allocation control tables cannot be truncated' USING ERRCODE = '55000';
        END;
        $$
        """,
    )
    for statement in statements:
        op.execute(statement)

    op.execute(
        """
        CREATE TRIGGER trg_argilla_connection_bindings_remote_freeze
        BEFORE UPDATE OR DELETE ON argilla_connection_bindings
        FOR EACH ROW EXECUTE FUNCTION lls_argilla_connection_binding_freeze()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_argilla_annotator_mappings_remote_freeze
        BEFORE UPDATE OR DELETE ON argilla_annotator_mappings
        FOR EACH ROW EXECUTE FUNCTION lls_argilla_annotator_mapping_freeze()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_annotator_cohort_members_guard
        BEFORE INSERT OR UPDATE OR DELETE ON annotator_cohort_members
        FOR EACH ROW EXECUTE FUNCTION lls_cohort_member_guard()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_annotator_cohort_revisions_guard
        BEFORE INSERT OR UPDATE OR DELETE ON annotator_cohort_revisions
        FOR EACH ROW EXECUTE FUNCTION lls_cohort_revision_guard()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_10_allocation_plans_contract
        BEFORE INSERT OR UPDATE ON allocation_plans
        FOR EACH ROW EXECUTE FUNCTION lls_allocation_plan_contract()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_allocation_plans_create_state
        AFTER INSERT ON allocation_plans
        FOR EACH ROW EXECUTE FUNCTION lls_allocation_plan_create_state()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_allocation_plan_states_guard
        BEFORE INSERT OR UPDATE OR DELETE ON allocation_plan_states
        FOR EACH ROW EXECUTE FUNCTION lls_allocation_plan_state_guard()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_00_allocation_plans_confirmed_guard
        BEFORE UPDATE OR DELETE ON allocation_plans
        FOR EACH ROW EXECUTE FUNCTION lls_confirmed_plan_child_guard()
        """
    )
    for table_name in (
        "allocation_workspace_groups",
        "allocation_dataset_groups",
        "allocation_assignment_items",
        "allocation_assignments",
    ):
        op.execute(
            f"""
            CREATE TRIGGER trg_00_{table_name}_confirmed_guard
            BEFORE INSERT OR UPDATE OR DELETE ON {table_name}
            FOR EACH ROW EXECUTE FUNCTION lls_confirmed_plan_child_guard()
            """
        )
    op.execute(
        """
        CREATE TRIGGER trg_10_allocation_workspace_groups_contract
        BEFORE INSERT OR UPDATE ON allocation_workspace_groups
        FOR EACH ROW EXECUTE FUNCTION lls_workspace_group_contract()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_10_allocation_dataset_groups_contract
        BEFORE INSERT OR UPDATE ON allocation_dataset_groups
        FOR EACH ROW EXECUTE FUNCTION lls_dataset_group_contract()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_allocation_dataset_groups_create_state
        AFTER INSERT ON allocation_dataset_groups
        FOR EACH ROW EXECUTE FUNCTION lls_dataset_group_create_state()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_allocation_dataset_group_states_guard
        BEFORE INSERT OR UPDATE OR DELETE ON allocation_dataset_group_states
        FOR EACH ROW EXECUTE FUNCTION lls_dataset_group_state_guard()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_allocation_assignment_items_create_binding
        AFTER INSERT ON allocation_assignment_items
        FOR EACH ROW EXECUTE FUNCTION lls_assignment_item_create_binding()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_allocation_record_bindings_guard
        BEFORE INSERT OR UPDATE OR DELETE ON allocation_record_bindings
        FOR EACH ROW EXECUTE FUNCTION lls_record_binding_guard()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_10_allocation_assignments_contract
        BEFORE INSERT OR UPDATE ON allocation_assignments
        FOR EACH ROW EXECUTE FUNCTION lls_assignment_contract()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_allocation_collection_receipts_contract
        BEFORE INSERT ON allocation_collection_receipts
        FOR EACH ROW EXECUTE FUNCTION lls_collection_receipt_guard()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_allocation_collection_receipts_append_only
        BEFORE UPDATE OR DELETE ON allocation_collection_receipts
        FOR EACH ROW EXECUTE FUNCTION lls_reject_allocation_receipt_mutation()
        """
    )
    for table_name in (
        "argilla_connection_bindings",
        "argilla_annotator_mappings",
        "annotator_cohorts",
        "annotator_cohort_revisions",
        "annotator_cohort_members",
        "allocation_plans",
        "allocation_plan_states",
        "allocation_workspace_groups",
        "allocation_dataset_groups",
        "allocation_dataset_group_states",
        "allocation_assignment_items",
        "allocation_record_bindings",
        "allocation_assignments",
        "allocation_collection_receipts",
    ):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table_name}_no_truncate
            BEFORE TRUNCATE ON {table_name}
            FOR EACH STATEMENT EXECUTE FUNCTION lls_reject_allocation_truncate()
            """
        )


def _install_sqlite_triggers() -> None:
    op.execute(
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
    op.execute(
        """
        CREATE TRIGGER trg_argilla_connection_bindings_bound_delete
        BEFORE DELETE ON argilla_connection_bindings
        WHEN OLD.argilla_workspace_id IS NOT NULL
        BEGIN
            SELECT RAISE(ABORT, 'bound Argilla connection cannot be deleted');
        END
        """
    )
    op.execute(
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
    op.execute(
        """
        CREATE TRIGGER trg_argilla_annotator_mappings_bound_delete
        BEFORE DELETE ON argilla_annotator_mappings
        WHEN OLD.argilla_user_id IS NOT NULL OR OLD.personal_argilla_workspace_id IS NOT NULL
        BEGIN
            SELECT RAISE(ABORT, 'bound Argilla annotator mapping cannot be deleted');
        END
        """
    )
    for operation in ("INSERT", "UPDATE"):
        member_update_guard = ""
        if operation == "UPDATE":
            member_update_guard = """
                SELECT RAISE(ABORT, 'cohort member identity is immutable')
                WHERE NEW.id IS NOT OLD.id
                   OR NEW.workspace_id IS NOT OLD.workspace_id
                   OR NEW.cohort_revision_id IS NOT OLD.cohort_revision_id
                   OR NEW.annotator_mapping_id IS NOT OLD.annotator_mapping_id
                   OR NEW.created_at IS NOT OLD.created_at;
            """
        op.execute(
            f"""
            CREATE TRIGGER trg_annotator_cohort_members_guard_{operation.lower()}
            BEFORE {operation} ON annotator_cohort_members
            BEGIN
                {member_update_guard}
                SELECT RAISE(ABORT, 'sealed cohort members are immutable')
                WHERE EXISTS (
                    SELECT 1 FROM annotator_cohort_revisions revision
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
    op.execute(
        """
        CREATE TRIGGER trg_annotator_cohort_members_guard_delete
        BEFORE DELETE ON annotator_cohort_members
        BEGIN
            SELECT RAISE(ABORT, 'sealed cohort members are immutable')
            WHERE EXISTS (
                SELECT 1 FROM annotator_cohort_revisions revision
                WHERE revision.id = OLD.cohort_revision_id
                  AND revision.workspace_id = OLD.workspace_id
                  AND revision.sealed_at IS NOT NULL
            );
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_annotator_cohort_revisions_unsealed_insert
        BEFORE INSERT ON annotator_cohort_revisions
        WHEN NEW.sealed_at IS NOT NULL OR NEW.connection_binding_id IS NOT NULL
        BEGIN
            SELECT RAISE(ABORT, 'cohort revision must be inserted unsealed');
        END
        """
    )
    op.execute(
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
    op.execute(
        """
        CREATE TRIGGER trg_annotator_cohort_revisions_sealed_delete
        BEFORE DELETE ON annotator_cohort_revisions
        WHEN OLD.sealed_at IS NOT NULL
        BEGIN
            SELECT RAISE(ABORT, 'sealed cohort revision is immutable');
        END
        """
    )
    for operation in ("INSERT", "UPDATE"):
        op.execute(
            f"""
            CREATE TRIGGER trg_allocation_plans_contract_{operation.lower()}
            BEFORE {operation} ON allocation_plans
            BEGIN
                SELECT RAISE(ABORT, 'allocation plan requires a sealed cohort on the same binding')
                WHERE NOT EXISTS (
                    SELECT 1 FROM annotator_cohort_revisions revision
                    WHERE revision.id = NEW.cohort_revision_id
                      AND revision.workspace_id = NEW.workspace_id
                      AND revision.sealed_at IS NOT NULL
                      AND revision.connection_binding_id = NEW.connection_binding_id
                );
                SELECT RAISE(ABORT, 'allocation plan task revision hash mismatch')
                WHERE NOT EXISTS (
                    SELECT 1 FROM task_revisions revision
                    WHERE revision.id = NEW.task_revision_id
                      AND revision.workspace_id = NEW.workspace_id
                      AND revision.task_id = NEW.task_id
                      AND revision.content_hash = NEW.task_revision_hash
                );
            END
            """
        )
    op.execute(
        """
        CREATE TRIGGER trg_allocation_plans_create_state
        AFTER INSERT ON allocation_plans
        BEGIN
            INSERT INTO allocation_plan_states (plan_id, workspace_id, lifecycle_state)
            VALUES (NEW.id, NEW.workspace_id, 'draft');
        END
        """
    )
    op.execute(
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
    op.execute(
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
    op.execute(
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
    op.execute(
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
    child_parent_exists = {
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
    for table_name, parent_exists in child_parent_exists.items():
        op.execute(
            f"""
            CREATE TRIGGER trg_{table_name}_confirmed_insert
            BEFORE INSERT ON {table_name}
            WHEN EXISTS (
                SELECT 1 FROM allocation_plan_states state
                WHERE state.plan_id = NEW.plan_id
                  AND state.lifecycle_state = 'confirmed'
            )
            BEGIN
                SELECT RAISE(ABORT, '{table_name} is immutable after confirmation');
            END
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER trg_{table_name}_confirmed_update
            BEFORE UPDATE ON {table_name}
            BEGIN
                SELECT RAISE(ABORT, '{table_name} is immutable after confirmation')
                WHERE EXISTS (
                    SELECT 1 FROM allocation_plan_states state
                    WHERE state.plan_id IN (OLD.plan_id, NEW.plan_id)
                      AND state.lifecycle_state = 'confirmed'
                );
                SELECT RAISE(ABORT, '{table_name} is immutable after insert');
            END
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER trg_{table_name}_confirmed_delete
            BEFORE DELETE ON {table_name}
            BEGIN
                SELECT RAISE(ABORT, '{table_name} is immutable after confirmation')
                WHERE EXISTS (
                    SELECT 1 FROM allocation_plan_states state
                    WHERE state.plan_id = OLD.plan_id
                      AND state.lifecycle_state = 'confirmed'
                );
                SELECT RAISE(ABORT, '{table_name} cannot be deleted directly')
                WHERE EXISTS ({parent_exists});
            END
            """
        )
    op.execute(
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
    op.execute(
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
    for operation in ("INSERT", "UPDATE"):
        op.execute(
            f"""
            CREATE TRIGGER trg_allocation_workspace_groups_contract_{operation.lower()}
            BEFORE {operation} ON allocation_workspace_groups
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
        op.execute(
            f"""
            CREATE TRIGGER trg_allocation_dataset_groups_contract_{operation.lower()}
            BEFORE {operation} ON allocation_dataset_groups
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
    op.execute(
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
    for operation in ("INSERT", "UPDATE"):
        op.execute(
            f"""
            CREATE TRIGGER trg_allocation_dataset_group_states_contract_{operation.lower()}
            BEFORE {operation} ON allocation_dataset_group_states
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
    op.execute(
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
    op.execute(
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
    op.execute(
        """
        CREATE TRIGGER trg_allocation_assignment_items_create_binding
        AFTER INSERT ON allocation_assignment_items
        BEGIN
            INSERT INTO allocation_record_bindings (item_id, workspace_id, plan_id)
            VALUES (NEW.id, NEW.workspace_id, NEW.plan_id);
        END
        """
    )
    op.execute(
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
    op.execute(
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
    op.execute(
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
    for operation in ("INSERT", "UPDATE"):
        op.execute(
            f"""
            CREATE TRIGGER trg_allocation_assignments_contract_{operation.lower()}
            BEFORE {operation} ON allocation_assignments
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
    op.execute(
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
                SELECT 1 FROM allocation_record_bindings binding
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
    op.execute(
        """
        CREATE TRIGGER trg_allocation_collection_receipts_append_only_update
        BEFORE UPDATE ON allocation_collection_receipts
        BEGIN
            SELECT RAISE(ABORT, 'allocation_collection_receipts is append-only');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_allocation_collection_receipts_append_only_delete
        BEFORE DELETE ON allocation_collection_receipts
        BEGIN
            SELECT RAISE(ABORT, 'allocation_collection_receipts is append-only');
        END
        """
    )


def upgrade() -> None:
    _create_tables()
    dialect_name = op.get_bind().dialect.name
    if dialect_name == "postgresql":
        _install_postgres_triggers()
    elif dialect_name == "sqlite":
        _install_sqlite_triggers()


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for function_name, signature in (
            ("lls_reject_allocation_truncate", "()"),
            ("lls_reject_allocation_receipt_mutation", "()"),
            ("lls_collection_receipt_guard", "()"),
            ("lls_assignment_contract", "()"),
            ("lls_record_binding_guard", "()"),
            ("lls_assignment_item_create_binding", "()"),
            ("lls_dataset_group_state_guard", "()"),
            ("lls_dataset_group_create_state", "()"),
            ("lls_dataset_group_contract", "()"),
            ("lls_workspace_group_contract", "()"),
            ("lls_confirmed_plan_child_guard", "()"),
            ("lls_validate_allocation_plan_graph", "(uuid)"),
            ("lls_allocation_plan_state_guard", "()"),
            ("lls_allocation_plan_create_state", "()"),
            ("lls_allocation_plan_contract", "()"),
            ("lls_cohort_revision_guard", "()"),
            ("lls_cohort_member_guard", "()"),
            ("lls_argilla_annotator_mapping_freeze", "()"),
            ("lls_argilla_connection_binding_freeze", "()"),
        ):
            op.execute(f"DROP FUNCTION IF EXISTS {function_name}{signature} CASCADE")

    for table_name in (
        "allocation_collection_receipts",
        "allocation_assignments",
        "allocation_record_bindings",
        "allocation_assignment_items",
        "allocation_dataset_group_states",
        "allocation_dataset_groups",
        "allocation_workspace_groups",
        "allocation_plan_states",
        "allocation_plans",
        "annotator_cohort_members",
        "annotator_cohort_revisions",
        "annotator_cohorts",
        "argilla_annotator_mappings",
        "argilla_connection_bindings",
    ):
        op.drop_table(table_name)

    if op.get_bind().dialect.name == "postgresql":
        for name, _values in reversed(ENUMS):
            postgresql.ENUM(name=name).drop(op.get_bind(), checkfirst=True)
