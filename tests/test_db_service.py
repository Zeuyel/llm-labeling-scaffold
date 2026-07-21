from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest
import llm_labeling_scaffold.db.service as service_module
from sqlalchemy import create_engine, delete, event, func, inspect, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError, StatementError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from llm_labeling_scaffold.db import (
    AuditChannel,
    AuthorizationDenied,
    AuthorizationReason,
    AuthorizationUnavailable,
    DatabaseService,
    ExternalIdentity,
    IdempotencyClaimStatus,
    IdempotencyConflict,
    IdentityTypeConflict,
    LastWorkspaceAdmin,
    MembershipConflict,
    MembershipMutationAction,
    MembershipNotFound,
    Permission,
    PrincipalType,
    Role,
    TaskDraftConflict,
    TaskLifecycleReasonRequired,
    TaskLifecycleState,
    TaskLifecycleTransitionInvalid,
    TaskPreconditionRequired,
    TaskMaterializationState,
)
from llm_labeling_scaffold.db.base import Base
from llm_labeling_scaffold.db.bootstrap import bootstrap_admin
from llm_labeling_scaffold.db.database import create_database_engine, resolve_database_url
from llm_labeling_scaffold.db.enums import AuditActorType, IdempotencyState
from llm_labeling_scaffold.db.models import (
    AuditEvent,
    IdempotencyRecord,
    ImmutableAuditEventError,
    Principal,
    RoleBinding,
    Task,
    TaskDraft,
    TaskRevision,
    TaskRevisionMaterialization,
    Workspace,
)


@pytest.fixture
def engine():
    value = create_database_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(value)
    try:
        yield value
    finally:
        value.dispose()


@pytest.fixture
def seeded_service(engine):
    identity = ExternalIdentity(
        issuer="https://access.example.test",
        subject="viewer-1",
        email_snapshot="viewer@example.test",
    )
    with Session(engine) as session, session.begin():
        viewer = Principal(
            issuer=identity.issuer,
            subject=identity.subject,
            principal_type=PrincipalType.USER,
            email_snapshot=identity.email_snapshot,
        )
        annotator = Principal(
            issuer=identity.issuer,
            subject="annotator-1",
            principal_type=PrincipalType.USER,
        )
        experimenter = Principal(
            issuer=identity.issuer,
            subject="experimenter-1",
            principal_type=PrincipalType.USER,
        )
        admin = Principal(
            issuer=identity.issuer,
            subject="admin-1",
            principal_type=PrincipalType.USER,
        )
        service_principal = Principal(
            issuer="llm-labeling-scaffold",
            subject="mcp-service",
            principal_type=PrincipalType.SERVICE,
        )
        worker_principal = Principal(
            issuer="llm-labeling-scaffold",
            subject="worker-service",
            principal_type=PrincipalType.SERVICE,
        )
        workspace_a = Workspace(slug="workspace-a", name="Workspace A")
        workspace_b = Workspace(slug="workspace-b", name="Workspace B")
        session.add_all(
            [
                viewer,
                annotator,
                experimenter,
                admin,
                service_principal,
                worker_principal,
                workspace_a,
                workspace_b,
            ]
        )
        session.flush()

        task_a = Task(
            workspace_id=workspace_a.id,
            task_key="shared-key",
            name="Task A",
            created_by_principal_id=admin.id,
        )
        task_a_other = Task(
            workspace_id=workspace_a.id,
            task_key="other-task",
            name="Other Task",
            created_by_principal_id=admin.id,
        )
        task_b = Task(
            workspace_id=workspace_b.id,
            task_key="workspace-b-key",
            name="Task B",
            created_by_principal_id=admin.id,
        )
        session.add_all([task_a, task_a_other, task_b])
        session.flush()
        session.add_all(
            [
                RoleBinding(
                    workspace_id=workspace_a.id,
                    principal_id=viewer.id,
                    role=Role.VIEWER,
                    created_by_principal_id=admin.id,
                ),
                RoleBinding(
                    workspace_id=workspace_a.id,
                    principal_id=annotator.id,
                    role=Role.ANNOTATOR,
                    created_by_principal_id=admin.id,
                ),
                RoleBinding(
                    workspace_id=workspace_a.id,
                    principal_id=annotator.id,
                    task_id=task_a.id,
                    role=Role.ANNOTATOR,
                    created_by_principal_id=admin.id,
                ),
                RoleBinding(
                    workspace_id=workspace_a.id,
                    principal_id=experimenter.id,
                    role=Role.EXPERIMENTER,
                    created_by_principal_id=admin.id,
                ),
                RoleBinding(
                    workspace_id=workspace_a.id,
                    principal_id=admin.id,
                    role=Role.ADMIN,
                    created_by_principal_id=admin.id,
                ),
            ]
        )

    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    service = DatabaseService(factory)
    return {
        "service": service,
        "viewer": identity,
        "annotator": ExternalIdentity(identity.issuer, "annotator-1"),
        "experimenter": ExternalIdentity(identity.issuer, "experimenter-1"),
        "admin": ExternalIdentity(identity.issuer, "admin-1"),
        "mcp": ExternalIdentity("llm-labeling-scaffold", "mcp-service"),
        "worker": ExternalIdentity("llm-labeling-scaffold", "worker-service"),
    }


def test_external_identity_uses_issuer_and_subject_only(engine):
    with Session(engine) as session, session.begin():
        session.add(
            Principal(
                issuer="issuer-a",
                subject="subject-1",
                principal_type=PrincipalType.USER,
                email_snapshot="shared@example.test",
            )
        )

    with Session(engine) as session, session.begin():
        session.add(
            Principal(
                issuer="issuer-b",
                subject="subject-1",
                principal_type=PrincipalType.USER,
                email_snapshot="shared@example.test",
            )
        )

    with pytest.raises(IntegrityError):
        with Session(engine) as session, session.begin():
            session.add(
                Principal(
                    issuer="issuer-a",
                    subject="subject-1",
                    principal_type=PrincipalType.SERVICE,
                    email_snapshot="different@example.test",
                )
            )


def test_database_url_is_built_from_shared_compose_components(monkeypatch):
    monkeypatch.delenv("LLS_DATABASE_URL", raising=False)
    monkeypatch.setenv("LLS_DATABASE_HOST", "scaffold-postgres")
    monkeypatch.setenv("LLS_DATABASE_PORT", "5432")
    monkeypatch.setenv("LLS_DATABASE_USER", "scaffold")
    monkeypatch.setenv("LLS_DATABASE_PASSWORD", "password:with/special@chars")
    monkeypatch.setenv("LLS_DATABASE_NAME", "scaffold")

    url = make_url(resolve_database_url())

    assert url.host == "scaffold-postgres"
    assert url.username == "scaffold"
    assert url.password == "password:with/special@chars"
    assert url.database == "scaffold"


def test_unknown_identity_and_missing_membership_have_zero_permissions(seeded_service):
    service = seeded_service["service"]
    unknown = ExternalIdentity("https://access.example.test", "unknown")

    unknown_decision = service.authorize_task(unknown, "workspace-a", "shared-key", Permission.TASK_READ)
    no_membership = service.authorize_task(
        seeded_service["mcp"],
        "workspace-a",
        "shared-key",
        Permission.TASK_READ,
    )

    assert unknown_decision.allowed is False
    assert unknown_decision.reason == AuthorizationReason.UNKNOWN_PRINCIPAL
    assert unknown_decision.workspace is None
    assert unknown_decision.task is None
    assert no_membership.allowed is False
    assert no_membership.reason == AuthorizationReason.RESOURCE_NOT_VISIBLE
    assert no_membership.workspace is None
    assert no_membership.task is None
    with pytest.raises(AuthorizationDenied):
        service.require_task(unknown, "workspace-a", "shared-key", Permission.TASK_READ)


def test_nonmember_cannot_enumerate_workspace_or_task_existence(seeded_service):
    service = seeded_service["service"]
    identity = seeded_service["mcp"]

    decisions = [
        service.authorize_workspace(identity, "workspace-a", Permission.AUDIT_VIEW),
        service.authorize_workspace(identity, "missing-workspace", Permission.AUDIT_VIEW),
        service.authorize_task(identity, "workspace-a", "shared-key", Permission.TASK_READ),
        service.authorize_task(identity, "workspace-a", "missing-task", Permission.TASK_READ),
        service.authorize_task(identity, "missing-workspace", "shared-key", Permission.TASK_READ),
    ]

    assert {decision.reason for decision in decisions} == {AuthorizationReason.RESOURCE_NOT_VISIBLE}
    assert all(decision.workspace is None for decision in decisions)
    assert all(decision.task is None for decision in decisions)


def test_membership_mutation_preserves_resource_not_visible_precheck(seeded_service):
    service = seeded_service["service"]
    unknown_target = ExternalIdentity("https://access.example.test", "unknown-membership-target")

    for workspace_slug in ("workspace-a", "missing-workspace"):
        with pytest.raises(AuthorizationDenied) as denied:
            service.grant_workspace_membership(
                workspace_slug=workspace_slug,
                target_identity=unknown_target,
                role=Role.VIEWER,
                actor_identity=seeded_service["mcp"],
                caller_identity=seeded_service["mcp"],
                channel=AuditChannel.API,
            )
        assert denied.value.decision.reason == AuthorizationReason.RESOURCE_NOT_VISIBLE
        assert denied.value.decision.workspace is None


