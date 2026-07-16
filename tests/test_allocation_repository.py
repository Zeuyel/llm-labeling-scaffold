from __future__ import annotations

import hashlib
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier

import pytest
from alembic import command
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from llm_labeling_scaffold.allocation import (
    AllocationRequest,
    AllocationStrategy,
    AnnotatorSpec,
    OverlapRule,
    RecordSpec,
    SourceManifestRef,
    TaskRevisionRef,
    ManifestKind,
)
from llm_labeling_scaffold.db import (
    AllocationReceiptRequest,
    AuditChannel,
    CollectionDisposition,
    DatabaseService,
    ExternalIdentity,
    AllocationRepositoryValidationError,
    ReceiptRejectionReason,
    TrustedAllocationRepository,
)
from llm_labeling_scaffold.db.base import Base
from llm_labeling_scaffold.db.bootstrap import bootstrap_admin
from llm_labeling_scaffold.db.database import create_database_engine
from llm_labeling_scaffold.db.migration import build_alembic_config
from llm_labeling_scaffold.db.enums import (
    AllocationDatasetState,
    AnnotatorMappingState,
    AnnotatorVerificationState,
    ArgillaBindingState,
    PrincipalType,
)
from llm_labeling_scaffold.db.models import (
    AllocationAssignment,
    AllocationAssignmentItem,
    AllocationDatasetGroupState,
    AllocationPlan,
    AllocationRecordBinding,
    AllocationWorkspaceGroup,
    AnnotatorCohort,
    AnnotatorCohortMember,
    AnnotatorCohortRevision,
    ArgillaAnnotatorMapping,
    ArgillaConnectionBinding,
    IdempotencyRecord,
    Principal,
    RoleBinding,
    Task,
    TaskRevision,
)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@pytest.fixture(params=("sqlite", "postgres"))
def repository(tmp_path: Path, request):
    suffix = uuid.uuid4().hex[:10]
    if request.param == "postgres":
        database_url = os.environ.get("LLS_TEST_POSTGRES_URL")
        if not database_url:
            pytest.skip("LLS_TEST_POSTGRES_URL is not set")
        command.upgrade(build_alembic_config(database_url), "head")
        engine = create_database_engine(database_url)
    else:
        engine = create_database_engine(
            f"sqlite+pysqlite:///{tmp_path / 'allocation-repository.db'}",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    workspace_slug = f"workspace-{suffix}"
    task_key = f"trusted-task-{suffix}"
    identity = ExternalIdentity("https://allocation.example", f"admin-{suffix}")
    with Session(engine) as session:
        bootstrap = bootstrap_admin(
            session,
            issuer=identity.issuer,
            subject=identity.subject,
            workspace_slug=workspace_slug,
            workspace_name="Allocation Workspace",
        )
    remote_workspace = uuid.uuid4()
    remote_users = (uuid.uuid4(), uuid.uuid4())
    mapping_ids: list[uuid.UUID] = []
    with Session(engine) as session, session.begin():
        task = Task(
            workspace_id=bootstrap.workspace_id,
            task_key=task_key,
            name="Trusted Allocation Task",
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
            rendered_task=f"task_id: {task_key}\n",
            reason="repository test",
            actor_principal_id=bootstrap.principal_id,
            caller_principal_id=bootstrap.principal_id,
            channel=AuditChannel.API,
            content_hash=_hash("task-revision"),
            idempotency_record_id=_claim(session, bootstrap.workspace_id, bootstrap.principal_id),
        )
        session.add(task_revision)
        session.flush()
        task.current_revision_id = task_revision.id
        binding = ArgillaConnectionBinding(
            workspace_id=bootstrap.workspace_id,
            connection_config_id=f"argilla-test-{suffix}",
            server_version="2.0",
            argilla_workspace_id=remote_workspace,
            argilla_workspace_name_snapshot="remote-workspace",
            state=ArgillaBindingState.ACTIVE,
            idempotency_record_id=_claim(session, bootstrap.workspace_id, bootstrap.principal_id),
        )
        session.add(binding)
        session.flush()
        for index, remote_user in enumerate(remote_users):
            mapping = ArgillaAnnotatorMapping(
                workspace_id=bootstrap.workspace_id,
                connection_binding_id=binding.id,
                argilla_user_id=remote_user,
                username_snapshot=f"annotator-{index}",
                personal_argilla_workspace_id=uuid.uuid4(),
                state=AnnotatorMappingState.ACTIVE,
                verification_state=AnnotatorVerificationState.VERIFIED,
                verified_by_principal_id=bootstrap.principal_id,
                verified_at=datetime.now(timezone.utc),
                idempotency_record_id=_claim(session, bootstrap.workspace_id, bootstrap.principal_id),
            )
            session.add(mapping)
            session.flush()
            mapping_ids.append(mapping.id)
        cohort = AnnotatorCohort(
            workspace_id=bootstrap.workspace_id,
            name=f"trusted-cohort-{suffix}",
            idempotency_record_id=_claim(session, bootstrap.workspace_id, bootstrap.principal_id),
        )
        session.add(cohort)
        session.flush()
        cohort_revision = AnnotatorCohortRevision(
            workspace_id=bootstrap.workspace_id,
            cohort_id=cohort.id,
            revision_number=1,
            fingerprint=_hash("cohort-revision"),
            member_count=2,
            actor_principal_id=bootstrap.principal_id,
            caller_principal_id=bootstrap.principal_id,
            channel=AuditChannel.API,
            idempotency_record_id=_claim(session, bootstrap.workspace_id, bootstrap.principal_id),
        )
        session.add(cohort_revision)
        session.flush()
        session.add_all(
            [
                AnnotatorCohortMember(
                    workspace_id=bootstrap.workspace_id,
                    cohort_revision_id=cohort_revision.id,
                    annotator_mapping_id=mapping_id,
                    default_capacity=10,
                )
                for mapping_id in mapping_ids
            ]
        )
        session.flush()
        cohort_revision.connection_binding_id = binding.id
        cohort_revision.sealed_at = datetime.now(timezone.utc)
        session.flush()
        fixture_task_id = task.id
        fixture_task_key = task.task_key
        fixture_task_revision_id = task_revision.id
        fixture_task_revision_hash = task_revision.content_hash
        fixture_binding_id = binding.id
        fixture_cohort_id = cohort.id
        fixture_cohort_revision_id = cohort_revision.id

    service = DatabaseService(factory)
    repo = TrustedAllocationRepository(factory, engine)
    try:
        yield {
            "repo": repo,
            "engine": engine,
            "factory": factory,
            "dialect": request.param,
            "identity": identity,
            "workspace_slug": workspace_slug,
            "workspace_id": bootstrap.workspace_id,
            "task_id": fixture_task_id,
            "task_key": fixture_task_key,
            "task_revision_id": fixture_task_revision_id,
            "task_revision_hash": fixture_task_revision_hash,
            "binding_id": fixture_binding_id,
            "cohort_id": fixture_cohort_id,
            "cohort_revision_id": fixture_cohort_revision_id,
            "mapping_ids": tuple(mapping_ids),
            "remote_workspace": remote_workspace,
            "remote_users": remote_users,
            "service": service,
        }
    finally:
        repo.close()


def _claim(session: Session, workspace_id: uuid.UUID, principal_id: uuid.UUID) -> uuid.UUID:
    record = IdempotencyRecord(
        workspace_id=workspace_id,
        actor_principal_id=principal_id,
        caller_principal_id=principal_id,
        operation=f"fixture.{uuid.uuid4()}",
        idempotency_key_hash=_hash(str(uuid.uuid4())),
        request_fingerprint=_hash(str(uuid.uuid4())),
        required_permission="task:create",
        resource_type="workspace",
        resource_id=workspace_id,
        channel=AuditChannel.API,
    )
    session.add(record)
    session.flush()
    return record.id


def _request(data: dict) -> AllocationRequest:
    return AllocationRequest(
        strategy=AllocationStrategy.SHARED_QUEUE,
        task_revision=TaskRevisionRef(
            task_id=data["task_key"],
            revision_id=str(data["task_revision_id"]),
            revision_hash=data["task_revision_hash"],
        ),
        source_manifest=SourceManifestRef(
            manifest_id="manifest-v1",
            manifest_hash=_hash("manifest"),
            kind=ManifestKind.SAMPLE,
        ),
        records=(
            RecordSpec("r-overlap", _hash("r-overlap"), "batch-1"),
            RecordSpec("r-primary", _hash("r-primary"), "batch-1"),
        ),
        annotators=tuple(
            AnnotatorSpec(str(mapping_id), str(data["cohort_id"]), 10)
            for mapping_id in data["mapping_ids"]
        ),
        seed=7,
        algorithm_version="allocation-v1",
        overlap_rules=(OverlapRule(record_ids=("r-overlap",), required_submissions=2),),
    )


def _bind_plan(data: dict, plan_id: uuid.UUID) -> None:
    with Session(data["engine"]) as session, session.begin():
        groups = session.scalars(
            select(AllocationWorkspaceGroup).where(AllocationWorkspaceGroup.plan_id == plan_id)
        ).all()
        assert len(groups) == 1
        datasets = session.scalars(
            select(AllocationDatasetGroupState).where(AllocationDatasetGroupState.plan_id == plan_id)
        ).all()
        items = session.scalars(
            select(AllocationAssignmentItem).where(AllocationAssignmentItem.plan_id == plan_id)
        ).all()
        dataset_ids: dict[uuid.UUID, uuid.UUID] = {}
        for dataset in datasets:
            dataset.argilla_workspace_id = data["remote_workspace"]
            dataset.argilla_dataset_id = uuid.uuid4()
            dataset.materialization_state = AllocationDatasetState.READY
            dataset.materialized_at = datetime.now(timezone.utc)
            dataset_ids[dataset.dataset_group_id] = dataset.argilla_dataset_id
        for item in items:
            binding = session.get(AllocationRecordBinding, item.id)
            binding.argilla_dataset_id = dataset_ids[item.dataset_group_id]
            binding.argilla_record_id = uuid.uuid4()
            binding.bound_at = datetime.now(timezone.utc)


def test_repository_rebuilds_and_idempotently_persists_planner_output(repository):
    data = repository
    request = _request(data)
    preview = data["repo"].preview(
        request,
        actor_identity=data["identity"],
        caller_identity=data["identity"],
        workspace_slug=data["workspace_slug"],
        task_key=data["task_key"],
        cohort_revision_id=data["cohort_revision_id"],
    )
    assert preview.ready
    first = data["repo"].create(
        request,
        actor_identity=data["identity"],
        caller_identity=data["identity"],
        workspace_slug=data["workspace_slug"],
        task_key=data["task_key"],
        cohort_revision_id=data["cohort_revision_id"],
        idempotency_key="plan-create-1",
        channel=AuditChannel.API,
    )
    replay = data["repo"].create(
        request,
        actor_identity=data["identity"],
        caller_identity=data["identity"],
        workspace_slug=data["workspace_slug"],
        task_key=data["task_key"],
        cohort_revision_id=data["cohort_revision_id"],
        idempotency_key="plan-create-1",
        channel=AuditChannel.API,
    )
    assert first.plan_id == replay.plan_id
    assert replay.replayed
    with Session(data["engine"]) as session:
        assert session.scalar(select(func.count()).select_from(AllocationPlan)) == 1


def test_repository_confirm_and_receipt_progress_are_server_derived(repository):
    data = repository
    request = _request(data)
    created = data["repo"].create(
        request,
        actor_identity=data["identity"],
        caller_identity=data["identity"],
        workspace_slug=data["workspace_slug"],
        task_key=data["task_key"],
        cohort_revision_id=data["cohort_revision_id"],
        idempotency_key="plan-create-2",
        channel=AuditChannel.API,
    )
    _bind_plan(data, created.plan_id)
    confirmed = data["repo"].confirm(
        created.plan_id,
        plan_fingerprint=created.fingerprint,
        actor_identity=data["identity"],
        caller_identity=data["identity"],
        workspace_slug=data["workspace_slug"],
        idempotency_key="plan-confirm-1",
        channel=AuditChannel.API,
    )
    assert confirmed.lifecycle_state.value == "confirmed"
    with Session(data["engine"]) as session:
        assignment = session.scalar(
            select(AllocationAssignment)
            .join(AllocationAssignmentItem, AllocationAssignmentItem.id == AllocationAssignment.item_id)
            .where(
                AllocationAssignment.plan_id == created.plan_id,
                AllocationAssignmentItem.record_id == "r-overlap",
            )
        )
        item = session.get(AllocationAssignmentItem, assignment.item_id)
        binding = session.get(AllocationRecordBinding, item.id)
    receipt_request = AllocationReceiptRequest(
        plan_id=created.plan_id,
        item_id=item.id,
        assignment_id=assignment.id,
        connection_binding_id=data["binding_id"],
        argilla_dataset_id=binding.argilla_dataset_id,
        argilla_record_id=binding.argilla_record_id,
        argilla_response_id=uuid.uuid4(),
        argilla_respondent_id=data["remote_users"][0],
        response_status="submitted",
        disposition=CollectionDisposition.ACCEPTED,
        labels_hash=_hash("labels-1"),
        pulled_at=datetime.now(timezone.utc),
    )
    result = data["repo"].record_receipt(
        receipt_request,
        actor_identity=data["identity"],
        caller_identity=data["identity"],
        workspace_slug=data["workspace_slug"],
        idempotency_key="receipt-1",
        channel=AuditChannel.WORKER,
    )
    replay = data["repo"].record_receipt(
        receipt_request,
        actor_identity=data["identity"],
        caller_identity=data["identity"],
        workspace_slug=data["workspace_slug"],
        idempotency_key="receipt-1",
        channel=AuditChannel.WORKER,
    )
    assert result.receipt_id == replay.receipt_id
    assert replay.replayed
    assert result.progress.accepted_submissions == 1
    assert result.progress.required_submissions == 3
    assert not result.progress.completed

    with Session(data["engine"]) as session:
        primary_item = session.scalar(
            select(AllocationAssignmentItem).where(
                AllocationAssignmentItem.plan_id == created.plan_id,
                AllocationAssignmentItem.record_id == "r-primary",
            )
        )
        primary_binding = session.get(AllocationRecordBinding, primary_item.id)
    quarantined = AllocationReceiptRequest(
        plan_id=created.plan_id,
        item_id=primary_item.id,
        connection_binding_id=data["binding_id"],
        argilla_dataset_id=primary_binding.argilla_dataset_id,
        argilla_record_id=primary_binding.argilla_record_id,
        argilla_response_id=uuid.uuid4(),
        argilla_respondent_id=uuid.uuid4(),
        response_status="rejected",
        disposition=CollectionDisposition.QUARANTINED,
        labels_hash=_hash("labels-quarantined"),
        pulled_at=datetime.now(timezone.utc),
        rejection_reason=ReceiptRejectionReason.MANUAL_REVIEW,
    )
    quarantined_result = data["repo"].record_receipt(
        quarantined,
        actor_identity=data["identity"],
        caller_identity=data["identity"],
        workspace_slug=data["workspace_slug"],
        idempotency_key="receipt-quarantined-1",
        channel=AuditChannel.WORKER,
    )
    assert quarantined_result.progress.accepted_submissions == 1
    assert quarantined_result.progress.required_submissions == 3


def test_repository_rejects_historical_revision_for_new_plan(repository):
    data = repository
    with Session(data["engine"]) as session, session.begin():
        current_revision = TaskRevision(
            workspace_id=data["workspace_id"],
            task_id=data["task_id"],
            revision_number=2,
            draft_version=2,
            draft_fingerprint=_hash("draft-current"),
            definition={"task_id": data["task_key"], "revision": 2},
            rendered_task="task_id: trusted-task\nrevision: 2\n",
            reason="current revision fixture",
            actor_principal_id=session.scalar(
                select(Principal.id).where(Principal.issuer == data["identity"].issuer)
            ),
            caller_principal_id=session.scalar(
                select(Principal.id).where(Principal.issuer == data["identity"].issuer)
            ),
            channel=AuditChannel.API,
            content_hash=_hash("current-task-revision"),
            idempotency_record_id=_claim(
                session,
                data["workspace_id"],
                session.scalar(select(Principal.id).where(Principal.issuer == data["identity"].issuer)),
            ),
        )
        session.add(current_revision)
        session.flush()
        task = session.get(Task, data["task_id"])
        task.current_revision_id = current_revision.id
    with pytest.raises(AllocationRepositoryValidationError, match="current revision"):
        data["repo"].create(
            _request(data),
            actor_identity=data["identity"],
            caller_identity=data["identity"],
            workspace_slug=data["workspace_slug"],
            task_key=data["task_key"],
            cohort_revision_id=data["cohort_revision_id"],
            idempotency_key="historical-plan-1",
            channel=AuditChannel.API,
        )


def test_postgres_repository_concurrent_create_replays_one_plan(repository):
    if repository["dialect"] != "postgres":
        pytest.skip("PostgreSQL concurrency coverage")
    data = repository
    request = _request(data)
    barrier = Barrier(2)

    def create_once():
        repo = TrustedAllocationRepository(data["factory"])
        barrier.wait(timeout=10)
        return repo.create(
            request,
            actor_identity=data["identity"],
            caller_identity=data["identity"],
            workspace_slug=data["workspace_slug"],
            task_key=data["task_key"],
            cohort_revision_id=data["cohort_revision_id"],
            idempotency_key="concurrent-plan-create",
            channel=AuditChannel.API,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: create_once(), range(2)))
    assert len({result.plan_id for result in results}) == 1
    assert sum(result.replayed for result in results) == 1
    with Session(data["engine"]) as session:
        assert session.scalar(select(func.count()).select_from(AllocationPlan)) == 1
