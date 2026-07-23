from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    PrimaryKeyConstraint,
    UniqueConstraint,
    inspect,
    select,
    text,
)
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from llm_labeling_scaffold.db.base import Base
from llm_labeling_scaffold.db.bootstrap import bootstrap_admin
from llm_labeling_scaffold.db.database import create_database_engine
from llm_labeling_scaffold.db.enums import (
    AllocationAssignmentRole,
    AllocationDatasetState,
    AllocationManifestKind,
    AllocationPhase,
    AllocationPlanLifecycle,
    AllocationStrategy,
    AllocationWorkspaceMode,
    ArgillaBindingState,
    AuditChannel,
    CollectionDisposition,
)
from llm_labeling_scaffold.db.migration import build_alembic_config
from llm_labeling_scaffold.db.models import (
    AllocationAssignment,
    AllocationAssignmentItem,
    AllocationCollectionReceipt,
    AllocationDatasetGroup,
    AllocationDatasetGroupState,
    AllocationPlan,
    AllocationPlanState,
    AllocationRecordBinding,
    AllocationWorkspaceGroup,
    AnnotationJob,
    AnnotatorCohort,
    AnnotatorCohortMember,
    AnnotatorCohortRevision,
    ArgillaAnnotatorMapping,
    ArgillaConnectionBinding,
    IdempotencyRecord,
    Task,
    TaskRevision,
)


ALLOCATION_TABLES = {
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
    "annotation_jobs",
}


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _db_uuid(value: uuid.UUID) -> str:
    return value.hex


def _metadata_schema_names(table_name: str, dialect_name: str) -> dict[str, set[str]]:
    table = Base.metadata.tables[table_name]
    return {
        "check": {
            constraint.name
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint)
            and constraint.name
            and not (dialect_name == "postgresql" and constraint._type_bound)
        },
        "foreign_key": {
            constraint.name
            for constraint in table.constraints
            if isinstance(constraint, ForeignKeyConstraint) and constraint.name
        },
        "primary_key": {
            constraint.name
            for constraint in table.constraints
            if isinstance(constraint, PrimaryKeyConstraint) and constraint.name
        },
        "unique": {
            constraint.name
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint) and constraint.name
        },
        "index": {index.name for index in table.indexes if index.name},
    }


def _database_schema_names(inspector, table_name: str) -> dict[str, set[str]]:
    primary_key_name = inspector.get_pk_constraint(table_name)["name"]
    return {
        "check": {constraint["name"] for constraint in inspector.get_check_constraints(table_name)},
        "foreign_key": {constraint["name"] for constraint in inspector.get_foreign_keys(table_name)},
        "primary_key": {primary_key_name} if primary_key_name else set(),
        "unique": {constraint["name"] for constraint in inspector.get_unique_constraints(table_name)},
        "index": {
            index["name"]
            for index in inspector.get_indexes(table_name)
            if not index.get("duplicates_constraint")
        },
    }


def test_database_schema_names_excludes_unique_constraint_index_mirrors():
    class InspectorStub:
        def get_pk_constraint(self, table_name):
            return {"name": "pk_example"}

        def get_check_constraints(self, table_name):
            return []

        def get_foreign_keys(self, table_name):
            return []

        def get_unique_constraints(self, table_name):
            return [{"name": "uq_example_value"}]

        def get_indexes(self, table_name):
            return [
                {
                    "name": "uq_example_value",
                    "unique": True,
                    "duplicates_constraint": "uq_example_value",
                },
                {"name": "ix_example_value", "unique": False},
            ]

    names = _database_schema_names(InspectorStub(), "example")

    assert names["unique"] == {"uq_example_value"}
    assert names["index"] == {"ix_example_value"}


def test_metadata_schema_names_excludes_only_enum_checks_for_postgres():
    for table_name in ALLOCATION_TABLES:
        table = Base.metadata.tables[table_name]
        explicit_checks = {
            constraint.name
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint)
            and constraint.name
            and not constraint._type_bound
        }
        enum_checks = {
            constraint.name
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint)
            and constraint.name
            and constraint._type_bound
        }

        sqlite_names = _metadata_schema_names(table_name, "sqlite")
        postgres_names = _metadata_schema_names(table_name, "postgresql")

        assert sqlite_names["check"] == explicit_checks | enum_checks
        assert postgres_names["check"] == explicit_checks
        assert sqlite_names["check"] - postgres_names["check"] == enum_checks


def test_annotation_job_schema_names_match_metadata_and_migration(allocation_engine):
    inspector = inspect(allocation_engine)
    assert _database_schema_names(inspector, "annotation_jobs") == _metadata_schema_names(
        "annotation_jobs",
        allocation_engine.dialect.name,
    )


@pytest.fixture(params=("metadata", "migration"))
def allocation_engine(request, tmp_path: Path):
    database_url = f"sqlite+pysqlite:///{tmp_path / f'allocation-{request.param}.db'}"
    engine = create_database_engine(database_url)
    if request.param == "metadata":
        Base.metadata.create_all(engine)
    else:
        command.upgrade(build_alembic_config(database_url), "head")
    try:
        yield engine
    finally:
        engine.dispose()


def _claim(session: Session, workspace_id: uuid.UUID, principal_id: uuid.UUID, operation: str) -> uuid.UUID:
    record = IdempotencyRecord(
        workspace_id=workspace_id,
        actor_principal_id=principal_id,
        caller_principal_id=principal_id,
        operation=operation,
        idempotency_key_hash=_hash(f"key:{operation}:{uuid.uuid4()}"),
        request_fingerprint=_hash(f"request:{operation}:{uuid.uuid4()}"),
        required_permission="task:create",
        resource_type="workspace",
        resource_id=workspace_id,
        channel=AuditChannel.API,
    )
    session.add(record)
    session.flush()
    return record.id


def _annotation_claim(
    session: Session,
    workspace_id: uuid.UUID,
    task_id: uuid.UUID,
    principal_id: uuid.UUID,
) -> uuid.UUID:
    record = IdempotencyRecord(
        workspace_id=workspace_id,
        actor_principal_id=principal_id,
        caller_principal_id=principal_id,
        operation=f"annotation.job.{uuid.uuid4()}",
        idempotency_key_hash=_hash(f"annotation-key:{uuid.uuid4()}"),
        request_fingerprint=_hash(f"annotation-request:{uuid.uuid4()}"),
        required_permission="annotation:review",
        resource_type="task",
        resource_id=task_id,
        channel=AuditChannel.API,
    )
    session.add(record)
    session.flush()
    return record.id