def test_role_matrix_task_acl_and_workspace_isolation(seeded_service):
    service = seeded_service["service"]

    assert service.authorize_task(
        seeded_service["viewer"],
        "workspace-a",
        "shared-key",
        Permission.TASK_READ,
    ).allowed
    assert not service.authorize_task(
        seeded_service["viewer"],
        "workspace-a",
        "shared-key",
        Permission.TASK_EDIT,
    ).allowed
    assert not service.authorize_workspace(
        seeded_service["viewer"],
        "workspace-a",
        Permission.TASK_CREATE,
    ).allowed

    assert service.authorize_task(
        seeded_service["annotator"],
        "workspace-a",
        "shared-key",
        Permission.ANNOTATION_WORK,
    ).allowed
    for permission in (
        Permission.TASK_EDIT,
        Permission.TASK_PUBLISH,
        Permission.IMPORT_SUBMIT,
        Permission.ANNOTATION_REVIEW,
    ):
        assert not service.authorize_task(
            seeded_service["annotator"],
            "workspace-a",
            "shared-key",
            permission,
        ).allowed
    assert service.authorize_task(
        seeded_service["annotator"],
        "workspace-a",
        "other-task",
        Permission.TASK_READ,
    ).allowed
    assert not service.authorize_workspace(
        seeded_service["annotator"],
        "workspace-a",
        Permission.TASK_CREATE,
    ).allowed

    experimenter = seeded_service["experimenter"]
    for permission in (
        Permission.TASK_READ,
        Permission.TASK_EDIT,
        Permission.TASK_PUBLISH,
        Permission.IMPORT_SUBMIT,
        Permission.ANNOTATION_WORK,
        Permission.ANNOTATION_REVIEW,
    ):
        assert service.authorize_task(experimenter, "workspace-a", "shared-key", permission).allowed
    assert service.authorize_workspace(experimenter, "workspace-a", Permission.AUDIT_VIEW).allowed
    assert service.authorize_workspace(experimenter, "workspace-a", Permission.TASK_CREATE).allowed
    assert not service.authorize_workspace(experimenter, "workspace-a", Permission.WORKSPACE_MANAGE).allowed

    admin = seeded_service["admin"]
    assert service.authorize_workspace(admin, "workspace-a", Permission.TASK_CREATE).allowed
    assert service.authorize_workspace(admin, "workspace-a", Permission.WORKSPACE_MANAGE).allowed
    cross_workspace = service.authorize_task(admin, "workspace-b", "workspace-b-key", Permission.TASK_READ)
    assert cross_workspace.allowed is False
    assert cross_workspace.reason == AuthorizationReason.RESOURCE_NOT_VISIBLE
    assert cross_workspace.workspace is None
    assert cross_workspace.task is None


def test_annotation_management_permissions_remain_separate(seeded_service):
    service = seeded_service["service"]

    for subject in ("viewer", "annotator"):
        identity = seeded_service[subject]
        assert service.authorize_task(
            identity,
            "workspace-a",
            "shared-key",
            Permission.TASK_READ,
        ).allowed
        assert not service.authorize_task(
            identity,
            "workspace-a",
            "shared-key",
            Permission.ANNOTATION_REVIEW,
        ).allowed

    experimenter = seeded_service["experimenter"]
    assert service.authorize_task(
        experimenter,
        "workspace-a",
        "shared-key",
        Permission.ANNOTATION_REVIEW,
    ).allowed
    assert not service.authorize_workspace(
        experimenter,
        "workspace-a",
        Permission.WORKSPACE_MANAGE,
    ).allowed

    admin = seeded_service["admin"]
    assert service.authorize_task(
        admin,
        "workspace-a",
        "shared-key",
        Permission.ANNOTATION_REVIEW,
    ).allowed
    assert service.authorize_workspace(
        admin,
        "workspace-a",
        Permission.WORKSPACE_MANAGE,
    ).allowed


def test_permission_scopes_fail_closed_and_filter_capabilities(seeded_service, engine):
    service = seeded_service["service"]
    admin = seeded_service["admin"]

    workspace_with_task_permission = service.authorize_workspace(
        admin,
        "workspace-a",
        Permission.TASK_READ,
    )
    task_with_workspace_permission = service.authorize_task(
        admin,
        "workspace-a",
        "shared-key",
        Permission.TASK_CREATE,
    )
    assert workspace_with_task_permission.allowed is False
    assert workspace_with_task_permission.reason == AuthorizationReason.INVALID_PERMISSION_SCOPE
    assert workspace_with_task_permission.workspace is None
    assert task_with_workspace_permission.allowed is False
    assert task_with_workspace_permission.reason == AuthorizationReason.INVALID_PERMISSION_SCOPE
    assert task_with_workspace_permission.task is None

    with Session(engine) as session, session.begin():
        workspace_id = session.scalar(select(Workspace.id).where(Workspace.slug == "workspace-a"))
        task_id = session.scalar(
            select(Task.id).where(Task.workspace_id == workspace_id, Task.task_key == "shared-key"),
        )
        principal_id = session.scalar(select(Principal.id).where(Principal.subject == "mcp-service"))
        session.add(
            RoleBinding(
                workspace_id=workspace_id,
                principal_id=principal_id,
                task_id=task_id,
                role=Role.EXPERIMENTER,
            )
        )

    task_only_decision = service.authorize_task(
        seeded_service["mcp"],
        "workspace-a",
        "shared-key",
        Permission.TASK_EDIT,
    )
    assert task_only_decision.allowed is False
    assert task_only_decision.reason == AuthorizationReason.RESOURCE_NOT_VISIBLE
    assert service.authorize_workspace(
        seeded_service["mcp"],
        "workspace-a",
        Permission.AUDIT_VIEW,
    ).reason == AuthorizationReason.RESOURCE_NOT_VISIBLE
    assert service.get_session(seeded_service["mcp"]).workspaces == ()
    task_access = service.list_authorized_tasks(
        seeded_service["mcp"],
        "workspace-a",
        limit=10,
    )
    assert task_access.workspace is None
    assert task_access.items == ()


def test_task_acl_cannot_bypass_revoked_workspace_membership(seeded_service, engine):
    service = seeded_service["service"]
    target = ExternalIdentity("https://access.example.test", "task-only-after-revoke")
    target_ref = service.resolve_or_provision(target)
    service.grant_workspace_membership(
        workspace_slug="workspace-a",
        target_identity=target,
        role=Role.VIEWER,
        actor_identity=seeded_service["admin"],
        caller_identity=seeded_service["admin"],
        channel=AuditChannel.API,
    )
    with Session(engine) as session, session.begin():
        task_id = session.scalar(
            select(Task.id).where(Task.workspace_id == session.scalar(
                select(Workspace.id).where(Workspace.slug == "workspace-a"),
            ), Task.task_key == "shared-key")
        )
        workspace_id = session.scalar(select(Workspace.id).where(Workspace.slug == "workspace-a"))
        session.add(
            RoleBinding(
                workspace_id=workspace_id,
                principal_id=target_ref.id,
                task_id=task_id,
                role=Role.EXPERIMENTER,
            )
        )

    assert service.authorize_task(
        target,
        "workspace-a",
        "shared-key",
        Permission.TASK_EDIT,
    ).allowed
    revoked = service.revoke_workspace_membership(
        workspace_slug="workspace-a",
        target_identity=target,
        actor_identity=seeded_service["admin"],
        caller_identity=seeded_service["admin"],
        channel=AuditChannel.API,
    )
    assert revoked.action == MembershipMutationAction.REVOKED
    decision = service.authorize_task(target, "workspace-a", "shared-key", Permission.TASK_EDIT)
    assert decision.allowed is False
    assert decision.reason == AuthorizationReason.RESOURCE_NOT_VISIBLE
    assert service.list_authorized_tasks(target, "workspace-a", limit=10).items == ()
    with Session(engine) as session:
        assert session.scalar(
            select(func.count()).select_from(RoleBinding).where(
                RoleBinding.principal_id == target_ref.id,
                RoleBinding.task_id.is_not(None),
            )
        ) == 1


def test_resolve_or_provision_never_merges_by_email_or_grants_membership(seeded_service, engine):
    service = seeded_service["service"]
    first_identity = ExternalIdentity(
        "https://issuer-one.example",
        "subject-one",
        email_snapshot="shared@example.test",
        display_name_snapshot="First",
    )
    second_identity = ExternalIdentity(
        "https://issuer-two.example",
        "subject-two",
        email_snapshot="shared@example.test",
        display_name_snapshot="Second",
    )

    first = service.resolve_or_provision(first_identity)
    second = service.resolve_or_provision(second_identity)
    refreshed = service.resolve_or_provision(
        ExternalIdentity(
            first_identity.issuer,
            first_identity.subject,
            email_snapshot="new@example.test",
            display_name_snapshot="Updated",
        )
    )

    assert first.id != second.id
    assert refreshed.id == first.id
    assert refreshed.email_snapshot == "new@example.test"
    assert service.get_session(first_identity).workspaces == ()
    assert service.authorize_workspace(first_identity, "workspace-a", Permission.AUDIT_VIEW).reason == (
        AuthorizationReason.RESOURCE_NOT_VISIBLE
    )
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Principal)) == 8
        assert session.scalar(
            select(func.count()).select_from(RoleBinding).where(
                RoleBinding.principal_id.in_((first.id, second.id)),
            )
        ) == 0


def test_resolve_or_provision_preserves_principal_type_conflicts(seeded_service):
    service = seeded_service["service"]
    identity = ExternalIdentity("https://issuer.example", "stable-subject")

    principal = service.resolve_or_provision(identity, PrincipalType.USER)

    assert principal.principal_type == PrincipalType.USER
    with pytest.raises(IdentityTypeConflict):
        service.resolve_or_provision(identity, PrincipalType.SERVICE)


