from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from llm_labeling_scaffold.db import (
    AnnotatorControlRepository,
    AnnotatorNotReady,
    AnnotatorResourceNotFound,
    AnnotatorRepositoryValidationError,
    AnnotatorVerificationRejected,
    AuditChannel,
    CohortMemberInput,
    ExternalIdentity,
    Permission,
    PrincipalType,
    Role,
)
from llm_labeling_scaffold.db.bootstrap import bootstrap_admin
from llm_labeling_scaffold.db.database import create_database_engine
from llm_labeling_scaffold.db.migration import upgrade_database
from llm_labeling_scaffold.db.models import (
    AuditEvent,
    AnnotatorCohort,
    AnnotatorCohortMember,
    IdempotencyRecord,
    Principal,
    RoleBinding,
    Workspace,
)
from llm_labeling_scaffold.db.service import DatabaseTransaction


@pytest.fixture
def annotator_repository(tmp_path: Path):
    database_url = f"sqlite+pysqlite:///{tmp_path / 'annotator.db'}"
    upgrade_database(database_url)
    engine = create_database_engine(database_url)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    admin_a = ExternalIdentity("https://annotator.test", "admin-a")
    admin_b = ExternalIdentity("https://annotator.test", "admin-b")
    with Session(engine) as session:
        bootstrap_admin(
            session,
            issuer=admin_a.issuer,
            subject=admin_a.subject,
            workspace_slug="workspace-a",
            workspace_name="Workspace A",
        )
    with Session(engine) as session:
        bootstrap_admin(
            session,
            issuer=admin_b.issuer,
            subject=admin_b.subject,
            workspace_slug="workspace-b",
            workspace_name="Workspace B",
        )
    with Session(engine) as session, session.begin():
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "workspace-a"))
        admin = session.scalar(
            select(Principal).where(
                Principal.issuer == admin_a.issuer,
                Principal.subject == admin_a.subject,
            )
        )
        assert workspace is not None and admin is not None
        for subject in ("annotator-owner", "annotator-a", "verified", "unverified", "scoped", "race"):
            principal = Principal(
                issuer=admin_a.issuer,
                subject=subject,
                principal_type=PrincipalType.USER,
            )
            session.add(principal)
            session.flush()
            session.add(
                RoleBinding(
                    workspace_id=workspace.id,
                    principal_id=principal.id,
                    role=Role.ANNOTATOR,
                    created_by_principal_id=admin.id,
                )
            )
    repository = AnnotatorControlRepository(factory, engine)
    try:
        yield {
            "repository": repository,
            "engine": engine,
            "admin_a": admin_a,
            "admin_b": admin_b,
        }
    finally:
        repository.close()