def _build_graph(engine) -> dict[str, uuid.UUID]:
    suffix = uuid.uuid4().hex[:10]
    with Session(engine) as session:
        bootstrap = bootstrap_admin(
            session,
            issuer="https://allocation-schema.example",
            subject=f"admin-{suffix}",
            workspace_slug=f"allocation-{suffix}",
            workspace_name="Allocation Schema",
        )

    with Session(engine) as session, session.begin():
        task = Task(
            workspace_id=bootstrap.workspace_id,
            task_key=f"task-{suffix}",
            name="Allocation Task",
            created_by_principal_id=bootstrap.principal_id,
        )
        session.add(task)
        session.flush()
        task_revision = TaskRevision(
            workspace_id=bootstrap.workspace_id,
            task_id=task.id,
            revision_number=1,
            draft_version=1,
            draft_fingerprint=_hash("draft"),
            definition={"task_id": task.task_key},
            rendered_task=f"task_id: {task.task_key}\n",
            reason="allocation schema fixture",
            actor_principal_id=bootstrap.principal_id,
            caller_principal_id=bootstrap.principal_id,
            channel=AuditChannel.API,
            content_hash=_hash("revision"),
            idempotency_record_id=_claim(
                session, bootstrap.workspace_id, bootstrap.principal_id, f"task.publish.{suffix}"
            ),
        )
        session.add(task_revision)
        session.flush()

        binding = ArgillaConnectionBinding(
            workspace_id=bootstrap.workspace_id,
            connection_config_id=f"argilla-{suffix}",
            server_version="2.0",
            state=ArgillaBindingState.ACTIVE,
            idempotency_record_id=_claim(
                session, bootstrap.workspace_id, bootstrap.principal_id, f"binding.create.{suffix}"
            ),
        )
        session.add(binding)
        session.flush()
        mapping_a = ArgillaAnnotatorMapping(
            workspace_id=bootstrap.workspace_id,
            connection_binding_id=binding.id,
            username_snapshot=f"annotator-a-{suffix}",
            idempotency_record_id=_claim(
                session, bootstrap.workspace_id, bootstrap.principal_id, f"mapping.a.{suffix}"
            ),
        )
        mapping_b = ArgillaAnnotatorMapping(
            workspace_id=bootstrap.workspace_id,
            connection_binding_id=binding.id,
            username_snapshot=f"annotator-b-{suffix}",
            idempotency_record_id=_claim(
                session, bootstrap.workspace_id, bootstrap.principal_id, f"mapping.b.{suffix}"
            ),
        )
        session.add_all([mapping_a, mapping_b])
        session.flush()

        cohort = AnnotatorCohort(
            workspace_id=bootstrap.workspace_id,
            name=f"cohort-{suffix}",
            idempotency_record_id=_claim(
                session, bootstrap.workspace_id, bootstrap.principal_id, f"cohort.create.{suffix}"
            ),
        )
        session.add(cohort)
        session.flush()
        cohort_revision = AnnotatorCohortRevision(
            workspace_id=bootstrap.workspace_id,
            cohort_id=cohort.id,
            revision_number=1,
            fingerprint=_hash("cohort"),
            member_count=2,
            actor_principal_id=bootstrap.principal_id,
            caller_principal_id=bootstrap.principal_id,
            channel=AuditChannel.API,
            idempotency_record_id=_claim(
                session, bootstrap.workspace_id, bootstrap.principal_id, f"cohort.revision.{suffix}"
            ),
        )
        session.add(cohort_revision)
        session.flush()
        session.add_all(
            [
                AnnotatorCohortMember(
                    workspace_id=bootstrap.workspace_id,
                    cohort_revision_id=cohort_revision.id,
                    annotator_mapping_id=mapping_a.id,
                    default_capacity=10,
                ),
                AnnotatorCohortMember(
                    workspace_id=bootstrap.workspace_id,
                    cohort_revision_id=cohort_revision.id,
                    annotator_mapping_id=mapping_b.id,
                    default_capacity=10,
                ),
            ]
        )
        session.flush()

        ids = {
            "workspace": bootstrap.workspace_id,
            "principal": bootstrap.principal_id,
            "task": task.id,
            "task_revision": task_revision.id,
            "binding": binding.id,
            "mapping_a": mapping_a.id,
            "mapping_b": mapping_b.id,
            "cohort": cohort.id,
            "cohort_revision": cohort_revision.id,
        }

    ids.update(
        {
            "remote_workspace": uuid.uuid4(),
            "remote_user_a": uuid.uuid4(),
            "remote_user_b": uuid.uuid4(),
            "personal_workspace_a": uuid.uuid4(),
            "personal_workspace_b": uuid.uuid4(),
        }
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE argilla_connection_bindings
                SET argilla_workspace_id = :remote_workspace,
                    argilla_workspace_name_snapshot = :workspace_name
                WHERE id = :binding_id
                """
            ),
            {
                "remote_workspace": _db_uuid(ids["remote_workspace"]),
                "workspace_name": f"remote-{suffix}",
                "binding_id": _db_uuid(ids["binding"]),
            },
        )
        for key, user_key, personal_key in (
            ("mapping_a", "remote_user_a", "personal_workspace_a"),
            ("mapping_b", "remote_user_b", "personal_workspace_b"),
        ):
            connection.execute(
                text(
                    """
                    UPDATE argilla_annotator_mappings
                    SET argilla_user_id = :remote_user,
                        personal_argilla_workspace_id = :personal_workspace
                    WHERE id = :mapping_id
                    """
                ),
                {
                    "remote_user": _db_uuid(ids[user_key]),
                    "personal_workspace": _db_uuid(ids[personal_key]),
                    "mapping_id": _db_uuid(ids[key]),
                },
            )
        connection.execute(
            text(
                """
                UPDATE annotator_cohort_revisions
                SET sealed_at = :sealed_at, connection_binding_id = :binding_id
                WHERE id = :revision_id
                """
            ),
            {
                "sealed_at": datetime.now(timezone.utc),
                "binding_id": _db_uuid(ids["binding"]),
                "revision_id": _db_uuid(ids["cohort_revision"]),
            },
        )

    with Session(engine) as session, session.begin():
        plan = AllocationPlan(
            workspace_id=ids["workspace"],
            connection_binding_id=ids["binding"],
            task_id=ids["task"],
            task_revision_id=ids["task_revision"],
            task_revision_hash=_hash("revision"),
            source_manifest_id=f"manifest-{suffix}",
            source_manifest_kind=AllocationManifestKind.SAMPLE,
            source_manifest_hash=_hash("manifest"),
            cohort_revision_id=ids["cohort_revision"],
            strategy=AllocationStrategy.FIXED_PARTITION,
            schema_version="allocation-plan-v1",
            seed=Decimal(17),
            algorithm_version="allocation-v1",
            input_fingerprint=_hash("input"),
            fingerprint=_hash(f"plan-{suffix}"),
            capacity_snapshot=[],
            qc_snapshot={},
            actor_principal_id=ids["principal"],
            caller_principal_id=ids["principal"],
            channel=AuditChannel.API,
            idempotency_record_id=_claim(
                session, ids["workspace"], ids["principal"], f"allocation.plan.{suffix}"
            ),
        )
        session.add(plan)
        session.flush()
        personal_group = AllocationWorkspaceGroup(
            workspace_id=ids["workspace"],
            plan_id=plan.id,
            planner_workspace_group_id=f"personal-{suffix}",
            phase=AllocationPhase.PRODUCTION,
            mode=AllocationWorkspaceMode.PERSONAL,
            assignee_mapping_id=ids["mapping_a"],
            member_mapping_ids=[str(ids["mapping_a"])],
        )
        shared_group = AllocationWorkspaceGroup(
            workspace_id=ids["workspace"],
            plan_id=plan.id,
            planner_workspace_group_id=f"shared-{suffix}",
            phase=AllocationPhase.PRODUCTION,
            mode=AllocationWorkspaceMode.SHARED,
            member_mapping_ids=[str(ids["mapping_a"]), str(ids["mapping_b"])],
        )
        session.add_all([personal_group, shared_group])
        session.flush()
        direct_dataset = AllocationDatasetGroup(
            workspace_id=ids["workspace"],
            plan_id=plan.id,
            workspace_group_id=personal_group.id,
            planner_dataset_group_id=f"direct-{suffix}",
            phase=AllocationPhase.PRODUCTION,
            min_submitted=1,
            assignee_mapping_id=ids["mapping_a"],
        )
        pooled_dataset = AllocationDatasetGroup(
            workspace_id=ids["workspace"],
            plan_id=plan.id,
            workspace_group_id=shared_group.id,
            planner_dataset_group_id=f"pooled-{suffix}",
            phase=AllocationPhase.PRODUCTION,
            min_submitted=2,
            pool_id=ids["cohort"],
        )
        session.add_all([direct_dataset, pooled_dataset])
        session.flush()
        direct_item = AllocationAssignmentItem(
            workspace_id=ids["workspace"],
            plan_id=plan.id,
            dataset_group_id=direct_dataset.id,
            record_id=f"direct-record-{suffix}",
            external_id=f"direct-record-{suffix}",
            batch_id="default",
            content_hash=_hash("direct-record"),
        )
        pooled_item = AllocationAssignmentItem(
            workspace_id=ids["workspace"],
            plan_id=plan.id,
            dataset_group_id=pooled_dataset.id,
            record_id=f"pooled-record-{suffix}",
            external_id=f"pooled-record-{suffix}",
            batch_id="default",
            content_hash=_hash("pooled-record"),
        )
        session.add_all([direct_item, pooled_item])
        session.flush()
        direct_assignment = AllocationAssignment(
            workspace_id=ids["workspace"],
            plan_id=plan.id,
            planner_assignment_id=f"direct-assignment-{suffix}",
            item_id=direct_item.id,
            phase=AllocationPhase.PRODUCTION,
            role=AllocationAssignmentRole.PRIMARY,
            assignee_mapping_id=ids["mapping_a"],
            required_submissions=1,
        )
        pooled_assignment = AllocationAssignment(
            workspace_id=ids["workspace"],
            plan_id=plan.id,
            planner_assignment_id=f"pooled-assignment-{suffix}",
            item_id=pooled_item.id,
            phase=AllocationPhase.PRODUCTION,
            role=AllocationAssignmentRole.OVERLAP,
            pool_id=ids["cohort"],
            required_submissions=2,
        )
        session.add_all([direct_assignment, pooled_assignment])
        session.flush()
        ids.update(
            {
                "plan": plan.id,
                "personal_group": personal_group.id,
                "shared_group": shared_group.id,
                "direct_dataset": direct_dataset.id,
                "pooled_dataset": pooled_dataset.id,
                "direct_item": direct_item.id,
                "pooled_item": pooled_item.id,
                "direct_assignment": direct_assignment.id,
                "pooled_assignment": pooled_assignment.id,
            }
        )

    ids.update(
        {
            "remote_direct_dataset": uuid.uuid4(),
            "remote_pooled_dataset": uuid.uuid4(),
            "remote_direct_record": uuid.uuid4(),
            "remote_pooled_record": uuid.uuid4(),
        }
    )
    with engine.begin() as connection:
        for dataset_key, workspace_key, remote_dataset_key in (
            ("direct_dataset", "personal_workspace_a", "remote_direct_dataset"),
            ("pooled_dataset", "remote_workspace", "remote_pooled_dataset"),
        ):
            connection.execute(
                text(
                    """
                    UPDATE allocation_dataset_group_states
                    SET materialization_state = 'ready',
                        argilla_workspace_id = :remote_workspace,
                        argilla_dataset_id = :remote_dataset,
                        materialized_at = :materialized_at
                    WHERE dataset_group_id = :dataset_group_id
                    """
                ),
                {
                    "remote_workspace": _db_uuid(ids[workspace_key]),
                    "remote_dataset": _db_uuid(ids[remote_dataset_key]),
                    "materialized_at": datetime.now(timezone.utc),
                    "dataset_group_id": _db_uuid(ids[dataset_key]),
                },
            )
        for item_key, remote_dataset_key, remote_record_key in (
            ("direct_item", "remote_direct_dataset", "remote_direct_record"),
            ("pooled_item", "remote_pooled_dataset", "remote_pooled_record"),
        ):
            connection.execute(
                text(
                    """
                    UPDATE allocation_record_bindings
                    SET argilla_dataset_id = :remote_dataset,
                        argilla_record_id = :remote_record,
                        bound_at = :bound_at
                    WHERE item_id = :item_id
                    """
                ),
                {
                    "remote_dataset": _db_uuid(ids[remote_dataset_key]),
                    "remote_record": _db_uuid(ids[remote_record_key]),
                    "bound_at": datetime.now(timezone.utc),
                    "item_id": _db_uuid(ids[item_key]),
                },
            )
    return ids


def _build_peer_draft_plan(engine, ids: dict[str, uuid.UUID]) -> dict[str, uuid.UUID]:
    suffix = uuid.uuid4().hex[:10]
    with Session(engine) as session, session.begin():
        plan = AllocationPlan(
            workspace_id=ids["workspace"],
            connection_binding_id=ids["binding"],
            task_id=ids["task"],
            task_revision_id=ids["task_revision"],
            task_revision_hash=_hash("revision"),
            source_manifest_id=f"peer-manifest-{suffix}",
            source_manifest_kind=AllocationManifestKind.SAMPLE,
            source_manifest_hash=_hash(f"peer-manifest-{suffix}"),
            cohort_revision_id=ids["cohort_revision"],
            strategy=AllocationStrategy.FIXED_PARTITION,
            schema_version="allocation-plan-v1",
            seed=Decimal(19),
            algorithm_version="allocation-v1",
            input_fingerprint=_hash(f"peer-input-{suffix}"),
            fingerprint=_hash(f"peer-plan-{suffix}"),
            capacity_snapshot=[],
            qc_snapshot={},
            actor_principal_id=ids["principal"],
            caller_principal_id=ids["principal"],
            channel=AuditChannel.API,
            idempotency_record_id=_claim(
                session, ids["workspace"], ids["principal"], f"allocation.peer.{suffix}"
            ),
        )
        session.add(plan)
        session.flush()
        group = AllocationWorkspaceGroup(
            workspace_id=ids["workspace"],
            plan_id=plan.id,
            planner_workspace_group_id=f"peer-personal-{suffix}",
            phase=AllocationPhase.PRODUCTION,
            mode=AllocationWorkspaceMode.PERSONAL,
            assignee_mapping_id=ids["mapping_a"],
            member_mapping_ids=[str(ids["mapping_a"])],
        )
        session.add(group)
        session.flush()
        dataset = AllocationDatasetGroup(
            workspace_id=ids["workspace"],
            plan_id=plan.id,
            workspace_group_id=group.id,
            planner_dataset_group_id=f"peer-dataset-{suffix}",
            phase=AllocationPhase.PRODUCTION,
            min_submitted=1,
            assignee_mapping_id=ids["mapping_a"],
        )
        session.add(dataset)
        session.flush()
        item = AllocationAssignmentItem(
            workspace_id=ids["workspace"],
            plan_id=plan.id,
            dataset_group_id=dataset.id,
            record_id=f"peer-record-{suffix}",
            external_id=f"peer-record-{suffix}",
            batch_id="default",
            content_hash=_hash(f"peer-record-{suffix}"),
        )
        session.add(item)
        session.flush()
        return {"plan": plan.id, "group": group.id, "dataset": dataset.id, "item": item.id}


def _build_unsealed_cohort_revision(engine, ids: dict[str, uuid.UUID]) -> dict[str, uuid.UUID]:
    with Session(engine) as session, session.begin():
        revision = AnnotatorCohortRevision(
            workspace_id=ids["workspace"],
            cohort_id=ids["cohort"],
            revision_number=2,
            fingerprint=_hash(f"unsealed-{uuid.uuid4()}"),
            member_count=1,
            actor_principal_id=ids["principal"],
            caller_principal_id=ids["principal"],
            channel=AuditChannel.API,
            idempotency_record_id=_claim(
                session,
                ids["workspace"],
                ids["principal"],
                f"cohort.unsealed.{uuid.uuid4()}",
            ),
        )
        session.add(revision)
        session.flush()
        member = AnnotatorCohortMember(
            workspace_id=ids["workspace"],
            cohort_revision_id=revision.id,
            annotator_mapping_id=ids["mapping_a"],
            default_capacity=10,
        )
        session.add(member)
        session.flush()
        return {"revision": revision.id, "member": member.id}


def _confirm_plan(engine, ids: dict[str, uuid.UUID]) -> uuid.UUID:
    with Session(engine) as session, session.begin():
        confirmation_id = _claim(
            session,
            ids["workspace"],
            ids["principal"],
            f"allocation.confirm.{uuid.uuid4()}",
        )
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE allocation_plan_states
                SET lifecycle_state = 'confirmed',
                    confirmed_at = :confirmed_at,
                    confirmed_by_principal_id = :principal_id,
                    confirmed_via_caller_principal_id = :principal_id,
                    confirmation_idempotency_record_id = :confirmation_id
                WHERE plan_id = :plan_id
                """
            ),
            {
                "confirmed_at": datetime.now(timezone.utc),
                "principal_id": _db_uuid(ids["principal"]),
                "confirmation_id": _db_uuid(confirmation_id),
                "plan_id": _db_uuid(ids["plan"]),
            },
        )
    return confirmation_id