def test_get_session_lists_only_authorized_workspaces_and_capabilities(seeded_service):
    service = seeded_service["service"]

    unknown_session = service.get_session(ExternalIdentity("issuer", "unknown"))
    assert unknown_session.principal is None
    assert unknown_session.workspaces == ()

    annotator_session = service.get_session(seeded_service["annotator"])
    assert [access.workspace.slug for access in annotator_session.workspaces] == ["workspace-a"]
    annotator_workspace = annotator_session.workspaces[0]
    assert annotator_workspace.roles == (Role.ANNOTATOR,)
    assert annotator_workspace.workspace_capabilities == ()
    assert annotator_workspace.task_capabilities == (
        Permission.TASK_READ,
        Permission.ANNOTATION_WORK,
    )
    annotator_tasks = service.list_authorized_tasks(
        seeded_service["annotator"],
        "workspace-a",
        limit=10,
    )
    assert [task.task.task_key for task in annotator_tasks.items] == ["other-task", "shared-key"]
    assert annotator_tasks.items[0].capabilities == (
        Permission.TASK_READ,
        Permission.ANNOTATION_WORK,
    )

    viewer_workspace = service.get_session(seeded_service["viewer"]).workspaces[0]
    assert viewer_workspace.roles == (Role.VIEWER,)
    assert viewer_workspace.workspace_capabilities == ()
    assert viewer_workspace.task_capabilities == (Permission.TASK_READ,)

    experimenter_workspace = service.get_session(seeded_service["experimenter"]).workspaces[0]
    assert experimenter_workspace.workspace_capabilities == (
        Permission.TASK_CREATE,
        Permission.AUDIT_VIEW,
    )
    assert Permission.TASK_CREATE not in experimenter_workspace.task_capabilities
    assert Permission.TASK_EDIT in experimenter_workspace.task_capabilities


def test_authorized_task_listing_is_explicit_and_bounded(seeded_service, engine):
    with Session(engine) as session, session.begin():
        workspace_id = session.scalar(select(Workspace.id).where(Workspace.slug == "workspace-a"))
        session.add_all(
            [
                Task(workspace_id=workspace_id, task_key=f"bulk-{index:04d}", name=f"Bulk {index}")
                for index in range(250)
            ]
        )

    statements: list[str] = []

    def capture_sql(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())

    event.listen(engine, "before_cursor_execute", capture_sql)
    try:
        auth_session = seeded_service["service"].get_session(seeded_service["viewer"])
    finally:
        event.remove(engine, "before_cursor_execute", capture_sql)

    assert [access.workspace.slug for access in auth_session.workspaces] == ["workspace-a"]
    assert not any("from tasks" in statement for statement in statements)

    service = seeded_service["service"]
    with pytest.raises(TypeError):
        service.list_authorized_tasks(seeded_service["viewer"], "workspace-a")
    with pytest.raises(ValueError):
        service.list_authorized_tasks(seeded_service["viewer"], "workspace-a", limit=0)
    with pytest.raises(ValueError):
        service.list_authorized_tasks(seeded_service["viewer"], "workspace-a", limit=101)

    first_page = service.list_authorized_tasks(
        seeded_service["viewer"],
        "workspace-a",
        limit=25,
    )
    second_page = service.list_authorized_tasks(
        seeded_service["viewer"],
        "workspace-a",
        limit=25,
        after_task_key=first_page.next_cursor,
    )

    assert first_page.workspace.slug == "workspace-a"
    assert len(first_page.items) == 25
    assert first_page.next_cursor == first_page.items[-1].task.task_key
    assert len(second_page.items) == 25
    assert {item.task.id for item in first_page.items}.isdisjoint(
        {item.task.id for item in second_page.items},
    )
    assert all(item.capabilities == (Permission.TASK_READ,) for item in first_page.items)


def test_workspace_and_task_role_binding_uniqueness(engine):
    with Session(engine) as session, session.begin():
        principal = Principal(issuer="issuer", subject="subject", principal_type=PrincipalType.USER)
        workspace = Workspace(slug="workspace", name="Workspace")
        session.add_all([principal, workspace])
        session.flush()
        task = Task(workspace_id=workspace.id, task_key="task", name="Task")
        session.add(task)
        session.flush()
        session.add(
            RoleBinding(
                workspace_id=workspace.id,
                principal_id=principal.id,
                role=Role.VIEWER,
            )
        )

    with pytest.raises(IntegrityError):
        with Session(engine) as session, session.begin():
            principal_id = session.scalar(select(Principal.id))
            workspace_id = session.scalar(select(Workspace.id))
            session.add(
                RoleBinding(
                    workspace_id=workspace_id,
                    principal_id=principal_id,
                    role=Role.ADMIN,
                )
            )

    with Session(engine) as session, session.begin():
        principal_id = session.scalar(select(Principal.id))
        workspace_id = session.scalar(select(Workspace.id))
        task_id = session.scalar(select(Task.id))
        session.add(
            RoleBinding(
                workspace_id=workspace_id,
                principal_id=principal_id,
                task_id=task_id,
                role=Role.ANNOTATOR,
            )
        )

    with pytest.raises(IntegrityError):
        with Session(engine) as session, session.begin():
            principal_id = session.scalar(select(Principal.id))
            workspace_id = session.scalar(select(Workspace.id))
            task_id = session.scalar(select(Task.id))
            session.add(
                RoleBinding(
                    workspace_id=workspace_id,
                    principal_id=principal_id,
                    task_id=task_id,
                    role=Role.EXPERIMENTER,
                )
            )


def test_task_key_is_globally_unique_and_task_acl_stays_workspace_scoped(seeded_service, engine):
    with Session(engine) as session:
        assert session.scalar(
            select(func.count()).select_from(Task).where(Task.task_key == "shared-key"),
        ) == 1
        workspace_a_id = session.scalar(select(Workspace.id).where(Workspace.slug == "workspace-a"))
        workspace_b_id = session.scalar(select(Workspace.id).where(Workspace.slug == "workspace-b"))
        task_a_id = session.scalar(
            select(Task.id).where(Task.workspace_id == workspace_a_id, Task.task_key == "shared-key"),
        )
        principal_id = session.scalar(select(Principal.id).where(Principal.subject == "mcp-service"))

    with pytest.raises(IntegrityError):
        with Session(engine) as session, session.begin():
            session.add(Task(workspace_id=workspace_a_id, task_key="shared-key", name="Duplicate"))

    with pytest.raises(IntegrityError):
        with Session(engine) as session, session.begin():
            session.add(Task(workspace_id=workspace_b_id, task_key="shared-key", name="Cross Workspace Duplicate"))

    with pytest.raises(IntegrityError):
        with Session(engine) as session, session.begin():
            session.add(
                RoleBinding(
                    workspace_id=workspace_b_id,
                    principal_id=principal_id,
                    task_id=task_a_id,
                    role=Role.VIEWER,
                )
            )


def test_role_enum_rejects_unknown_values(engine):
    with pytest.raises(StatementError):
        with Session(engine) as session, session.begin():
            principal = Principal(issuer="issuer", subject="subject", principal_type=PrincipalType.USER)
            workspace = Workspace(slug="workspace", name="Workspace")
            session.add_all([principal, workspace])
            session.flush()
            session.add(
                RoleBinding(
                    workspace_id=workspace.id,
                    principal_id=principal.id,
                    role="owner",
                )
            )
            session.flush()


def test_idempotency_key_is_bound_to_actor_caller_and_fingerprint(seeded_service, engine):
    with Session(engine) as session, session.begin():
        workspace_id = session.scalar(select(Workspace.id).where(Workspace.slug == "workspace-a"))
        actor_id = session.scalar(select(Principal.id).where(Principal.subject == "admin-1"))
        caller_id = session.scalar(select(Principal.id).where(Principal.subject == "mcp-service"))
        task_id = session.scalar(select(Task.id).where(Task.task_key == "shared-key"))
        session.add(
            IdempotencyRecord(
                workspace_id=workspace_id,
                actor_principal_id=actor_id,
                caller_principal_id=caller_id,
                operation="task.publish",
                idempotency_key_hash=hashlib.sha256(b"request-1").hexdigest(),
                request_fingerprint="fingerprint-a",
                required_permission=Permission.TASK_PUBLISH.value,
                resource_type="task",
                resource_id=task_id,
                channel=AuditChannel.MCP,
                state=IdempotencyState.PENDING,
            )
        )

    with pytest.raises(IntegrityError):
        with Session(engine) as session, session.begin():
            workspace_id = session.scalar(select(Workspace.id).where(Workspace.slug == "workspace-a"))
            different_actor_id = session.scalar(select(Principal.id).where(Principal.subject == "viewer-1"))
            different_caller_id = session.scalar(select(Principal.id).where(Principal.subject == "admin-1"))
            task_id = session.scalar(select(Task.id).where(Task.task_key == "shared-key"))
            session.add(
                IdempotencyRecord(
                    workspace_id=workspace_id,
                    actor_principal_id=different_actor_id,
                    caller_principal_id=different_caller_id,
                    operation="task.publish",
                    idempotency_key_hash=hashlib.sha256(b"request-1").hexdigest(),
                    request_fingerprint="fingerprint-b",
                    required_permission=Permission.TASK_PUBLISH.value,
                    resource_type="task",
                    resource_id=task_id,
                    channel=AuditChannel.MCP,
                    state=IdempotencyState.PENDING,
                )
            )

    with Session(engine) as session:
        record = session.scalar(select(IdempotencyRecord))
        assert record.request_fingerprint == "fingerprint-a"
        assert record.actor_principal_id != record.caller_principal_id
        assert record.idempotency_key_hash == hashlib.sha256(b"request-1").hexdigest()
        assert "idempotency_key" not in IdempotencyRecord.__table__.columns