def test_binding_mapping_verification_and_sensitive_boundary(annotator_repository):
    repository = annotator_repository["repository"]
    admin = annotator_repository["admin_a"]
    remote_workspace_id = uuid.uuid4()
    remote_user_id = uuid.uuid4()
    personal_workspace_id = uuid.uuid4()
    secret = "one-time-password-never-persisted"

    binding = repository.ensure_workspace_binding(
        workspace_slug="workspace-a",
        connection_config_id="argilla-main",
        server_version="2.8.0",
        argilla_workspace_id=remote_workspace_id,
        argilla_workspace_name="workspace-a-remote",
        actor_identity=admin,
        idempotency_key="binding-create",
        channel=AuditChannel.API,
    )
    replay = repository.ensure_workspace_binding(
        workspace_slug="workspace-a",
        connection_config_id="argilla-main",
        server_version="2.8.0",
        argilla_workspace_id=remote_workspace_id,
        argilla_workspace_name="workspace-a-remote",
        actor_identity=admin,
        idempotency_key="binding-create",
        channel=AuditChannel.API,
    )
    assert replay.replayed is True
    assert replay.binding.id == binding.binding.id

    with pytest.raises(AnnotatorRepositoryValidationError):
        repository.create_annotator_mapping(
            workspace_slug="workspace-a",
            binding_id=binding.binding.id,
            principal_identity=ExternalIdentity("https://annotator.test", "annotator-owner"),
            username="annotator-owner",
            argilla_user_id=uuid.uuid4(),
            personal_argilla_workspace_id=uuid.uuid4(),
            argilla_role="owner",
            actor_identity=admin,
            idempotency_key="owner-mapping",
        )

    mapping = repository.create_annotator_mapping(
        workspace_slug="workspace-a",
        binding_id=binding.binding.id,
        principal_identity=ExternalIdentity("https://annotator.test", "annotator-a"),
        username="annotator-a",
        argilla_user_id=remote_user_id,
        personal_argilla_workspace_id=personal_workspace_id,
        membership_verified=False,
        initial_password=secret,
        actor_identity=admin,
        idempotency_key="mapping-create",
        channel=AuditChannel.API,
    )
    assert mapping.mapping.verification_state.value == "unverified"
    assert secret not in repr(mapping)

    with pytest.raises(AnnotatorVerificationRejected) as rejected:
        repository.verify_annotator(
            workspace_slug="workspace-a",
            mapping_id=mapping.mapping.id,
            argilla_user_id=uuid.uuid4(),
            username="annotator-a",
            personal_argilla_workspace_id=personal_workspace_id,
            membership_verified=True,
            actor_identity=admin,
            idempotency_key="mapping-verify-drift",
        )
    assert rejected.value.reason == "argilla_user_id_mismatch"
    rejected_mapping = repository.get_annotator(
        workspace_slug="workspace-a",
        identity=admin,
        mapping_id=mapping.mapping.id,
    )
    assert rejected_mapping is not None
    assert rejected_mapping.verification_state.value == "rejected"
    assert rejected_mapping.verified_at is None

    verified = repository.verify_annotator(
        workspace_slug="workspace-a",
        mapping_id=mapping.mapping.id,
        argilla_user_id=remote_user_id,
        username="annotator-a",
        personal_argilla_workspace_id=personal_workspace_id,
        membership_verified=True,
        actor_identity=admin,
        idempotency_key="mapping-verify",
    )
    assert verified.verified is True
    assert verified.mapping.verification_state.value == "verified"
    assert verified.mapping.recent_verification_at is not None

    with Session(annotator_repository["engine"]) as session:
        audit_values = session.scalars(
            select(AuditEvent.details).where(AuditEvent.workspace_id == binding.binding.workspace_id),
        ).all()
        response_values = session.scalars(
            select(IdempotencyRecord.response_body).where(
                IdempotencyRecord.workspace_id == binding.binding.workspace_id,
            ),
        ).all()
    serialized = json.dumps([*audit_values, *response_values], sort_keys=True)
    assert secret not in serialized
    assert "initial_password" not in serialized