def test_allocation_schema_metadata_and_migration_have_same_surface(allocation_engine):
    inspector = inspect(allocation_engine)
    assert ALLOCATION_TABLES <= set(inspector.get_table_names())
    assert {member.value for member in AllocationPlanLifecycle} == {"draft", "confirmed"}
    receipt_columns = {column["name"] for column in inspector.get_columns("allocation_collection_receipts")}
    assert {
        "argilla_dataset_id",
        "argilla_record_id",
        "argilla_response_id",
        "argilla_respondent_id",
    } <= receipt_columns
    for table_name in sorted(ALLOCATION_TABLES):
        expected_names = _metadata_schema_names(table_name, allocation_engine.dialect.name)
        actual_names = _database_schema_names(inspector, table_name)
        assert actual_names == expected_names
        assert all(
            len(name.encode("utf-8")) <= 63
            for names in actual_names.values()
            for name in names
        )


def test_remote_uuid_bindings_freeze_after_first_bind(allocation_engine):
    ids = _build_graph(allocation_engine)
    attacks = (
        (
            "UPDATE argilla_connection_bindings SET argilla_workspace_id = :new_id WHERE id = :row_id",
            ids["binding"],
        ),
        (
            "UPDATE argilla_annotator_mappings SET argilla_user_id = :new_id WHERE id = :row_id",
            ids["mapping_a"],
        ),
        (
            "UPDATE allocation_dataset_group_states SET argilla_dataset_id = :new_id "
            "WHERE dataset_group_id = :row_id",
            ids["direct_dataset"],
        ),
        (
            "UPDATE allocation_record_bindings SET argilla_record_id = :new_id WHERE item_id = :row_id",
            ids["direct_item"],
        ),
    )
    for statement, row_id in attacks:
        with pytest.raises(DBAPIError):
            with allocation_engine.begin() as connection:
                connection.execute(
                    text(statement),
                    {"new_id": _db_uuid(uuid.uuid4()), "row_id": _db_uuid(row_id)},
                )
    for statement, row_id in (
        ("DELETE FROM argilla_connection_bindings WHERE id = :row_id", ids["binding"]),
        ("DELETE FROM argilla_annotator_mappings WHERE id = :row_id", ids["mapping_a"]),
    ):
        with pytest.raises(DBAPIError):
            with allocation_engine.begin() as connection:
                connection.execute(text(statement), {"row_id": _db_uuid(row_id)})
    identity_attacks = (
        (
            "UPDATE argilla_connection_bindings SET connection_config_id = :value WHERE id = :row_id",
            {"value": f"replacement-{uuid.uuid4()}", "row_id": _db_uuid(ids["binding"])},
        ),
        (
            "UPDATE argilla_connection_bindings SET id = :value WHERE id = :row_id",
            {"value": _db_uuid(uuid.uuid4()), "row_id": _db_uuid(ids["binding"])},
        ),
        (
            "UPDATE argilla_connection_bindings SET created_at = :value WHERE id = :row_id",
            {"value": datetime(2000, 1, 1, tzinfo=timezone.utc), "row_id": _db_uuid(ids["binding"])},
        ),
        (
            "UPDATE argilla_annotator_mappings SET id = :value WHERE id = :row_id",
            {"value": _db_uuid(uuid.uuid4()), "row_id": _db_uuid(ids["mapping_a"])},
        ),
        (
            "UPDATE argilla_annotator_mappings SET principal_id = :value WHERE id = :row_id",
            {"value": _db_uuid(ids["principal"]), "row_id": _db_uuid(ids["mapping_a"])},
        ),
        (
            "UPDATE argilla_annotator_mappings SET created_at = :value WHERE id = :row_id",
            {"value": datetime(2000, 1, 1, tzinfo=timezone.utc), "row_id": _db_uuid(ids["mapping_a"])},
        ),
    )
    for statement, params in identity_attacks:
        with pytest.raises(DBAPIError):
            with allocation_engine.begin() as connection:
                connection.execute(text(statement), params)


