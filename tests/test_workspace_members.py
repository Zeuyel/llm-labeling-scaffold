from __future__ import annotations

import os

import pytest
from sqlalchemy import func, inspect, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from llm_labeling_scaffold.db import (
    AuditChannel,
    AuthorizationDenied,
    AuthorizationReason,
    DatabaseService,
    ExternalIdentity,
    LastWorkspaceAdmin,
    MembershipMutationAction,
    Permission,
    PrincipalType,
    Role,
    WorkspaceInvitationStatus,
)
from llm_labeling_scaffold.db.base import Base
from llm_labeling_scaffold.db.database import create_database_engine
from llm_labeling_scaffold.db.models import AuditEvent, Principal, RoleBinding, Workspace, WorkspaceInvitation


@pytest.fixture
def member_database():
    engine = create_database_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    issuer = "https://members.example.test"
    admin = ExternalIdentity(issuer, "admin", email_snapshot="admin@example.test")
    viewer = ExternalIdentity(issuer, "viewer", email_snapshot="viewer@example.test")
    with Session(engine) as session, session.begin():
        admin_principal = Principal(
            issuer=admin.issuer,
            subject=admin.subject,
            principal_type=PrincipalType.USER,
            email_snapshot=admin.email_snapshot,
        )
        viewer_principal = Principal(
            issuer=viewer.issuer,
            subject=viewer.subject,
            principal_type=PrincipalType.USER,
            email_snapshot=viewer.email_snapshot,
        )
        workspace = Workspace(slug="members-a", name="Members A")
        other_workspace = Workspace(slug="members-b", name="Members B")
        session.add_all([admin_principal, viewer_principal, workspace, other_workspace])
        session.flush()
        session.add_all(
            [
                RoleBinding(
                    workspace_id=workspace.id,
                    principal_id=admin_principal.id,
                    role=Role.ADMIN,
                ),
                RoleBinding(
                    workspace_id=workspace.id,
                    principal_id=viewer_principal.id,
                    role=Role.VIEWER,
                ),
            ]
        )
    service = DatabaseService(sessionmaker(bind=engine, expire_on_commit=False))
    try:
        yield {
            "engine": engine,
            "service": service,
            "admin": admin,
            "viewer": viewer,
            "issuer": issuer,
        }
    finally:
        engine.dispose()


def test_pending_invitation_claim_requires_verified_email_and_is_identity_bound(member_database):
    service = member_database["service"]
    engine = member_database["engine"]
    admin = member_database["admin"]
    invite = service.create_workspace_invitation(
        workspace_slug="members-a",
        email="Invitee@Example.test",
        role=Role.ANNOTATOR,
        actor_identity=admin,
        caller_identity=admin,
        idempotency_key="invite-1",
        channel=AuditChannel.PANEL,
    )
    assert invite.invitation.status == WorkspaceInvitationStatus.PENDING
    assert invite.claim_status == "pending"
    replay = service.create_workspace_invitation(
        workspace_slug="members-a",
        email="invitee@example.test",
        role=Role.ANNOTATOR,
        actor_identity=admin,
        caller_identity=admin,
        idempotency_key="invite-1",
        channel=AuditChannel.PANEL,
    )
    assert replay.replayed is True
    assert replay.invitation.id == invite.invitation.id
    assert replay.claim_status == "pending"

    no_email = ExternalIdentity(member_database["issuer"], "no-email")
    no_email_invite = service.create_workspace_invitation(
        workspace_slug="members-a",
        email="no-email@example.test",
        role=Role.VIEWER,
        actor_identity=admin,
        caller_identity=admin,
        idempotency_key="invite-no-email",
        channel=AuditChannel.PANEL,
    )
    no_email_session = service.get_session(no_email)
    assert no_email_session.principal is None
    assert no_email_session.workspaces == ()
    with Session(engine) as session:
        assert session.get(WorkspaceInvitation, no_email_invite.invitation.id).status == (
            WorkspaceInvitationStatus.PENDING
        )

    invitee = ExternalIdentity(
        member_database["issuer"],
        "invitee-subject",
        email_snapshot="invitee@example.test",
    )
    claimed = service.get_session(invitee)
    assert claimed.principal is not None
    assert [item.workspace.slug for item in claimed.workspaces] == ["members-a"]
    assert claimed.workspaces[0].roles == (Role.ANNOTATOR,)
    assert len(claimed.invitation_claims) == 1
    assert claimed.invitation_claims[0].membership_created is True

    repeated = service.get_session(invitee)
    assert repeated.workspaces == claimed.workspaces
    assert repeated.invitation_claims == ()

    same_email_other_subject = service.get_session(
        ExternalIdentity(
            member_database["issuer"],
            "different-subject",
            email_snapshot="invitee@example.test",
        )
    )
    assert same_email_other_subject.principal is None
    assert same_email_other_subject.workspaces == ()
    with Session(engine) as session:
        assert session.scalar(
            select(func.count()).where(AuditEvent.event_type == "workspace.invitation_claimed")
        ) == 1
        invitation = session.get(WorkspaceInvitation, invite.invitation.id)
        assert invitation.status == WorkspaceInvitationStatus.CLAIMED
        assert invitation.claimed_principal_id == claimed.principal.id