def test_cohort_requires_verified_mapping_and_replacement_creates_revision(annotator_repository):
    repository = annotator_repository["repository"]
    admin = annotator_repository["admin_a"]
    binding = repository.ensure_workspace_binding(
        workspace_slug="workspace-a",
        connection_config_id="argilla-main",
        server_version="2.8.0",
        argilla_workspace_id=uuid.uuid4(),
        argilla_workspace_name="workspace-a-remote",
        actor_identity=admin,
        idempotency_key="binding",
    )
    verified_mapping = repository.create_annotator_mapping(
        workspace_slug="workspace-a",
        binding_id=binding.binding.id,
        principal_identity=ExternalIdentity("https://annotator.test", "verified"),
        username="verified",
        argilla_user_id=uuid.uuid4(),
        personal_argilla_workspace_id=uuid.uuid4(),
        membership_verified=True,
        actor_identity=admin,
        idempotency_key="mapping-verified",
    )
    unverified_mapping = repository.create_annotator_mapping(
        workspace_slug="workspace-a",
        binding_id=binding.binding.id,
        principal_identity=ExternalIdentity("https://annotator.test", "unverified"),
        username="unverified",
        argilla_user_id=uuid.uuid4(),
        personal_argilla_workspace_id=uuid.uuid4(),
        membership_verified=False,
        actor_identity=admin,
        idempotency_key="mapping-unverified",
    )

    with pytest.raises(AnnotatorNotReady):
        repository.create_cohort(
            workspace_slug="workspace-a",
            name="verified-only",
            binding_id=binding.binding.id,
            members=(
                CohortMemberInput(unverified_mapping.mapping.id, 10),
            ),
            actor_identity=admin,
            idempotency_key="cohort-unverified",
        )

    first = repository.create_cohort(
        workspace_slug="workspace-a",
        name="verified-only",
        binding_id=binding.binding.id,
        members=(CohortMemberInput(verified_mapping.mapping.id, 10),),
        actor_identity=admin,
        idempotency_key="cohort-create",
    )
    assert first.revision.immutable is True
    assert first.revision.revision_number == 1
    assert first.revision.members[0].default_capacity == 10

    second = repository.replace_cohort_members(
        workspace_slug="workspace-a",
        cohort_id=first.cohort.id,
        binding_id=binding.binding.id,
        members=(CohortMemberInput(verified_mapping.mapping.id, 25),),
        actor_identity=admin,
        idempotency_key="cohort-replace",
    )
    assert second.revision.revision_number == 2
    assert second.revision.id != first.revision.id
    assert second.revision.members[0].default_capacity == 25

    with pytest.raises(DBAPIError):
        with Session(annotator_repository["engine"]) as session, session.begin():
            member = session.scalar(
                select(AnnotatorCohortMember).where(
                    AnnotatorCohortMember.workspace_id == first.revision.workspace_id,
                    AnnotatorCohortMember.cohort_revision_id == first.revision.id,
                ),
            )
            assert member is not None
            member.default_capacity = 99
            session.flush()

    revisions = repository.list_cohort_revisions(
        workspace_slug="workspace-a",
        identity=admin,
        cohort_id=first.cohort.id,
    )
    assert [(revision.revision_number, revision.members[0].default_capacity) for revision in revisions] == [
        (1, 10),
        (2, 25),
    ]


def test_workspace_scope_blocks_cross_workspace_reads_and_writes(annotator_repository):
    repository = annotator_repository["repository"]
    admin_a = annotator_repository["admin_a"]
    admin_b = annotator_repository["admin_b"]
    binding = repository.ensure_workspace_binding(
        workspace_slug="workspace-a",
        connection_config_id="argilla-main",
        server_version="2.8.0",
        argilla_workspace_id=uuid.uuid4(),
        argilla_workspace_name="workspace-a-remote",
        actor_identity=admin_a,
        idempotency_key="binding",
    )
    mapping = repository.create_annotator_mapping(
        workspace_slug="workspace-a",
        binding_id=binding.binding.id,
        principal_identity=ExternalIdentity("https://annotator.test", "scoped"),
        username="scoped",
        argilla_user_id=uuid.uuid4(),
        personal_argilla_workspace_id=uuid.uuid4(),
        membership_verified=True,
        actor_identity=admin_a,
        idempotency_key="mapping",
    )
    cohort = repository.create_cohort(
        workspace_slug="workspace-a",
        name="scoped",
        binding_id=binding.binding.id,
        members=(CohortMemberInput(mapping.mapping.id, 5),),
        actor_identity=admin_a,
        idempotency_key="cohort",
    )

    assert repository.get_annotator(
        workspace_slug="workspace-b",
        identity=admin_b,
        mapping_id=mapping.mapping.id,
    ) is None
    assert repository.get_cohort_revision(
        workspace_slug="workspace-b",
        identity=admin_b,
        revision_id=cohort.revision.id,
    ) is None
    with pytest.raises(AnnotatorResourceNotFound):
        repository.create_cohort_revision(
            workspace_slug="workspace-b",
            cohort_id=cohort.cohort.id,
            binding_id=binding.binding.id,
            members=(CohortMemberInput(mapping.mapping.id, 99),),
            actor_identity=admin_b,
            idempotency_key="cross-workspace-revision",
        )