def test_planner_graph_parents_freeze_and_draft_plan_delete_cascades(allocation_engine):
    ids = _build_graph(allocation_engine)
    attacks = (
        (
            "UPDATE allocation_workspace_groups SET assignee_mapping_id = :value "
            "WHERE id = :row_id",
            {"value": _db_uuid(ids["mapping_b"]), "row_id": _db_uuid(ids["personal_group"])},
        ),
        (
            "UPDATE allocation_workspace_groups SET mode = 'shared', assignee_mapping_id = NULL "
            "WHERE id = :row_id",
            {"row_id": _db_uuid(ids["personal_group"])},
        ),
        (
            "UPDATE allocation_dataset_groups SET assignee_mapping_id = :value WHERE id = :row_id",
            {"value": _db_uuid(ids["mapping_b"]), "row_id": _db_uuid(ids["direct_dataset"])},
        ),
        (
            "UPDATE allocation_dataset_groups SET min_submitted = 1 WHERE id = :row_id",
            {"row_id": _db_uuid(ids["pooled_dataset"])},
        ),
        (
            "DELETE FROM allocation_dataset_groups WHERE id = :row_id",
            {"row_id": _db_uuid(ids["direct_dataset"])},
        ),
        (
            "DELETE FROM allocation_assignments WHERE id = :row_id",
            {"row_id": _db_uuid(ids["direct_assignment"])},
        ),
    )
    for statement, params in attacks:
        with pytest.raises(DBAPIError):
            with allocation_engine.begin() as connection:
                connection.execute(text(statement), params)

    with allocation_engine.begin() as connection:
        connection.execute(
            text("DELETE FROM allocation_plans WHERE id = :plan_id"),
            {"plan_id": _db_uuid(ids["plan"])},
        )
    with allocation_engine.connect() as connection:
        assert connection.scalar(
            text("SELECT count(*) FROM allocation_plans WHERE id = :plan_id"),
            {"plan_id": _db_uuid(ids["plan"])},
        ) == 0
        for table_name in (
            "allocation_plan_states",
            "allocation_workspace_groups",
            "allocation_dataset_groups",
            "allocation_dataset_group_states",
            "allocation_assignment_items",
            "allocation_record_bindings",
            "allocation_assignments",
        ):
            assert connection.scalar(
                text(f"SELECT count(*) FROM {table_name} WHERE plan_id = :plan_id"),
                {"plan_id": _db_uuid(ids["plan"])},
            ) == 0
        assert connection.scalar(
            text("SELECT count(*) FROM argilla_connection_bindings WHERE id = :binding_id"),
            {"binding_id": _db_uuid(ids["binding"])},
        ) == 1