def test_idempotency_claim_conflicts_and_successful_replay(seeded_service, engine):
    service = seeded_service["service"]
    claim_args = {
        "actor_identity": seeded_service["admin"],
        "caller_identity": seeded_service["mcp"],
        "workspace_slug": "workspace-a",
        "task_key": "shared-key",
        "required_permission": Permission.TASK_PUBLISH,
        "operation": "task.publish",
        "idempotency_key": "claim-1",
        "channel": AuditChannel.MCP,
    }

    first = service.claim_idempotency(
        **claim_args,
        request_payload={"label": "e\u0301", "count": 2},
    )
    pending = service.claim_idempotency(
        **claim_args,
        request_payload={"count": 2, "label": "é"},
    )

    assert first.status == IdempotencyClaimStatus.CLAIMED
    assert pending.status == IdempotencyClaimStatus.PENDING
    assert first.record_id == pending.record_id
    assert first.request_fingerprint == pending.request_fingerprint
    assert first.idempotency_key_hash == hashlib.sha256(b"claim-1").hexdigest()
    assert not hasattr(first, "idempotency_key")
    assert first.required_permission == Permission.TASK_PUBLISH
    assert first.resource_type == "task"
    assert first.channel == AuditChannel.MCP
    assert pending.response_body is None

    completed = service.complete_idempotency(
        first,
        actor_identity=seeded_service["admin"],
        caller_identity=seeded_service["mcp"],
        response_status=202,
        response_body={"published": True},
    )
    replay = service.claim_idempotency(
        **claim_args,
        request_payload={"count": 2, "label": "é"},
    )

    assert completed.status == IdempotencyClaimStatus.REPLAY
    assert replay.status == IdempotencyClaimStatus.REPLAY
    assert replay.response_status == 202
    assert replay.response_body == {"published": True}

    same_completion = service.complete_idempotency(
        first,
        actor_identity=seeded_service["admin"],
        caller_identity=seeded_service["mcp"],
        response_status=202,
        response_body={"published": True},
    )
    assert same_completion.response_body == {"published": True}

    for response_status, response_body, succeeded in (
        (409, {"published": True}, True),
        (202, {"published": 1}, True),
        (202, {"published": 1.0}, True),
        (202, {"published": True}, False),
    ):
        with pytest.raises(IdempotencyConflict):
            service.complete_idempotency(
                first,
                actor_identity=seeded_service["admin"],
                caller_identity=seeded_service["mcp"],
                response_status=response_status,
                response_body=response_body,
                succeeded=succeeded,
            )

    with Session(engine) as session:
        events = session.scalars(
            select(AuditEvent).where(AuditEvent.event_type == "idempotency.completed")
        ).all()
        assert len(events) == 1
        assert events[0].resource_type == "task"
        assert events[0].resource_id == str(first.resource_id)
        assert events[0].details == {
            "idempotency_record_id": str(first.record_id),
            "operation": "task.publish",
            "response_status": 202,
            "state": "succeeded",
        }
        assert "response_body" not in events[0].details
        assert "idempotency_key_hash" not in events[0].details

    with pytest.raises(IdempotencyConflict) as actor_conflict:
        service.claim_idempotency(
            **{
                **claim_args,
                "actor_identity": seeded_service["viewer"],
                "required_permission": Permission.TASK_READ,
            },
            request_payload={"count": 2, "label": "é"},
        )
    with pytest.raises(IdempotencyConflict) as caller_conflict:
        service.claim_idempotency(
            **{**claim_args, "caller_identity": seeded_service["worker"]},
            request_payload={"count": 2, "label": "é"},
        )
    with pytest.raises(IdempotencyConflict) as fingerprint_conflict:
        service.claim_idempotency(
            **claim_args,
            request_payload={"count": 3, "label": "é"},
        )

    assert actor_conflict.value.code == "idempotency_conflict"
    assert caller_conflict.value.code == "idempotency_conflict"
    assert fingerprint_conflict.value.code == "idempotency_conflict"

    with pytest.raises(AuthorizationDenied) as user_caller:
        service.claim_idempotency(
            **{**claim_args, "caller_identity": seeded_service["viewer"]},
            request_payload={"count": 2, "label": "é"},
        )
    assert user_caller.value.decision.reason == AuthorizationReason.INVALID_AUDIT_CONTEXT


def test_idempotency_completion_rejects_revoked_membership(seeded_service, engine):
    service = seeded_service["service"]
    claim = service.claim_idempotency(
        actor_identity=seeded_service["experimenter"],
        caller_identity=seeded_service["mcp"],
        workspace_slug="workspace-a",
        task_key="shared-key",
        required_permission=Permission.TASK_PUBLISH,
        operation="task.publish",
        idempotency_key="revoked-membership",
        request_payload={"revision": 1},
        channel=AuditChannel.MCP,
    )

    service.revoke_workspace_membership(
        workspace_slug="workspace-a",
        target_identity=seeded_service["experimenter"],
        actor_identity=seeded_service["admin"],
        caller_identity=seeded_service["mcp"],
        channel=AuditChannel.MCP,
    )

    with pytest.raises(AuthorizationDenied) as denied:
        service.complete_idempotency(
            claim,
            actor_identity=seeded_service["experimenter"],
            caller_identity=seeded_service["mcp"],
            response_status=200,
            response_body={"published": True},
        )
    assert denied.value.decision.reason == AuthorizationReason.RESOURCE_NOT_VISIBLE
    with Session(engine) as session:
        record = session.get(IdempotencyRecord, claim.record_id)
        assert record.state == IdempotencyState.PENDING


@pytest.mark.parametrize(
    ("inactive_target", "expected_reason"),
    [
        ("actor", AuthorizationReason.INACTIVE_PRINCIPAL),
        ("caller", AuthorizationReason.INVALID_AUDIT_CONTEXT),
        ("workspace", AuthorizationReason.INACTIVE_WORKSPACE),
    ],
)
def test_idempotency_completion_rejects_inactive_context(
    seeded_service,
    engine,
    inactive_target,
    expected_reason,
):
    service = seeded_service["service"]
    claim = service.claim_idempotency(
        actor_identity=seeded_service["admin"],
        caller_identity=seeded_service["mcp"],
        workspace_slug="workspace-a",
        task_key="shared-key",
        required_permission=Permission.TASK_PUBLISH,
        operation="task.publish",
        idempotency_key=f"inactive-{inactive_target}",
        request_payload={"revision": 1},
        channel=AuditChannel.MCP,
    )

    with Session(engine) as session, session.begin():
        if inactive_target == "actor":
            principal = session.scalar(select(Principal).where(Principal.subject == "admin-1"))
            principal.is_active = False
        elif inactive_target == "caller":
            principal = session.scalar(select(Principal).where(Principal.subject == "mcp-service"))
            principal.is_active = False
        else:
            workspace = session.scalar(select(Workspace).where(Workspace.slug == "workspace-a"))
            workspace.is_active = False

    with pytest.raises(AuthorizationDenied) as denied:
        service.complete_idempotency(
            claim,
            actor_identity=seeded_service["admin"],
            caller_identity=seeded_service["mcp"],
            response_status=200,
            response_body={"published": True},
        )
    assert denied.value.decision.reason == expected_reason
    with Session(engine) as session:
        assert session.get(IdempotencyRecord, claim.record_id).state == IdempotencyState.PENDING


def test_idempotency_completion_rejects_forged_claim_without_enumerating_context(
    seeded_service,
    engine,
):
    service = seeded_service["service"]
    claim = service.claim_idempotency(
        actor_identity=seeded_service["admin"],
        caller_identity=seeded_service["mcp"],
        workspace_slug="workspace-a",
        task_key="shared-key",
        required_permission=Permission.TASK_PUBLISH,
        operation="task.publish",
        idempotency_key="forged-claim",
        request_payload={"revision": 1},
        channel=AuditChannel.MCP,
    )
    with Session(engine) as session:
        other_workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "workspace-b")
        )
        other_task_id = session.scalar(select(Task.id).where(Task.task_key == "workspace-b-key"))
        viewer_id = session.scalar(select(Principal.id).where(Principal.subject == "viewer-1"))

    forged_claims = (
        replace(claim, workspace_id=other_workspace_id),
        replace(claim, actor_principal_id=viewer_id),
        replace(claim, resource_id=other_task_id),
        replace(claim, required_permission=Permission.TASK_READ),
        replace(claim, channel=AuditChannel.API),
    )
    for forged in forged_claims:
        with pytest.raises(IdempotencyConflict) as conflict:
            service.complete_idempotency(
                forged,
                actor_identity=seeded_service["admin"],
                caller_identity=seeded_service["mcp"],
                response_status=200,
                response_body={"published": True},
            )
        assert conflict.value.code == "idempotency_conflict"

    for actor_identity, caller_identity in (
        (seeded_service["viewer"], seeded_service["mcp"]),
        (seeded_service["admin"], seeded_service["worker"]),
    ):
        with pytest.raises(IdempotencyConflict):
            service.complete_idempotency(
                claim,
                actor_identity=actor_identity,
                caller_identity=caller_identity,
                response_status=200,
                response_body={"published": True},
            )


def test_idempotency_replay_uses_canonical_body_and_exact_json_types(seeded_service):
    service = seeded_service["service"]
    claim = service.claim_idempotency(
        actor_identity=seeded_service["admin"],
        caller_identity=seeded_service["mcp"],
        workspace_slug="workspace-a",
        task_key="shared-key",
        required_permission=Permission.TASK_PUBLISH,
        operation="task.publish",
        idempotency_key="canonical-response",
        request_payload={"revision": 1},
        channel=AuditChannel.MCP,
    )
    service.complete_idempotency(
        claim,
        actor_identity=seeded_service["admin"],
        caller_identity=seeded_service["mcp"],
        response_status=200,
        response_body={"result": {"label": "e\u0301", "count": 1}},
    )

    replay = service.complete_idempotency(
        claim,
        actor_identity=seeded_service["admin"],
        caller_identity=seeded_service["mcp"],
        response_status=200,
        response_body={"result": {"count": 1, "label": "é"}},
    )
    assert replay.response_body == {"result": {"count": 1, "label": "é"}}

    for response_body in (
        {"result": {"count": True, "label": "é"}},
        {"result": {"count": 1.0, "label": "é"}},
    ):
        with pytest.raises(IdempotencyConflict):
            service.complete_idempotency(
                claim,
                actor_identity=seeded_service["admin"],
                caller_identity=seeded_service["mcp"],
                response_status=200,
                response_body=response_body,
            )
    with pytest.raises(ValueError, match="response_status"):
        service.complete_idempotency(
            claim,
            actor_identity=seeded_service["admin"],
            caller_identity=seeded_service["mcp"],
            response_status=True,
            response_body={"result": {"count": 1, "label": "é"}},
        )
    with pytest.raises(ValueError, match="succeeded"):
        service.complete_idempotency(
            claim,
            actor_identity=seeded_service["admin"],
            caller_identity=seeded_service["mcp"],
            response_status=200,
            response_body={"result": {"count": 1, "label": "é"}},
            succeeded=1,
        )


