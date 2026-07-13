from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, delete, event, func, select, update
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
    IdentityTypeConflict,
    Permission,
    PrincipalType,
    Role,
)
from llm_labeling_scaffold.db.base import Base
from llm_labeling_scaffold.db.bootstrap import bootstrap_admin
from llm_labeling_scaffold.db.database import create_database_engine, resolve_database_url
from llm_labeling_scaffold.db.enums import IdempotencyState, TaskStatus
from llm_labeling_scaffold.db.models import (
    AuditEvent,
    IdempotencyRecord,
    ImmutableAuditEventError,
    Principal,
    RoleBinding,
    Task,
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
        workspace_a = Workspace(slug="workspace-a", name="Workspace A")
        workspace_b = Workspace(slug="workspace-b", name="Workspace B")
        session.add_all([viewer, annotator, experimenter, admin, service_principal, workspace_a, workspace_b])
        session.flush()

        task_a = Task(
            workspace_id=workspace_a.id,
            task_key="shared-key",
            name="Task A",
            status=TaskStatus.PUBLISHED,
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
            task_key="shared-key",
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
    assert not service.authorize_task(
        seeded_service["annotator"],
        "workspace-a",
        "other-task",
        Permission.TASK_READ,
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
    assert not service.authorize_workspace(experimenter, "workspace-a", Permission.WORKSPACE_MANAGE).allowed

    admin = seeded_service["admin"]
    assert service.authorize_workspace(admin, "workspace-a", Permission.WORKSPACE_MANAGE).allowed
    cross_workspace = service.authorize_task(admin, "workspace-b", "shared-key", Permission.TASK_READ)
    assert cross_workspace.allowed is False
    assert cross_workspace.reason == AuthorizationReason.RESOURCE_NOT_VISIBLE
    assert cross_workspace.workspace is None
    assert cross_workspace.task is None


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
    assert service.authorize_workspace(first_identity, "workspace-a", Permission.TASK_READ).reason == (
        AuthorizationReason.RESOURCE_NOT_VISIBLE
    )
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Principal)) == 7
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
    assert annotator_workspace.roles == ()
    assert annotator_workspace.capabilities == ()
    annotator_tasks = service.list_authorized_tasks(
        seeded_service["annotator"],
        "workspace-a",
        limit=10,
    )
    assert [task.task.task_key for task in annotator_tasks.items] == ["shared-key"]
    assert annotator_tasks.items[0].capabilities == (
        Permission.TASK_READ,
        Permission.ANNOTATION_WORK,
    )

    viewer_workspace = service.get_session(seeded_service["viewer"]).workspaces[0]
    assert viewer_workspace.roles == (Role.VIEWER,)
    assert viewer_workspace.capabilities == (Permission.TASK_READ,)


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


def test_task_key_and_task_acl_are_scoped_to_workspace(seeded_service, engine):
    with Session(engine) as session:
        assert session.scalar(
            select(func.count()).select_from(Task).where(Task.task_key == "shared-key"),
        ) == 2
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
        session.add(
            IdempotencyRecord(
                workspace_id=workspace_id,
                actor_principal_id=actor_id,
                caller_principal_id=caller_id,
                operation="task.publish",
                idempotency_key="request-1",
                request_fingerprint="fingerprint-a",
                state=IdempotencyState.PENDING,
            )
        )

    with pytest.raises(IntegrityError):
        with Session(engine) as session, session.begin():
            workspace_id = session.scalar(select(Workspace.id).where(Workspace.slug == "workspace-a"))
            different_actor_id = session.scalar(select(Principal.id).where(Principal.subject == "viewer-1"))
            different_caller_id = session.scalar(select(Principal.id).where(Principal.subject == "admin-1"))
            session.add(
                IdempotencyRecord(
                    workspace_id=workspace_id,
                    actor_principal_id=different_actor_id,
                    caller_principal_id=different_caller_id,
                    operation="task.publish",
                    idempotency_key="request-1",
                    request_fingerprint="fingerprint-b",
                    state=IdempotencyState.PENDING,
                )
            )

    with Session(engine) as session:
        record = session.scalar(select(IdempotencyRecord))
        assert record.request_fingerprint == "fingerprint-a"
        assert record.actor_principal_id != record.caller_principal_id


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
        assert event.actor_principal_id == first.principal_id
        assert event.caller_principal_id == first.principal_id
        assert event.channel == AuditChannel.CLI
        assert event.actor_email_snapshot == "admin@example.test"


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
            resource_type="task",
            resource_id=decision.task.id,
        )

    assert event_ref.actor_principal_id == decision.principal.id
    assert event_ref.caller_principal_id != event_ref.actor_principal_id
    assert event_ref.channel == AuditChannel.MCP
    with Session(engine) as session:
        event = session.get(AuditEvent, event_ref.id)
        assert event.resource_id == str(decision.task.id)


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