def test_record_binding_cannot_delete_reinsert_but_can_bind_once(allocation_engine):
    ids = _build_graph(allocation_engine)
    with Session(allocation_engine) as session, session.begin():
        item = AllocationAssignmentItem(
            workspace_id=ids["workspace"],
            plan_id=ids["plan"],
            dataset_group_id=ids["direct_dataset"],
            record_id=f"reinsert-{uuid.uuid4()}",
            external_id=f"reinsert-{uuid.uuid4()}",
            batch_id="default",
            content_hash=_hash(f"reinsert-{uuid.uuid4()}"),
        )
        session.add(item)
        session.flush()
        item_id = item.id

    injected_record_id = uuid.uuid4()
    with pytest.raises(DBAPIError):
        with allocation_engine.begin() as connection:
            connection.execute(
                text("DELETE FROM allocation_record_bindings WHERE item_id = :item_id"),
                {"item_id": _db_uuid(item_id)},
            )
    with pytest.raises(DBAPIError):
        with allocation_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO allocation_record_bindings (
                        item_id, workspace_id, plan_id,
                        argilla_dataset_id, argilla_record_id, bound_at
                    ) VALUES (
                        :item_id, :workspace_id, :plan_id,
                        :dataset_id, :record_id, :bound_at
                    )
                    """
                ),
                {
                    "item_id": _db_uuid(item_id),
                    "workspace_id": _db_uuid(ids["workspace"]),
                    "plan_id": _db_uuid(ids["plan"]),
                    "dataset_id": _db_uuid(ids["remote_direct_dataset"]),
                    "record_id": _db_uuid(injected_record_id),
                    "bound_at": datetime.now(timezone.utc),
                },
            )

    with allocation_engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE allocation_record_bindings
                SET argilla_dataset_id = :dataset_id,
                    argilla_record_id = :record_id,
                    bound_at = :bound_at
                WHERE item_id = :item_id
                """
            ),
            {
                "dataset_id": _db_uuid(ids["remote_direct_dataset"]),
                "record_id": _db_uuid(injected_record_id),
                "bound_at": datetime.now(timezone.utc),
                "item_id": _db_uuid(item_id),
            },
        )