def test_idempotency_rejects_sensitive_request_and_response_json(seeded_service, engine):
    service = seeded_service["service"]
    claim_args = {
        "actor_identity": seeded_service["admin"],
        "caller_identity": seeded_service["mcp"],
        "workspace_slug": "workspace-a",
        "task_key": "shared-key",
        "required_permission": Permission.TASK_PUBLISH,
        "operation": "task.publish",
        "channel": AuditChannel.MCP,
    }
    sensitive_requests = (
        {"message": "contact admin@example.test"},
        {"assertion": "redacted"},
        {"access_token": "redacted"},
        {"refresh_token": "redacted"},
    )
    for index, request_payload in enumerate(sensitive_requests):
        with pytest.raises(ValueError):
            service.claim_idempotency(
                **claim_args,
                idempotency_key=f"sensitive-request-{index}",
                request_payload=request_payload,
            )

    claim = service.claim_idempotency(
        **claim_args,
        idempotency_key="sensitive-response",
        request_payload={"revision": 1},
    )
    with pytest.raises(ValueError):
        service.complete_idempotency(
            claim,
            actor_identity=seeded_service["admin"],
            caller_identity=seeded_service["mcp"],
            response_status=200,
            response_body={"message": "Bearer secret-token"},
        )
    with Session(engine) as session:
        assert session.get(IdempotencyRecord, claim.record_id).state == IdempotencyState.PENDING
        assert session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.event_type == "idempotency.completed")
        ) == 0


def test_workspace_membership_lifecycle_is_atomic_idempotent_and_audited(seeded_service, engine):
    service = seeded_service["service"]
    target = ExternalIdentity(
        "https://access.example.test",
        "new-member",
        email_snapshot="new-member@example.test",
    )
    target_ref = service.resolve_or_provision(target)

    granted = service.grant_workspace_membership(
        workspace_slug="workspace-a",
        target_identity=target,
        role=Role.VIEWER,
        actor_identity=seeded_service["admin"],
        caller_identity=seeded_service["mcp"],
        channel=AuditChannel.MCP,
        request_id="membership-grant",
    )
    grant_unchanged = service.grant_workspace_membership(
        workspace_slug="workspace-a",
        target_identity=target,
        role=Role.VIEWER,
        actor_identity=seeded_service["admin"],
        caller_identity=seeded_service["admin"],
        channel=AuditChannel.API,
    )
    with pytest.raises(MembershipConflict):
        service.grant_workspace_membership(
            workspace_slug="workspace-a",
            target_identity=target,
            role=Role.ANNOTATOR,
            actor_identity=seeded_service["admin"],
            caller_identity=seeded_service["admin"],
            channel=AuditChannel.API,
        )

    changed = service.change_workspace_membership(
        workspace_slug="workspace-a",
        target_identity=target,
        role=Role.EXPERIMENTER,
        actor_identity=seeded_service["admin"],
        caller_identity=seeded_service["admin"],
        channel=AuditChannel.API,
        request_id="membership-change",
    )
    change_unchanged = service.change_workspace_membership(
        workspace_slug="workspace-a",
        target_identity=target,
        role=Role.EXPERIMENTER,
        actor_identity=seeded_service["admin"],
        caller_identity=seeded_service["admin"],
        channel=AuditChannel.API,
    )
    assert service.authorize_workspace(target, "workspace-a", Permission.AUDIT_VIEW).allowed is True

    with Session(engine) as session, session.begin():
        principal = session.get(Principal, target_ref.id)
        principal.is_active = False

    with pytest.raises(MembershipNotFound):
        service.change_workspace_membership(
            workspace_slug="workspace-a",
            target_identity=target,
            role=Role.VIEWER,
            actor_identity=seeded_service["admin"],
            caller_identity=seeded_service["admin"],
            channel=AuditChannel.API,
        )
    revoked = service.revoke_workspace_membership(
        workspace_slug="workspace-a",
        target_identity=target,
        actor_identity=seeded_service["admin"],
        caller_identity=seeded_service["admin"],
        channel=AuditChannel.API,
        request_id="membership-revoke",
    )
    revoke_unchanged = service.revoke_workspace_membership(
        workspace_slug="workspace-a",
        target_identity=target,
        actor_identity=seeded_service["admin"],
        caller_identity=seeded_service["admin"],
        channel=AuditChannel.API,
    )

    assert granted.action == MembershipMutationAction.GRANTED
    assert grant_unchanged.action == MembershipMutationAction.UNCHANGED
    assert changed.action == MembershipMutationAction.CHANGED
    assert change_unchanged.action == MembershipMutationAction.UNCHANGED
    assert revoked.action == MembershipMutationAction.REVOKED
    assert revoked.principal.is_active is False
    assert revoke_unchanged.action == MembershipMutationAction.UNCHANGED
    assert grant_unchanged.audit_event_id is None
    assert change_unchanged.audit_event_id is None
    assert revoke_unchanged.audit_event_id is None

    with Session(engine) as session:
        events = session.scalars(
            select(AuditEvent)
            .where(AuditEvent.event_type.like("workspace.membership_%"))
            .order_by(AuditEvent.event_type)
        ).all()
        assert {event.event_type for event in events} == {
            "workspace.membership_granted",
            "workspace.membership_changed",
            "workspace.membership_revoked",
        }
        assert len(events) == 3
        assert all(event.details["target_principal_id"] == str(target_ref.id) for event in events)
        assert all("email" not in event.details for event in events)
        assert session.scalar(
            select(func.count()).select_from(RoleBinding).where(
                RoleBinding.principal_id == target_ref.id,
                RoleBinding.task_id.is_(None),
            )
        ) == 0


def test_sqlite_membership_grant_rereads_a_stale_missing_binding(
    seeded_service,
    engine,
    monkeypatch,
):
    service = seeded_service["service"]
    target = ExternalIdentity("https://access.example.test", "stale-grant-target")
    target_ref = service.resolve_or_provision(target)
    granted = service.grant_workspace_membership(
        workspace_slug="workspace-a",
        target_identity=target,
        role=Role.VIEWER,
        actor_identity=seeded_service["admin"],
        caller_identity=seeded_service["admin"],
        channel=AuditChannel.API,
    )
    original = service_module.DatabaseTransaction._workspace_binding
    lock_modes = []

    def stale_first_lookup(transaction, workspace_id, principal_id, *, for_update=True):
        if principal_id == target_ref.id:
            lock_modes.append(for_update)
            if len(lock_modes) == 1:
                return None
        return original(
            transaction,
            workspace_id,
            principal_id,
            for_update=for_update,
        )

    monkeypatch.setattr(
        service_module.DatabaseTransaction,
        "_workspace_binding",
        stale_first_lookup,
    )

    unchanged = service.grant_workspace_membership(
        workspace_slug="workspace-a",
        target_identity=target,
        role=Role.VIEWER,
        actor_identity=seeded_service["admin"],
        caller_identity=seeded_service["admin"],
        channel=AuditChannel.API,
    )

    assert unchanged.action == MembershipMutationAction.UNCHANGED
    assert unchanged.binding_id == granted.binding_id
    assert lock_modes == [False, True]
    with Session(engine) as session:
        assert session.scalar(
            select(func.count()).select_from(RoleBinding).where(
                RoleBinding.principal_id == target_ref.id,
                RoleBinding.task_id.is_(None),
            )
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(AuditEvent).where(
                AuditEvent.event_type == "workspace.membership_granted",
                AuditEvent.details["target_principal_id"].as_string() == str(target_ref.id),
            )
        ) == 1


def test_principal_with_membership_requires_revoke_before_delete(engine):
    issuer = "https://principal-lifecycle.example.test"
    last_admin_identity = ExternalIdentity(issuer, "last-admin")
    actor_identity = ExternalIdentity(issuer, "actor-admin")
    removable_identity = ExternalIdentity(issuer, "removable-admin")
    with Session(engine) as session, session.begin():
        last_admin = Principal(
            issuer=issuer,
            subject=last_admin_identity.subject,
            principal_type=PrincipalType.USER,
        )
        actor = Principal(
            issuer=issuer,
            subject=actor_identity.subject,
            principal_type=PrincipalType.USER,
        )
        removable = Principal(
            issuer=issuer,
            subject=removable_identity.subject,
            principal_type=PrincipalType.USER,
        )
        last_workspace = Workspace(slug="principal-last-admin", name="Principal Last Admin")
        shared_workspace = Workspace(slug="principal-shared-admin", name="Principal Shared Admin")
        session.add_all([last_admin, actor, removable, last_workspace, shared_workspace])
        session.flush()
        session.add_all(
            [
                RoleBinding(
                    workspace_id=last_workspace.id,
                    principal_id=last_admin.id,
                    role=Role.ADMIN,
                ),
                RoleBinding(
                    workspace_id=shared_workspace.id,
                    principal_id=actor.id,
                    role=Role.ADMIN,
                ),
                RoleBinding(
                    workspace_id=shared_workspace.id,
                    principal_id=removable.id,
                    role=Role.ADMIN,
                ),
            ]
        )
        last_admin_id = last_admin.id
        removable_id = removable.id

    principal_fk = next(iter(RoleBinding.__table__.c.principal_id.foreign_keys))
    database_fk = next(
        foreign_key
        for foreign_key in inspect(engine).get_foreign_keys("role_bindings")
        if foreign_key["constrained_columns"] == ["principal_id"]
    )
    assert principal_fk.ondelete == "RESTRICT"
    assert database_fk["options"]["ondelete"] == "RESTRICT"

    for principal_id in (last_admin_id, removable_id):
        with pytest.raises(DBAPIError):
            with engine.begin() as connection:
                connection.execute(delete(Principal).where(Principal.id == principal_id))
        with Session(engine) as session:
            assert session.get(Principal, principal_id) is not None
            assert session.scalar(
                select(func.count()).select_from(RoleBinding).where(
                    RoleBinding.principal_id == principal_id,
                )
            ) == 1

    service = DatabaseService(sessionmaker(bind=engine, class_=Session, expire_on_commit=False))
    revoked = service.revoke_workspace_membership(
        workspace_slug="principal-shared-admin",
        target_identity=removable_identity,
        actor_identity=actor_identity,
        caller_identity=actor_identity,
        channel=AuditChannel.API,
    )
    assert revoked.action == MembershipMutationAction.REVOKED

    with engine.begin() as connection:
        assert connection.execute(delete(Principal).where(Principal.id == removable_id)).rowcount == 1
    with Session(engine) as session:
        assert session.get(Principal, removable_id) is None
        assert session.scalar(
            select(func.count()).select_from(RoleBinding).where(
                RoleBinding.principal_id == removable_id,
            )
        ) == 0