def test_member_management_is_scoped_idempotent_and_protects_last_admin(member_database):
    service = member_database["service"]
    admin = member_database["admin"]
    target = ExternalIdentity(
        member_database["issuer"],
        "target-subject",
        email_snapshot="target@example.test",
    )
    target_ref = service.resolve_or_provision(target)
    granted = service.grant_workspace_membership(
        workspace_slug="members-a",
        target_identity=target,
        role=Role.VIEWER,
        actor_identity=admin,
        caller_identity=admin,
        channel=AuditChannel.API,
    )
    changed = service.change_workspace_membership_by_principal_id(
        workspace_slug="members-a",
        principal_id=target_ref.id,
        role=Role.EXPERIMENTER,
        actor_identity=admin,
        caller_identity=admin,
        idempotency_key="role-1",
        channel=AuditChannel.PANEL,
    )
    assert changed.result.action == MembershipMutationAction.CHANGED
    replay = service.change_workspace_membership_by_principal_id(
        workspace_slug="members-a",
        principal_id=target_ref.id,
        role=Role.EXPERIMENTER,
        actor_identity=admin,
        caller_identity=admin,
        idempotency_key="role-1",
        channel=AuditChannel.PANEL,
    )
    assert replay.replayed is True
    assert replay.result.role == Role.EXPERIMENTER

    revoked = service.revoke_workspace_membership_by_principal_id(
        workspace_slug="members-a",
        principal_id=target_ref.id,
        actor_identity=admin,
        caller_identity=admin,
        idempotency_key="revoke-1",
        channel=AuditChannel.PANEL,
    )
    assert revoked.result.action == MembershipMutationAction.REVOKED
    assert service.authorize_workspace(target, "members-a", Permission.AUDIT_VIEW).reason == (
        AuthorizationReason.RESOURCE_NOT_VISIBLE
    )
    revoke_replay = service.revoke_workspace_membership_by_principal_id(
        workspace_slug="members-a",
        principal_id=target_ref.id,
        actor_identity=admin,
        caller_identity=admin,
        idempotency_key="revoke-1",
        channel=AuditChannel.PANEL,
    )
    assert revoke_replay.replayed is True
    assert revoke_replay.result.action == MembershipMutationAction.REVOKED

    admin_id = service.resolve_identity(admin).id
    with pytest.raises(LastWorkspaceAdmin):
        service.change_workspace_membership_by_principal_id(
            workspace_slug="members-a",
            principal_id=admin_id,
            role=Role.VIEWER,
            actor_identity=admin,
            caller_identity=admin,
            idempotency_key="last-admin-role",
            channel=AuditChannel.PANEL,
        )

    with pytest.raises(AuthorizationDenied) as denied:
        service.list_workspace_members(
            identity=member_database["viewer"],
            workspace_slug="members-a",
        )
    assert denied.value.decision.reason == AuthorizationReason.ROLE_DENIED
    with pytest.raises(AuthorizationDenied) as hidden:
        service.list_workspace_members(identity=admin, workspace_slug="members-b")
    assert hidden.value.decision.reason == AuthorizationReason.RESOURCE_NOT_VISIBLE
    assert granted.action == MembershipMutationAction.GRANTED