def test_cohort_member_and_state_transition_identity_are_frozen(allocation_engine):
    ids = _build_graph(allocation_engine)
    unsealed = _build_unsealed_cohort_revision(allocation_engine, ids)

    with allocation_engine.begin() as connection:
        connection.execute(
            text("UPDATE annotator_cohort_members SET default_capacity = 11 WHERE id = :member_id"),
            {"member_id": _db_uuid(unsealed["member"])},
        )
    with allocation_engine.connect() as connection:
        assert connection.scalar(
            text("SELECT default_capacity FROM annotator_cohort_members WHERE id = :member_id"),
            {"member_id": _db_uuid(unsealed["member"])},
        ) == 11

    member_attacks = (
        (
            "UPDATE annotator_cohort_members SET cohort_revision_id = :value WHERE id = :member_id",
            _db_uuid(ids["cohort_revision"]),
        ),
        (
            "UPDATE annotator_cohort_members SET workspace_id = :value WHERE id = :member_id",
            _db_uuid(uuid.uuid4()),
        ),
        (
            "UPDATE annotator_cohort_members SET annotator_mapping_id = :value WHERE id = :member_id",
            _db_uuid(ids["mapping_b"]),
        ),
        (
            "UPDATE annotator_cohort_members SET created_at = :value WHERE id = :member_id",
            datetime(2000, 1, 1, tzinfo=timezone.utc),
        ),
    )
    for statement, value in member_attacks:
        with pytest.raises(DBAPIError):
            with allocation_engine.begin() as connection:
                connection.execute(
                    text(statement),
                    {"value": value, "member_id": _db_uuid(unsealed["member"])},
                )

    with pytest.raises(DBAPIError):
        with allocation_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE annotator_cohort_revisions
                    SET sealed_at = :sealed_at,
                        connection_binding_id = :binding_id,
                        created_at = :created_at
                    WHERE id = :revision_id
                    """
                ),
                {
                    "sealed_at": datetime.now(timezone.utc),
                    "binding_id": _db_uuid(ids["binding"]),
                    "created_at": datetime(2000, 1, 1, tzinfo=timezone.utc),
                    "revision_id": _db_uuid(unsealed["revision"]),
                },
            )
    with allocation_engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE annotator_cohort_revisions
                SET sealed_at = :sealed_at, connection_binding_id = :binding_id
                WHERE id = :revision_id
                """
            ),
            {
                "sealed_at": datetime.now(timezone.utc),
                "binding_id": _db_uuid(ids["binding"]),
                "revision_id": _db_uuid(unsealed["revision"]),
            },
        )

    with Session(allocation_engine) as session, session.begin():
        confirmation_id = _claim(
            session,
            ids["workspace"],
            ids["principal"],
            f"allocation.confirm.created-at.{uuid.uuid4()}",
        )
    with pytest.raises(DBAPIError):
        with allocation_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE allocation_plan_states
                    SET lifecycle_state = 'confirmed',
                        confirmed_at = :confirmed_at,
                        confirmed_by_principal_id = :principal_id,
                        confirmed_via_caller_principal_id = :principal_id,
                        confirmation_idempotency_record_id = :confirmation_id,
                        created_at = :created_at
                    WHERE plan_id = :plan_id
                    """
                ),
                {
                    "confirmed_at": datetime.now(timezone.utc),
                    "principal_id": _db_uuid(ids["principal"]),
                    "confirmation_id": _db_uuid(confirmation_id),
                    "created_at": datetime(2000, 1, 1, tzinfo=timezone.utc),
                    "plan_id": _db_uuid(ids["plan"]),
                },
            )
    _confirm_plan(allocation_engine, ids)


def test_cohort_seal_plan_state_and_confirmed_children_are_irreversible(allocation_engine):
    ids = _build_graph(allocation_engine)
    peer = _build_peer_draft_plan(allocation_engine, ids)
    with Session(allocation_engine) as session, session.begin():
        sealed_insert_claim = _claim(
            session,
            ids["workspace"],
            ids["principal"],
            f"cohort.sealed.insert.{uuid.uuid4()}",
        )
    with pytest.raises(DBAPIError):
        with allocation_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO annotator_cohort_revisions (
                        id, workspace_id, cohort_id, revision_number, fingerprint,
                        member_count, connection_binding_id, actor_principal_id,
                        caller_principal_id, channel, idempotency_record_id, sealed_at
                    ) VALUES (
                        :id, :workspace_id, :cohort_id, 2, :fingerprint,
                        1, :binding_id, :principal_id,
                        :principal_id, 'api', :idempotency_id, :sealed_at
                    )
                    """
                ),
                {
                    "id": _db_uuid(uuid.uuid4()),
                    "workspace_id": _db_uuid(ids["workspace"]),
                    "cohort_id": _db_uuid(ids["cohort"]),
                    "fingerprint": _hash(f"sealed-insert-{uuid.uuid4()}"),
                    "binding_id": _db_uuid(ids["binding"]),
                    "principal_id": _db_uuid(ids["principal"]),
                    "idempotency_id": _db_uuid(sealed_insert_claim),
                    "sealed_at": datetime.now(timezone.utc),
                },
            )
    with pytest.raises(DBAPIError):
        with allocation_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE annotator_cohort_members
                    SET default_capacity = default_capacity + 1
                    WHERE cohort_revision_id = :revision_id
                    """
                ),
                {"revision_id": _db_uuid(ids["cohort_revision"])},
            )
    with pytest.raises(DBAPIError):
        with allocation_engine.begin() as connection:
            connection.execute(
                text("DELETE FROM allocation_plan_states WHERE plan_id = :plan_id"),
                {"plan_id": _db_uuid(ids["plan"])},
            )

    _confirm_plan(allocation_engine, ids)
    with pytest.raises(DBAPIError):
        with allocation_engine.begin() as connection:
            connection.execute(
                text("UPDATE allocation_dataset_groups SET min_submitted = 1 WHERE id = :dataset_id"),
                {"dataset_id": _db_uuid(ids["pooled_dataset"])},
            )
    with pytest.raises(DBAPIError):
        with allocation_engine.begin() as connection:
            connection.execute(
                text("DELETE FROM allocation_plan_states WHERE plan_id = :plan_id"),
                {"plan_id": _db_uuid(ids["plan"])},
            )
    with pytest.raises(DBAPIError):
        with allocation_engine.begin() as connection:
            connection.execute(
                text("UPDATE allocation_plan_states SET lifecycle_state = 'draft' WHERE plan_id = :plan_id"),
                {"plan_id": _db_uuid(ids["plan"])},
            )
    with pytest.raises(DBAPIError):
        with allocation_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO allocation_assignment_items (
                        id, workspace_id, plan_id, dataset_group_id,
                        record_id, external_id, batch_id, content_hash
                    ) VALUES (
                        :id, :workspace_id, :plan_id, :dataset_group_id,
                        'late-record', 'late-record', 'default', :content_hash
                    )
                    """
                ),
                {
                    "id": _db_uuid(uuid.uuid4()),
                    "workspace_id": _db_uuid(ids["workspace"]),
                    "plan_id": _db_uuid(ids["plan"]),
                    "dataset_group_id": _db_uuid(ids["direct_dataset"]),
                    "content_hash": _hash("late-record"),
                },
            )
    with pytest.raises(DBAPIError):
        with allocation_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE allocation_assignments
                    SET plan_id = :draft_plan_id, item_id = :draft_item_id
                    WHERE id = :assignment_id
                    """
                ),
                {
                    "draft_plan_id": _db_uuid(peer["plan"]),
                    "draft_item_id": _db_uuid(peer["item"]),
                    "assignment_id": _db_uuid(ids["direct_assignment"]),
                },
            )
    with pytest.raises(DBAPIError):
        with allocation_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE allocation_record_bindings SET plan_id = :draft_plan_id "
                    "WHERE item_id = :item_id"
                ),
                {
                    "draft_plan_id": _db_uuid(peer["plan"]),
                    "item_id": _db_uuid(ids["direct_item"]),
                },
            )


def test_direct_and_pooled_assignment_contracts_enforce_binding_and_cohort_size(allocation_engine):
    ids = _build_graph(allocation_engine)
    with pytest.raises(DBAPIError):
        with allocation_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO allocation_assignments (
                        id, workspace_id, plan_id, planner_assignment_id, item_id,
                        phase, role, assignee_mapping_id, required_submissions
                    ) VALUES (
                        :id, :workspace_id, :plan_id, 'direct-on-pooled', :item_id,
                        'production', 'primary', :mapping_id, 1
                    )
                    """
                ),
                {
                    "id": _db_uuid(uuid.uuid4()),
                    "workspace_id": _db_uuid(ids["workspace"]),
                    "plan_id": _db_uuid(ids["plan"]),
                    "item_id": _db_uuid(ids["pooled_item"]),
                    "mapping_id": _db_uuid(ids["mapping_a"]),
                },
            )
    with pytest.raises(DBAPIError):
        with allocation_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO allocation_assignments (
                        id, workspace_id, plan_id, planner_assignment_id, item_id,
                        phase, role, pool_id, required_submissions
                    ) VALUES (
                        :id, :workspace_id, :plan_id, 'pooled-on-direct', :item_id,
                        'production', 'shared', :pool_id, 1
                    )
                    """
                ),
                {
                    "id": _db_uuid(uuid.uuid4()),
                    "workspace_id": _db_uuid(ids["workspace"]),
                    "plan_id": _db_uuid(ids["plan"]),
                    "item_id": _db_uuid(ids["direct_item"]),
                    "pool_id": _db_uuid(ids["cohort"]),
                },
            )
    with pytest.raises(DBAPIError):
        with allocation_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO allocation_assignments (
                        id, workspace_id, plan_id, planner_assignment_id, item_id,
                        phase, role, pool_id, required_submissions
                    ) VALUES (
                        :id, :workspace_id, :plan_id, 'too-many', :item_id,
                        'production', 'overlap', :pool_id, 3
                    )
                    """
                ),
                {
                    "id": _db_uuid(uuid.uuid4()),
                    "workspace_id": _db_uuid(ids["workspace"]),
                    "plan_id": _db_uuid(ids["plan"]),
                    "item_id": _db_uuid(ids["pooled_item"]),
                    "pool_id": _db_uuid(ids["cohort"]),
                },
            )
    with pytest.raises(DBAPIError):
        with allocation_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE allocation_assignments
                    SET assignee_mapping_id = NULL, pool_id = :wrong_pool
                    WHERE id = :assignment_id
                    """
                ),
                {
                    "wrong_pool": _db_uuid(uuid.uuid4()),
                    "assignment_id": _db_uuid(ids["direct_assignment"]),
                },
            )


def test_receipts_bind_remote_identity_validate_respondents_and_are_append_only(allocation_engine):
    ids = _build_graph(allocation_engine)
    _confirm_plan(allocation_engine, ids)
    with Session(allocation_engine) as session, session.begin():
        receipt = AllocationCollectionReceipt(
            workspace_id=ids["workspace"],
            plan_id=ids["plan"],
            item_id=ids["direct_item"],
            assignment_id=ids["direct_assignment"],
            connection_binding_id=ids["binding"],
            argilla_dataset_id=ids["remote_direct_dataset"],
            argilla_record_id=ids["remote_direct_record"],
            argilla_response_id=uuid.uuid4(),
            argilla_respondent_id=ids["remote_user_a"],
            response_status="submitted",
            disposition=CollectionDisposition.ACCEPTED,
            labels_hash=_hash("labels"),
            pulled_at=datetime.now(timezone.utc),
            actor_principal_id=ids["principal"],
            caller_principal_id=ids["principal"],
            channel=AuditChannel.WORKER,
            idempotency_record_id=_claim(
                session, ids["workspace"], ids["principal"], f"receipt.{uuid.uuid4()}"
            ),
        )
        session.add(receipt)
        session.flush()
        receipt_id = receipt.id

    with Session(allocation_engine) as session, session.begin():
        quarantine_claims = [
            _claim(
                session,
                ids["workspace"],
                ids["principal"],
                f"receipt.quarantine.{uuid.uuid4()}",
            )
            for _ in range(2)
        ]
    for rejection_reason, claim_id in ((None, quarantine_claims[0]), ("", quarantine_claims[1])):
        with pytest.raises(DBAPIError):
            with allocation_engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO allocation_collection_receipts (
                            id, workspace_id, plan_id, item_id, assignment_id,
                            connection_binding_id, argilla_dataset_id, argilla_record_id,
                            argilla_response_id, argilla_respondent_id, response_status,
                            disposition, rejection_reason, labels_hash, pulled_at,
                            actor_principal_id, caller_principal_id, channel,
                            idempotency_record_id
                        ) VALUES (
                            :id, :workspace_id, :plan_id, :item_id, NULL,
                            :binding_id, :dataset_id, :record_id,
                            :response_id, :respondent_id, 'submitted',
                            'quarantined', :rejection_reason, :labels_hash, :pulled_at,
                            :principal_id, :principal_id, 'worker', :idempotency_id
                        )
                        """
                    ),
                    {
                        "id": _db_uuid(uuid.uuid4()),
                        "workspace_id": _db_uuid(ids["workspace"]),
                        "plan_id": _db_uuid(ids["plan"]),
                        "item_id": _db_uuid(ids["direct_item"]),
                        "binding_id": _db_uuid(ids["binding"]),
                        "dataset_id": _db_uuid(ids["remote_direct_dataset"]),
                        "record_id": _db_uuid(ids["remote_direct_record"]),
                        "response_id": _db_uuid(uuid.uuid4()),
                        "respondent_id": _db_uuid(ids["remote_user_a"]),
                        "rejection_reason": rejection_reason,
                        "labels_hash": _hash("quarantine-labels"),
                        "pulled_at": datetime.now(timezone.utc),
                        "principal_id": _db_uuid(ids["principal"]),
                        "idempotency_id": _db_uuid(claim_id),
                    },
                )

    with pytest.raises(DBAPIError):
        with allocation_engine.begin() as connection:
            connection.execute(
                text("UPDATE allocation_collection_receipts SET response_status = 'changed' WHERE id = :id"),
                {"id": _db_uuid(receipt_id)},
            )
    with pytest.raises(DBAPIError):
        with allocation_engine.begin() as connection:
            connection.execute(
                text("DELETE FROM allocation_collection_receipts WHERE id = :id"),
                {"id": _db_uuid(receipt_id)},
            )

    with Session(allocation_engine) as session, session.begin():
        bad_receipt = AllocationCollectionReceipt(
            workspace_id=ids["workspace"],
            plan_id=ids["plan"],
            item_id=ids["pooled_item"],
            assignment_id=ids["pooled_assignment"],
            connection_binding_id=ids["binding"],
            argilla_dataset_id=ids["remote_pooled_dataset"],
            argilla_record_id=ids["remote_pooled_record"],
            argilla_response_id=uuid.uuid4(),
            argilla_respondent_id=uuid.uuid4(),
            response_status="submitted",
            disposition=CollectionDisposition.ACCEPTED,
            labels_hash=_hash("bad-labels"),
            pulled_at=datetime.now(timezone.utc),
            actor_principal_id=ids["principal"],
            caller_principal_id=ids["principal"],
            channel=AuditChannel.WORKER,
            idempotency_record_id=_claim(
                session, ids["workspace"], ids["principal"], f"receipt.bad.{uuid.uuid4()}"
            ),
        )
        session.add(bad_receipt)
        with pytest.raises(DBAPIError):
            session.flush()