def test_membership_audit_failure_rolls_back_mutation(seeded_service, engine, monkeypatch):
    service = seeded_service["service"]
    target = ExternalIdentity("https://access.example.test", "audit-failure-target")
    target_ref = service.resolve_or_provision(target)

    def fail_audit(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(service_module, "append_audit_event", fail_audit)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        service.grant_workspace_membership(
            workspace_slug="workspace-a",
            target_identity=target,
            role=Role.VIEWER,
            actor_identity=seeded_service["admin"],
            caller_identity=seeded_service["admin"],
            channel=AuditChannel.API,
        )

    with Session(engine) as session:
        assert session.scalar(
            select(func.count()).select_from(RoleBinding).where(
                RoleBinding.principal_id == target_ref.id,
                RoleBinding.task_id.is_(None),
            )
        ) == 0
        assert session.scalar(
            select(func.count()).select_from(AuditEvent).where(
                AuditEvent.event_type.like("workspace.membership_%"),
            )
        ) == 0


def test_last_workspace_admin_cannot_be_downgraded_or_deleted(seeded_service, engine):
    service = seeded_service["service"]
    admin = seeded_service["admin"]
    admin_id = service.resolve_identity(admin).id
    inactive_admin = ExternalIdentity("https://access.example.test", "inactive-admin")
    inactive_admin_ref = service.resolve_or_provision(inactive_admin)
    service.grant_workspace_membership(
        workspace_slug="workspace-a",
        target_identity=inactive_admin,
        role=Role.ADMIN,
        actor_identity=admin,
        caller_identity=admin,
        channel=AuditChannel.API,
    )
    with Session(engine) as session, session.begin():
        principal = session.get(Principal, inactive_admin_ref.id)
        principal.is_active = False

    with pytest.raises(LastWorkspaceAdmin):
        service.change_workspace_membership(
            workspace_slug="workspace-a",
            target_identity=admin,
            role=Role.VIEWER,
            actor_identity=admin,
            caller_identity=admin,
            channel=AuditChannel.API,
        )
    with pytest.raises(LastWorkspaceAdmin):
        service.revoke_workspace_membership(
            workspace_slug="workspace-a",
            target_identity=admin,
            actor_identity=admin,
            caller_identity=admin,
            channel=AuditChannel.API,
        )

    with pytest.raises(DBAPIError):
        with Session(engine) as session, session.begin():
            session.execute(
                update(RoleBinding)
                .where(RoleBinding.principal_id == admin_id, RoleBinding.task_id.is_(None))
                .values(role=Role.VIEWER)
            )
    with pytest.raises(DBAPIError):
        with Session(engine) as session, session.begin():
            session.execute(
                delete(RoleBinding).where(
                    RoleBinding.principal_id == admin_id,
                    RoleBinding.task_id.is_(None),
                )
            )

    revoked = service.revoke_workspace_membership(
        workspace_slug="workspace-a",
        target_identity=inactive_admin,
        actor_identity=admin,
        caller_identity=admin,
        channel=AuditChannel.API,
    )

    assert revoked.action == MembershipMutationAction.REVOKED
    assert revoked.principal.is_active is False
    assert service.authorize_workspace(admin, "workspace-a", Permission.WORKSPACE_MANAGE).allowed is True


def test_bootstrap_is_idempotent_and_audit_actor_is_structured(engine):
    with Session(engine) as session:
        first = bootstrap_admin(
            session,
            issuer="https://access.example.test",
            subject="admin-subject",
            workspace_slug="default",
            workspace_name="Default Workspace",
            email="admin@example.test",
        )
    with Session(engine) as session:
        second = bootstrap_admin(
            session,
            issuer="https://access.example.test",
            subject="admin-subject",
            workspace_slug="default",
            workspace_name="Default Workspace",
            email="admin@example.test",
        )

    assert first.changed is True
    assert second.changed is False
    assert first.principal_id == second.principal_id
    assert first.workspace_id == second.workspace_id
    assert first.role_binding_id == second.role_binding_id

    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Principal)) == 1
        assert session.scalar(select(func.count()).select_from(Workspace)) == 1
        assert session.scalar(select(func.count()).select_from(RoleBinding)) == 1
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 1
        event = session.scalar(select(AuditEvent))
        assert event.actor_type == AuditActorType.SYSTEM
        assert event.actor_principal_id is None
        assert event.caller_principal_id is None
        assert event.channel == AuditChannel.CLI
        assert "actor_email_snapshot" not in AuditEvent.__table__.columns
        assert event.details["granted_principal_id"] == str(first.principal_id)
        assert event.details["workspace_id"] == str(first.workspace_id)
        assert event.details["granted_role"] == Role.ADMIN.value


def test_facade_appends_audit_without_exposing_orm(seeded_service, engine):
    service = seeded_service["service"]
    with service.transaction() as transaction:
        decision = transaction.authorize_task(
            seeded_service["admin"],
            "workspace-a",
            "shared-key",
            Permission.TASK_PUBLISH,
        )
        transaction.require(decision)
        event_ref = transaction.append_audit(
            workspace_slug="workspace-a",
            event_type="task.published",
            actor_identity=seeded_service["admin"],
            caller_identity=seeded_service["mcp"],
            channel=AuditChannel.MCP,
            required_permission=Permission.TASK_PUBLISH,
            task_key="shared-key",
        )

    assert event_ref.actor_principal_id == decision.principal.id
    assert event_ref.caller_principal_id != event_ref.actor_principal_id
    assert event_ref.channel == AuditChannel.MCP
    with Session(engine) as session:
        event = session.get(AuditEvent, event_ref.id)
        assert event.resource_id == str(decision.task.id)


def test_audit_details_defaults_only_none_and_rejects_falsy_non_objects(seeded_service, engine):
    service = seeded_service["service"]
    event_ref = service.append_audit(
        workspace_slug="workspace-a",
        event_type="workspace.viewed",
        actor_identity=seeded_service["admin"],
        caller_identity=seeded_service["admin"],
        channel=AuditChannel.API,
        required_permission=Permission.AUDIT_VIEW,
        details=None,
    )
    with Session(engine) as session:
        assert session.get(AuditEvent, event_ref.id).details == {}

    for details in (False, 0, "", []):
        with pytest.raises(ValueError):
            service.append_audit(
                workspace_slug="workspace-a",
                event_type="workspace.viewed",
                actor_identity=seeded_service["admin"],
                caller_identity=seeded_service["admin"],
                channel=AuditChannel.API,
                required_permission=Permission.AUDIT_VIEW,
                details=details,
            )


def test_audit_facade_allows_actor_as_caller_and_rejects_inactive_service_caller(seeded_service, engine):
    service = seeded_service["service"]
    event = service.append_audit(
        workspace_slug="workspace-a",
        event_type="workspace.viewed",
        actor_identity=seeded_service["admin"],
        caller_identity=seeded_service["admin"],
        channel=AuditChannel.API,
        required_permission=Permission.AUDIT_VIEW,
    )
    assert event.actor_principal_id == event.caller_principal_id

    with Session(engine) as session, session.begin():
        worker = session.scalar(select(Principal).where(Principal.subject == "worker-service"))
        worker.is_active = False

    with pytest.raises(AuthorizationDenied) as inactive_service:
        service.append_audit(
            workspace_slug="workspace-a",
            event_type="workspace.viewed",
            actor_identity=seeded_service["admin"],
            caller_identity=seeded_service["worker"],
            channel=AuditChannel.WORKER,
            required_permission=Permission.AUDIT_VIEW,
        )
    assert inactive_service.value.decision.reason == AuthorizationReason.INVALID_AUDIT_CONTEXT