def test_duplicate_fingerprint_savepoint_does_not_poison_outer_transaction(
    annotator_repository,
    monkeypatch,
):
    repository = annotator_repository["repository"]
    engine = annotator_repository["engine"]
    admin = annotator_repository["admin_a"]
    binding = repository.ensure_workspace_binding(
        workspace_slug="workspace-a",
        connection_config_id="argilla-main",
        server_version="2.8.0",
        argilla_workspace_id=uuid.uuid4(),
        argilla_workspace_name="workspace-a-remote",
        actor_identity=admin,
        idempotency_key="binding",
    )
    mapping = repository.create_annotator_mapping(
        workspace_slug="workspace-a",
        binding_id=binding.binding.id,
        principal_identity=ExternalIdentity("https://annotator.test", "race"),
        username="race",
        argilla_user_id=uuid.uuid4(),
        personal_argilla_workspace_id=uuid.uuid4(),
        membership_verified=True,
        actor_identity=admin,
        idempotency_key="mapping",
    )
    first = repository.create_cohort(
        workspace_slug="workspace-a",
        name="race-cohort",
        binding_id=binding.binding.id,
        members=(CohortMemberInput(mapping.mapping.id, 4),),
        actor_identity=admin,
        idempotency_key="cohort",
    )
    duplicate = repository.create_cohort_revision(
        workspace_slug="workspace-a",
        cohort_id=first.cohort.id,
        binding_id=binding.binding.id,
        members=(CohortMemberInput(mapping.mapping.id, 4),),
        actor_identity=admin,
        idempotency_key="duplicate-fingerprint",
    )
    assert duplicate.revision.id == first.revision.id
    assert duplicate.response_status == 200

    with Session(engine) as session, session.begin():
        transaction = DatabaseTransaction(session)
        claim = transaction.claim_idempotency(
            actor_identity=admin,
            caller_identity=admin,
            workspace_slug="workspace-a",
            required_permission=Permission.WORKSPACE_MANAGE,
            operation="annotator.cohort.revision.create",
            idempotency_key="simulated-race",
            request_payload={"cohort_id": str(first.cohort.id)},
            channel=AuditChannel.API,
        )
        cohort = session.scalar(
            select(AnnotatorCohort).where(
                AnnotatorCohort.workspace_id == first.cohort.workspace_id,
                AnnotatorCohort.id == first.cohort.id,
            ),
        )
        assert cohort is not None
        original_scalar = session.scalar
        skipped_fingerprint_read = False

        def scalar_with_race(statement, *args, **kwargs):
            nonlocal skipped_fingerprint_read
            if not skipped_fingerprint_read:
                skipped_fingerprint_read = True
                return None
            return original_scalar(statement, *args, **kwargs)

        monkeypatch.setattr(session, "scalar", scalar_with_race)
        revision, reused = repository._persist_revision(
            session,
            cohort=cohort,
            binding_id=binding.binding.id,
            members=(CohortMemberInput(mapping.mapping.id, 4),),
            claim=claim,
        )
        assert reused is True
        assert revision.id == first.revision.id
        assert not session.new
        transaction.complete_idempotency(
            claim,
            actor_identity=admin,
            caller_identity=admin,
            response_status=200,
            response_body={"revision_id": str(revision.id)},
        )

    with Session(engine) as session:
        revisions = session.scalars(
            select(AnnotatorCohortMember).where(
                AnnotatorCohortMember.workspace_id == first.revision.workspace_id,
                AnnotatorCohortMember.cohort_revision_id == first.revision.id,
            ),
        ).all()
    assert len(revisions) == 1
    assert revisions[0].default_capacity == 4

    def create_concurrent_revision(key: str):
        return repository.create_cohort_revision(
            workspace_slug="workspace-a",
            cohort_id=first.cohort.id,
            binding_id=binding.binding.id,
            members=(CohortMemberInput(mapping.mapping.id, 8),),
            actor_identity=admin,
            idempotency_key=key,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        concurrent_results = list(
            executor.map(create_concurrent_revision, ("concurrent-a", "concurrent-b")),
        )
    assert concurrent_results[0].revision.id == concurrent_results[1].revision.id
    assert len(
        repository.list_cohort_revisions(
            workspace_slug="workspace-a",
            identity=admin,
            cohort_id=first.cohort.id,
        ),
    ) == 2