def test_annotation_job_is_scoped_immutable_and_stateful(allocation_engine):
    ids = _build_graph(allocation_engine)
    _confirm_plan(allocation_engine, ids)
    with Session(allocation_engine) as session, session.begin():
        plan = session.get(AllocationPlan, ids["plan"])
        claim_id = _annotation_claim(
            session,
            ids["workspace"],
            ids["task"],
            ids["principal"],
        )
        job = AnnotationJob(
            workspace_id=ids["workspace"],
            task_id=ids["task"],
            task_revision_id=ids["task_revision"],
            source_manifest_id=plan.source_manifest_id,
            source_manifest_hash=plan.source_manifest_hash,
            connection_binding_id=ids["binding"],
            cohort_revision_id=ids["cohort_revision"],
            allocation_plan_id=ids["plan"],
            logical_job_id=f"annotation-job-{uuid.uuid4().hex[:10]}",
            dataset_name=f"annotation-dataset-{uuid.uuid4().hex[:10]}",
            created_by_principal_id=ids["principal"],
            idempotency_record_id=claim_id,
        )
        session.add(job)
        session.flush()
        job_id = job.id
        assert job.lifecycle_state.value == "draft"
        assert job.dispatch_state.value == "pending"
        assert job.collection_state.value == "pending"

        job.lifecycle_state = "ready"
        session.flush()
        job.lifecycle_state = "dispatching"
        job.dispatch_state = "running"
        session.flush()
        job.lifecycle_state = "dispatched"
        job.dispatch_state = "succeeded"
        job.remote_dataset_id = uuid.uuid4()
        session.flush()
        job.lifecycle_state = "collecting"
        job.collection_state = "running"
        session.flush()
        job.lifecycle_state = "completed"
        job.collection_state = "succeeded"
        session.flush()

    with pytest.raises(DBAPIError):
        with allocation_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE annotation_jobs SET source_manifest_hash = :value WHERE id = :job_id"
                ),
                {"value": _hash("replacement"), "job_id": _db_uuid(job_id)},
            )

        with pytest.raises(DBAPIError):
            with allocation_engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM annotation_jobs WHERE id = :job_id"),
                    {"job_id": _db_uuid(job_id)},
                )