def test_audit_facade_rejects_cross_workspace_system_spoof_and_unknown_caller(seeded_service, engine):
    service = seeded_service["service"]
    with Session(engine) as session:
        initial_count = session.scalar(select(func.count()).select_from(AuditEvent))

    with pytest.raises(AuthorizationDenied) as cross_workspace:
        service.append_audit(
            workspace_slug="workspace-b",
            event_type="workspace.viewed",
            actor_identity=seeded_service["admin"],
            caller_identity=seeded_service["admin"],
            channel=AuditChannel.API,
            required_permission=Permission.AUDIT_VIEW,
        )
    with pytest.raises(AuthorizationDenied) as missing_workspace:
        service.append_audit(
            workspace_slug="missing-workspace",
            event_type="workspace.viewed",
            actor_identity=seeded_service["admin"],
            caller_identity=seeded_service["admin"],
            channel=AuditChannel.API,
            required_permission=Permission.AUDIT_VIEW,
        )
    with pytest.raises(AuthorizationDenied) as system_spoof:
        service.append_audit(
            workspace_slug="workspace-a",
            event_type="workspace.viewed",
            actor_identity=seeded_service["admin"],
            caller_identity=seeded_service["admin"],
            channel=AuditChannel.SYSTEM,
            required_permission=Permission.AUDIT_VIEW,
        )
    with pytest.raises(AuthorizationDenied) as null_actor:
        service.append_audit(
            workspace_slug="workspace-a",
            event_type="workspace.viewed",
            actor_identity=None,
            caller_identity=seeded_service["admin"],
            channel=AuditChannel.API,
            required_permission=Permission.AUDIT_VIEW,
        )
    with pytest.raises(AuthorizationDenied) as unknown_caller:
        service.append_audit(
            workspace_slug="workspace-a",
            event_type="workspace.viewed",
            actor_identity=seeded_service["admin"],
            caller_identity=ExternalIdentity("issuer", "unknown-caller"),
            channel=AuditChannel.API,
            required_permission=Permission.AUDIT_VIEW,
        )
    with pytest.raises(AuthorizationDenied) as user_caller:
        service.append_audit(
            workspace_slug="workspace-a",
            event_type="workspace.viewed",
            actor_identity=seeded_service["admin"],
            caller_identity=seeded_service["viewer"],
            channel=AuditChannel.API,
            required_permission=Permission.AUDIT_VIEW,
        )

    assert cross_workspace.value.decision.reason == AuthorizationReason.RESOURCE_NOT_VISIBLE
    assert missing_workspace.value.decision.reason == AuthorizationReason.RESOURCE_NOT_VISIBLE
    assert system_spoof.value.decision.reason == AuthorizationReason.INVALID_AUDIT_CONTEXT
    assert null_actor.value.decision.reason == AuthorizationReason.UNKNOWN_PRINCIPAL
    assert unknown_caller.value.decision.reason == AuthorizationReason.INVALID_AUDIT_CONTEXT
    assert user_caller.value.decision.reason == AuthorizationReason.INVALID_AUDIT_CONTEXT
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == initial_count


def test_task_draft_strong_etag_noop_and_aba_protection(seeded_service, engine):
    service = seeded_service["service"]
    definition_a = {"task_id": "shared-key", "labels": ["yes", "no"]}
    rendered_a = "task_id: shared-key\nlabels:\n  - yes\n  - no\n"

    with pytest.raises(TaskPreconditionRequired):
        service.save_task_draft(
            actor_identity=seeded_service["experimenter"],
            caller_identity=seeded_service["experimenter"],
            workspace_slug="workspace-a",
            task_key="shared-key",
            definition=definition_a,
            rendered_task=rendered_a,
            if_match=None,
            channel=AuditChannel.API,
        )

    with pytest.raises(TaskDraftConflict) as missing:
        service.save_task_draft(
            actor_identity=seeded_service["experimenter"],
            caller_identity=seeded_service["experimenter"],
            workspace_slug="workspace-a",
            task_key="shared-key",
            definition=definition_a,
            rendered_task=rendered_a,
            if_match="*",
            channel=AuditChannel.API,
        )
    assert missing.value.current_etag is None

    first = service.save_task_draft(
        actor_identity=seeded_service["experimenter"],
        caller_identity=seeded_service["experimenter"],
        workspace_slug="workspace-a",
        task_key="shared-key",
        definition=definition_a,
        rendered_task=rendered_a,
        if_match=None,
        channel=AuditChannel.API,
        if_none_match="*",
    )
    assert first.changed is True
    assert first.draft.version == 1
    assert first.draft.etag.startswith('"task-draft-v1-')
    assert first.draft.etag.endswith('"')
    assert not first.draft.etag.startswith("W/")

    with pytest.raises(TaskDraftConflict) as existing:
        service.save_task_draft(
            actor_identity=seeded_service["experimenter"],
            caller_identity=seeded_service["experimenter"],
            workspace_slug="workspace-a",
            task_key="shared-key",
            definition=definition_a,
            rendered_task=rendered_a,
            if_match=None,
            channel=AuditChannel.API,
            if_none_match="*",
        )
    assert existing.value.current_etag == first.draft.etag

    noop = service.save_task_draft(
        actor_identity=seeded_service["experimenter"],
        caller_identity=seeded_service["experimenter"],
        workspace_slug="workspace-a",
        task_key="shared-key",
        definition={"labels": ["yes", "no"], "task_id": "shared-key"},
        rendered_task=rendered_a,
        if_match="*",
        channel=AuditChannel.API,
    )
    assert noop.changed is False
    assert noop.draft == first.draft

    second = service.save_task_draft(
        actor_identity=seeded_service["experimenter"],
        caller_identity=seeded_service["experimenter"],
        workspace_slug="workspace-a",
        task_key="shared-key",
        definition={"task_id": "shared-key", "labels": ["yes", "no", "review"]},
        rendered_task="task_id: shared-key\nlabels:\n  - yes\n  - no\n  - review\n",
        if_match=first.draft.etag,
        channel=AuditChannel.API,
    )
    third = service.save_task_draft(
        actor_identity=seeded_service["experimenter"],
        caller_identity=seeded_service["experimenter"],
        workspace_slug="workspace-a",
        task_key="shared-key",
        definition=definition_a,
        rendered_task=rendered_a,
        if_match=second.draft.etag,
        channel=AuditChannel.API,
    )
    assert third.draft.version == 3
    assert third.draft.fingerprint == first.draft.fingerprint
    assert third.draft.etag != first.draft.etag

    with pytest.raises(TaskDraftConflict) as stale:
        service.save_task_draft(
            actor_identity=seeded_service["experimenter"],
            caller_identity=seeded_service["experimenter"],
            workspace_slug="workspace-a",
            task_key="shared-key",
            definition=definition_a,
            rendered_task=rendered_a,
            if_match=first.draft.etag,
            channel=AuditChannel.API,
        )
    assert stale.value.current_etag == third.draft.etag
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(TaskDraft)) == 1
        assert session.scalar(
            select(func.count()).select_from(AuditEvent).where(
                AuditEvent.event_type.in_(["task.draft_created", "task.draft_updated"]),
            )
        ) == 3


def test_task_writes_resolve_actor_and_caller_principals(seeded_service, engine):
    service = seeded_service["service"]
    saved = service.save_task_draft(
        actor_identity=seeded_service["experimenter"],
        caller_identity=seeded_service["mcp"],
        workspace_slug="workspace-a",
        task_key="shared-key",
        definition={"task_id": "shared-key"},
        rendered_task="task_id: shared-key\n",
        if_match=None,
        channel=AuditChannel.MCP,
        if_none_match="*",
    )
    assert saved.changed is True
    with Session(engine) as session:
        event_row = session.scalar(
            select(AuditEvent).where(AuditEvent.event_type == "task.draft_created"),
        )
        actor = session.get(Principal, event_row.actor_principal_id)
        caller = session.get(Principal, event_row.caller_principal_id)
        assert (actor.issuer, actor.subject) == (
            seeded_service["experimenter"].issuer,
            seeded_service["experimenter"].subject,
        )
        assert (caller.issuer, caller.subject) == (
            seeded_service["mcp"].issuer,
            seeded_service["mcp"].subject,
        )
        assert event_row.channel == AuditChannel.MCP

    with pytest.raises(AuthorizationDenied):
        service.save_task_draft(
            actor_identity=ExternalIdentity("untrusted", "principal-id-string"),
            caller_identity=seeded_service["mcp"],
            workspace_slug="workspace-a",
            task_key="shared-key",
            definition={"task_id": "shared-key", "version": 2},
            rendered_task="task_id: shared-key\nversion: 2\n",
            if_match=saved.draft.etag,
            channel=AuditChannel.MCP,
        )
    with pytest.raises(AuthorizationDenied):
        service.save_task_draft(
            actor_identity=seeded_service["experimenter"],
            caller_identity=seeded_service["viewer"],
            workspace_slug="workspace-a",
            task_key="shared-key",
            definition={"task_id": "shared-key", "version": 2},
            rendered_task="task_id: shared-key\nversion: 2\n",
            if_match=saved.draft.etag,
            channel=AuditChannel.MCP,
        )


def test_task_draft_and_publish_enforce_acl_and_preconditions(seeded_service):
    service = seeded_service["service"]
    draft = service.save_task_draft(
        actor_identity=seeded_service["experimenter"],
        caller_identity=seeded_service["experimenter"],
        workspace_slug="workspace-a",
        task_key="shared-key",
        definition={"task_id": "shared-key"},
        rendered_task="task_id: shared-key\n",
        if_match=None,
        channel=AuditChannel.API,
        if_none_match="*",
    ).draft

    with pytest.raises(AuthorizationDenied):
        service.get_task_draft(
            identity=seeded_service["viewer"],
            workspace_slug="workspace-a",
            task_key="shared-key",
        )
    with pytest.raises(AuthorizationDenied):
        service.publish_task_draft(
            actor_identity=seeded_service["viewer"],
            caller_identity=seeded_service["viewer"],
            workspace_slug="workspace-a",
            task_key="shared-key",
            if_match=draft.etag,
            reason="Unauthorized release",
            idempotency_key="viewer-publish",
            channel=AuditChannel.API,
        )
    with pytest.raises(TaskPreconditionRequired):
        service.publish_task_draft(
            actor_identity=seeded_service["experimenter"],
            caller_identity=seeded_service["experimenter"],
            workspace_slug="workspace-a",
            task_key="shared-key",
            if_match=None,
            reason="Missing precondition",
            idempotency_key="missing-precondition",
            channel=AuditChannel.API,
        )
    with pytest.raises(ValueError, match="reason"):
        service.publish_task_draft(
            actor_identity=seeded_service["experimenter"],
            caller_identity=seeded_service["experimenter"],
            workspace_slug="workspace-a",
            task_key="shared-key",
            if_match=draft.etag,
            reason="   ",
            idempotency_key="missing-reason",
            channel=AuditChannel.API,
        )