def test_invitation_claim_keeps_pending_on_role_conflict(member_database):
    service = member_database["service"]
    admin = member_database["admin"]
    target = ExternalIdentity(
        member_database["issuer"],
        "conflicting-subject",
        email_snapshot="conflict@example.test",
    )
    target_ref = service.resolve_or_provision(target)
    service.grant_workspace_membership(
        workspace_slug="members-a",
        target_identity=target,
        role=Role.VIEWER,
        actor_identity=admin,
        caller_identity=admin,
        channel=AuditChannel.API,
    )
    invitation = service.create_workspace_invitation(
        workspace_slug="members-a",
        email="conflict@example.test",
        role=Role.ADMIN,
        actor_identity=admin,
        caller_identity=admin,
        idempotency_key="invite-conflict",
        channel=AuditChannel.PANEL,
    )

    session = service.get_session(target)
    assert session.workspaces[0].roles == (Role.VIEWER,)
    assert session.invitation_claims[0].claim_status == "role_conflict"
    assert session.invitation_claims[0].membership_created is False
    with Session(member_database["engine"]) as db_session:
        stored = db_session.get(WorkspaceInvitation, invitation.invitation.id)
        assert stored.status == WorkspaceInvitationStatus.PENDING
        assert stored.claimed_principal_id is None
        assert db_session.scalar(
            select(func.count()).where(
                RoleBinding.principal_id == target_ref.id,
                RoleBinding.workspace_id == stored.workspace_id,
                RoleBinding.task_id.is_(None),
                RoleBinding.role == Role.VIEWER,
            )
        ) == 1


def test_idempotent_self_role_downgrade_completes_after_authority_changes(member_database):
    service = member_database["service"]
    admin = member_database["admin"]
    viewer = member_database["viewer"]
    viewer_id = service.resolve_identity(viewer).id
    service.change_workspace_membership_by_principal_id(
        workspace_slug="members-a",
        principal_id=viewer_id,
        role=Role.ADMIN,
        actor_identity=admin,
        caller_identity=admin,
        idempotency_key="promote-viewer-for-self-change",
        channel=AuditChannel.PANEL,
    )
    admin_id = service.resolve_identity(admin).id

    changed = service.change_workspace_membership_by_principal_id(
        workspace_slug="members-a",
        principal_id=admin_id,
        role=Role.VIEWER,
        actor_identity=admin,
        caller_identity=admin,
        idempotency_key="self-downgrade",
        channel=AuditChannel.PANEL,
    )

    assert changed.result.action == MembershipMutationAction.CHANGED
    assert changed.result.role == Role.VIEWER
    assert service.authorize_workspace(admin, "members-a", Permission.WORKSPACE_MANAGE).allowed is False


def test_idempotent_self_revoke_completes_after_authority_changes(member_database):
    service = member_database["service"]
    admin = member_database["admin"]
    viewer = member_database["viewer"]
    viewer_id = service.resolve_identity(viewer).id
    service.change_workspace_membership_by_principal_id(
        workspace_slug="members-a",
        principal_id=viewer_id,
        role=Role.ADMIN,
        actor_identity=admin,
        caller_identity=admin,
        idempotency_key="promote-viewer-for-self-revoke",
        channel=AuditChannel.PANEL,
    )
    admin_id = service.resolve_identity(admin).id

    revoked = service.revoke_workspace_membership_by_principal_id(
        workspace_slug="members-a",
        principal_id=admin_id,
        actor_identity=admin,
        caller_identity=admin,
        idempotency_key="self-revoke",
        channel=AuditChannel.PANEL,
    )

    assert revoked.result.action == MembershipMutationAction.REVOKED
    assert service.authorize_workspace(admin, "members-a", Permission.AUDIT_VIEW).reason == (
        AuthorizationReason.RESOURCE_NOT_VISIBLE
    )


@pytest.mark.skipif(
    not os.environ.get("LLS_TEST_POSTGRES_URL"),
    reason="LLS_TEST_POSTGRES_URL is not set",
)
def test_postgres_invitation_table_and_identity_claim_adapter():
    from llm_labeling_scaffold.db.migration import upgrade_database

    database_url = os.environ["LLS_TEST_POSTGRES_URL"]
    upgrade_database(database_url, "head")
    engine = create_database_engine(database_url)
    try:
        assert "workspace_invitations" in set(inspect(engine).get_table_names())
        with engine.connect() as connection:
            assert connection.scalar(
                select(func.count()).select_from(WorkspaceInvitation)
            ) >= 0
    finally:
        engine.dispose()