def test_annotation_job_requires_confirmed_allocation_plan(allocation_engine):
    ids = _build_graph(allocation_engine)
    with Session(allocation_engine) as session, session.begin():
        plan = session.get(AllocationPlan, ids["plan"])
        job = AnnotationJob(
            workspace_id=ids["workspace"],
            task_id=ids["task"],
            task_revision_id=ids["task_revision"],
            source_manifest_id=plan.source_manifest_id,
            source_manifest_hash=plan.source_manifest_hash,
            connection_binding_id=ids["binding"],
            cohort_revision_id=ids["cohort_revision"],
            allocation_plan_id=ids["plan"],
            logical_job_id=f"unconfirmed-annotation-job-{uuid.uuid4().hex[:10]}",
            dataset_name=f"unconfirmed-annotation-dataset-{uuid.uuid4().hex[:10]}",
            created_by_principal_id=ids["principal"],
            idempotency_record_id=_annotation_claim(
                session,
                ids["workspace"],
                ids["task"],
                ids["principal"],
            ),
        )
        session.add(job)
        with pytest.raises(DBAPIError):
            session.flush()

    _confirm_plan(allocation_engine, ids)
    with Session(allocation_engine) as session, session.begin():
        plan = session.get(AllocationPlan, ids["plan"])
        job = AnnotationJob(
            workspace_id=ids["workspace"],
            task_id=ids["task"],
            task_revision_id=ids["task_revision"],
            source_manifest_id=plan.source_manifest_id,
            source_manifest_hash=plan.source_manifest_hash,
            connection_binding_id=ids["binding"],
            cohort_revision_id=ids["cohort_revision"],
            allocation_plan_id=ids["plan"],
            logical_job_id=f"confirmed-annotation-job-{uuid.uuid4().hex[:10]}",
            dataset_name=f"confirmed-annotation-dataset-{uuid.uuid4().hex[:10]}",
            created_by_principal_id=ids["principal"],
            idempotency_record_id=_annotation_claim(
                session,
                ids["workspace"],
                ids["task"],
                ids["principal"],
            ),
        )
        session.add(job)
        session.flush()


def test_allocation_migration_upgrade_downgrade_round_trip(tmp_path: Path):
    database_url = f"sqlite+pysqlite:///{tmp_path / 'allocation-round-trip.db'}"
    config = build_alembic_config(database_url)
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    try:
        assert ALLOCATION_TABLES <= set(inspect(engine).get_table_names())
        with Session(engine) as session:
            assert session.scalar(select(text("version_num")).select_from(text("alembic_version"))) == (
                "20260721_0008"
            )
    finally:
        engine.dispose()

    command.downgrade(config, "20260714_0002")
    engine = create_database_engine(database_url)
    try:
        assert ALLOCATION_TABLES.isdisjoint(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    command.upgrade(config, "head")