def test_task_publish_is_atomic_immutable_and_replays_original_snapshot(seeded_service, engine):
    service = seeded_service["service"]
    first_draft = service.save_task_draft(
        actor_identity=seeded_service["experimenter"],
        caller_identity=seeded_service["mcp"],
        workspace_slug="workspace-a",
        task_key="shared-key",
        definition={"task_id": "shared-key", "labels": ["yes", "no"]},
        rendered_task="task_id: shared-key\nlabels:\n  - yes\n  - no\n",
        if_match=None,
        channel=AuditChannel.MCP,
        if_none_match="*",
    ).draft
    published = service.publish_task_draft(
        actor_identity=seeded_service["experimenter"],
        caller_identity=seeded_service["mcp"],
        workspace_slug="workspace-a",
        task_key="shared-key",
        if_match=first_draft.etag,
        reason="Initial controlled release",
        idempotency_key="publish-shared-key-v1",
        channel=AuditChannel.MCP,
        request_id="request-1",
    )
    assert published.response_status == 202
    assert published.replayed is False
    assert published.draft_version == 1
    assert published.draft_fingerprint == first_draft.fingerprint
    assert published.materialization_state == TaskMaterializationState.PENDING

    revision = service.get_task_revision(
        identity=seeded_service["experimenter"],
        workspace_slug="workspace-a",
        task_key="shared-key",
        revision_id=published.revision_id,
    )
    assert revision is not None
    assert revision.definition == first_draft.definition
    assert revision.rendered_task == first_draft.rendered_task
    assert revision.reason == "Initial controlled release"
    assert revision.channel == AuditChannel.MCP
    assert revision.previous_revision_id is None
    assert revision.content_hash == first_draft.fingerprint

    with Session(engine) as session:
        task = session.get(Task, published.task_id)
        persisted_revision = session.get(TaskRevision, published.revision_id)
        materialization = session.get(TaskRevisionMaterialization, published.materialization_id)
        claim = session.get(IdempotencyRecord, persisted_revision.idempotency_record_id)
        actor = session.get(Principal, persisted_revision.actor_principal_id)
        caller = session.get(Principal, persisted_revision.caller_principal_id)
        assert task.current_revision_id is None
        assert materialization.state == TaskMaterializationState.PENDING
        assert materialization.claimed_at is None
        assert materialization.lease_expires_at is None
        assert claim.idempotency_key_hash == hashlib.sha256(b"publish-shared-key-v1").hexdigest()
        assert "publish-shared-key-v1" not in str(claim.response_body)
        assert actor.subject == seeded_service["experimenter"].subject
        assert caller.subject == seeded_service["mcp"].subject
        assert session.scalar(
            select(func.count()).select_from(AuditEvent).where(
                AuditEvent.event_type == "task.revision_published",
            )
        ) == 1

    second_draft = service.save_task_draft(
        actor_identity=seeded_service["experimenter"],
        caller_identity=seeded_service["experimenter"],
        workspace_slug="workspace-a",
        task_key="shared-key",
        definition={"task_id": "shared-key", "labels": ["yes", "no", "review"]},
        rendered_task="task_id: shared-key\nlabels:\n  - yes\n  - no\n  - review\n",
        if_match=first_draft.etag,
        channel=AuditChannel.API,
    ).draft
    assert second_draft.version == 2

    with pytest.raises(TaskDraftConflict):
        service.publish_task_draft(
            actor_identity=seeded_service["experimenter"],
            caller_identity=seeded_service["mcp"],
            workspace_slug="workspace-a",
            task_key="shared-key",
            if_match=first_draft.etag,
            reason="Stale draft release",
            idempotency_key="stale-publish-attempt",
            channel=AuditChannel.MCP,
        )
    with Session(engine) as session:
        assert session.scalar(
            select(func.count()).select_from(IdempotencyRecord).where(
                IdempotencyRecord.idempotency_key_hash
                == hashlib.sha256(b"stale-publish-attempt").hexdigest(),
            )
        ) == 0

    replay = service.publish_task_draft(
        actor_identity=seeded_service["experimenter"],
        caller_identity=seeded_service["mcp"],
        workspace_slug="workspace-a",
        task_key="shared-key",
        if_match=first_draft.etag,
        reason="Initial controlled release",
        idempotency_key="publish-shared-key-v1",
        channel=AuditChannel.MCP,
        request_id="request-2",
    )
    assert replay.replayed is True
    assert replay.revision_id == published.revision_id
    assert replay.materialization_id == published.materialization_id
    assert replay.draft_version == published.draft_version
    assert replay.draft_fingerprint == published.draft_fingerprint

    with pytest.raises(IdempotencyConflict):
        service.publish_task_draft(
            actor_identity=seeded_service["experimenter"],
            caller_identity=seeded_service["mcp"],
            workspace_slug="workspace-a",
            task_key="shared-key",
            if_match=first_draft.etag,
            reason="Different reason",
            idempotency_key="publish-shared-key-v1",
            channel=AuditChannel.MCP,
        )
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(TaskRevision)) == 1
        assert session.scalar(select(func.count()).select_from(TaskRevisionMaterialization)) == 1

    with Session(engine) as session, session.begin():
        task = session.get(Task, published.task_id)
        task.current_revision_id = published.revision_id
    second_publish = service.publish_task_draft(
        actor_identity=seeded_service["experimenter"],
        caller_identity=seeded_service["experimenter"],
        workspace_slug="workspace-a",
        task_key="shared-key",
        if_match=second_draft.etag,
        reason="Second controlled release",
        idempotency_key="publish-shared-key-v2",
        channel=AuditChannel.API,
    )
    assert second_publish.previous_revision_id == published.revision_id
    with Session(engine) as session:
        task = session.get(Task, published.task_id)
        assert task.current_revision_id == published.revision_id
        assert task.current_revision_id != second_publish.revision_id
        assert session.scalar(select(func.count()).select_from(TaskRevision)) == 2
        assert session.scalar(select(func.count()).select_from(TaskRevisionMaterialization)) == 2

    with pytest.raises(DBAPIError):
        with Session(engine) as session, session.begin():
            session.execute(
                update(TaskRevision)
                .where(TaskRevision.id == published.revision_id)
                .values(reason="overwritten"),
            )
    with pytest.raises(DBAPIError):
        with Session(engine) as session, session.begin():
            session.execute(delete(TaskRevision).where(TaskRevision.id == published.revision_id))


def test_task_lifecycle_is_acl_idempotent_audited_and_restorable(seeded_service, engine):
    service = seeded_service["service"]
    common = {
        "workspace_slug": "workspace-a",
        "task_key": "shared-key",
        "actor_identity": seeded_service["experimenter"],
        "caller_identity": seeded_service["experimenter"],
        "channel": AuditChannel.API,
    }

    with pytest.raises(TaskLifecycleReasonRequired):
        service.disable_task(**common, reason=" ", idempotency_key="lifecycle-blank")

    disabled = service.disable_task(**common, reason="pause intake", idempotency_key="lifecycle-disable")
    replay = service.disable_task(**common, reason="pause intake", idempotency_key="lifecycle-disable")
    assert disabled.previous_state == TaskLifecycleState.ACTIVE
    assert disabled.lifecycle_state == TaskLifecycleState.DISABLED
    assert disabled.changed is True
    assert replay.replayed is True
    assert replay.audit_event_id == disabled.audit_event_id

    archived = service.archive_task(**common, reason="retention complete", idempotency_key="lifecycle-archive")
    assert archived.previous_state == TaskLifecycleState.DISABLED
    assert archived.lifecycle_state == TaskLifecycleState.ARCHIVED

    with pytest.raises(TaskLifecycleTransitionInvalid):
        service.disable_task(**common, reason="invalid", idempotency_key="lifecycle-invalid")

    with pytest.raises(AuthorizationDenied):
        service.restore_task(
            **{**common, "actor_identity": seeded_service["viewer"], "caller_identity": seeded_service["viewer"]},
            reason="viewer restore",
            idempotency_key="lifecycle-viewer",
        )

    restored = service.restore_task(**common, reason="reopen task", idempotency_key="lifecycle-restore")
    assert restored.previous_state == TaskLifecycleState.ARCHIVED
    assert restored.lifecycle_state == TaskLifecycleState.ACTIVE

    with Session(engine) as session:
        task = session.scalar(select(Task).where(Task.task_key == "shared-key"))
        assert task.lifecycle_state == TaskLifecycleState.ACTIVE
        events = session.scalars(
            select(AuditEvent).where(
                AuditEvent.resource_id == str(task.id),
                AuditEvent.event_type.like("task.lifecycle_%"),
            )
        ).all()
        assert [event.details["reason"] for event in events] == [
            "pause intake",
            "retention complete",
            "reopen task",
        ]
        assert session.scalar(
            select(func.count()).select_from(IdempotencyRecord).where(
                IdempotencyRecord.operation == "task.lifecycle",
                IdempotencyRecord.resource_id == task.id,
            )
        ) == 3


def test_audit_events_are_append_only_in_orm(engine):
    with Session(engine) as session:
        result = bootstrap_admin(
            session,
            issuer="issuer",
            subject="subject",
            workspace_slug="workspace",
            workspace_name="Workspace",
        )

    with pytest.raises(ImmutableAuditEventError):
        with Session(engine) as session, session.begin():
            event = session.scalar(select(AuditEvent))
            event.event_type = "overwritten"

    with pytest.raises(ImmutableAuditEventError):
        with Session(engine) as session, session.begin():
            event = session.scalar(select(AuditEvent))
            session.delete(event)

    with pytest.raises(DBAPIError):
        with Session(engine) as session, session.begin():
            session.execute(update(AuditEvent).values(event_type="bulk-overwritten"))

    with pytest.raises(DBAPIError):
        with Session(engine) as session, session.begin():
            session.execute(delete(AuditEvent))

    with Session(engine) as session:
        assert session.scalar(select(AuditEvent.workspace_id)) == result.workspace_id


def test_authorization_database_errors_fail_closed(tmp_path: Path):
    database_path = tmp_path / "missing-schema.db"
    engine = create_engine(f"sqlite+pysqlite:///{database_path}")
    service = DatabaseService(sessionmaker(bind=engine, class_=Session))

    with pytest.raises(AuthorizationUnavailable):
        service.authorize_task(
            ExternalIdentity("issuer", "subject"),
            "workspace",
            "task",
            Permission.TASK_READ,
        )

    engine.dispose()
