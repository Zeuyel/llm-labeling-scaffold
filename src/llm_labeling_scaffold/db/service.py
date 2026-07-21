from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from sqlalchemy import Engine, and_, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from .audit import append_audit_event
from .database import create_database_engine, create_session_factory
from .enums import (
    AuditChannel,
    IdempotencyState,
    PrincipalType,
    Role,
    TaskLifecycleState,
    TaskMaterializationState,
    WorkspaceInvitationStatus,
)
from .models import (
    AuditEvent,
    IdempotencyRecord,
    Principal,
    RoleBinding,
    Task,
    TaskDraft,
    TaskRevision,
    TaskRevisionMaterialization,
    Workspace,
    WorkspaceInvitation,
)
from .idempotency_boundary import sqlite_idempotency_completion_gate
from .rbac import TASK_PERMISSIONS, WORKSPACE_PERMISSIONS, Permission, role_allows
from .sensitive_json import canonical_sensitive_json_bytes, normalize_sensitive_json_object


MAX_AUTHORIZED_TASKS_PAGE_SIZE = 100
DEFAULT_INVITATION_TTL = timedelta(days=7)
_IDEMPOTENCY_OPERATION = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,254}\Z", re.ASCII)
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\Z", re.ASCII)
_TASK_LIFECYCLE_TRANSITIONS = {
    TaskLifecycleState.ACTIVE: frozenset({TaskLifecycleState.DISABLED, TaskLifecycleState.ARCHIVED}),
    TaskLifecycleState.DISABLED: frozenset({TaskLifecycleState.ACTIVE, TaskLifecycleState.ARCHIVED}),
    TaskLifecycleState.ARCHIVED: frozenset({TaskLifecycleState.ACTIVE}),
}


class AuthorizationReason(str, Enum):
    ALLOWED = "allowed"
    UNKNOWN_PRINCIPAL = "unknown_principal"
    INACTIVE_PRINCIPAL = "inactive_principal"
    RESOURCE_NOT_VISIBLE = "resource_not_visible"
    INACTIVE_WORKSPACE = "inactive_workspace"
    INVALID_PERMISSION_SCOPE = "invalid_permission_scope"
    INVALID_AUDIT_CONTEXT = "invalid_audit_context"
    ROLE_DENIED = "role_denied"


@dataclass(frozen=True)
class ExternalIdentity:
    issuer: str
    subject: str
    email_snapshot: str | None = None
    display_name_snapshot: str | None = None

    def __post_init__(self) -> None:
        if not self.issuer.strip() or not self.subject.strip():
            raise ValueError("issuer and subject must not be blank")


@dataclass(frozen=True)
class PrincipalRef:
    id: uuid.UUID
    issuer: str
    subject: str
    principal_type: PrincipalType
    display_name: str | None
    email_snapshot: str | None
    is_active: bool


@dataclass(frozen=True)
class WorkspaceRef:
    id: uuid.UUID
    slug: str
    name: str


@dataclass(frozen=True)
class TaskRef:
    id: uuid.UUID
    workspace_id: uuid.UUID
    task_key: str
    current_revision_id: uuid.UUID | None = None
    draft_version: int | None = None
    draft_fingerprint: str | None = None
    lifecycle_state: TaskLifecycleState = TaskLifecycleState.ACTIVE


@dataclass(frozen=True)
class TaskAccess:
    task: TaskRef
    roles: tuple[Role, ...]
    capabilities: tuple[Permission, ...]


@dataclass(frozen=True)
class WorkspaceAccess:
    workspace: WorkspaceRef
    roles: tuple[Role, ...]
    workspace_capabilities: tuple[Permission, ...]
    task_capabilities: tuple[Permission, ...]


@dataclass(frozen=True)
class TaskAccessPage:
    workspace: WorkspaceRef | None
    items: tuple[TaskAccess, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class AuthorizationSession:
    principal: PrincipalRef | None
    workspaces: tuple[WorkspaceAccess, ...]
    invitation_claims: tuple["InvitationClaimRef", ...] = ()


@dataclass(frozen=True)
class AuthorizationDecision:
    allowed: bool
    reason: AuthorizationReason
    permission: Permission
    principal: PrincipalRef | None
    workspace: WorkspaceRef | None
    task: TaskRef | None
    roles: tuple[Role, ...]


@dataclass(frozen=True)
class AuditEventRef:
    id: uuid.UUID
    workspace_id: uuid.UUID
    actor_principal_id: uuid.UUID | None
    caller_principal_id: uuid.UUID | None
    channel: AuditChannel
    event_type: str


class IdempotencyClaimStatus(str, Enum):
    CLAIMED = "claimed"
    PENDING = "pending"
    REPLAY = "replay"


@dataclass(frozen=True)
class IdempotencyClaim:
    record_id: uuid.UUID
    workspace_id: uuid.UUID
    actor_principal_id: uuid.UUID
    caller_principal_id: uuid.UUID
    operation: str
    idempotency_key_hash: str
    request_fingerprint: str
    required_permission: Permission
    resource_type: str
    resource_id: uuid.UUID
    channel: AuditChannel
    status: IdempotencyClaimStatus
    state: IdempotencyState
    response_status: int | None
    response_body: dict[str, Any] | None


@dataclass(frozen=True)
class TaskDraftRef:
    id: uuid.UUID
    workspace_id: uuid.UUID
    task_id: uuid.UUID
    version: int
    fingerprint: str
    etag: str
    definition: dict[str, Any]
    rendered_task: str


@dataclass(frozen=True)
class TaskDraftSaveResult:
    draft: TaskDraftRef
    changed: bool


@dataclass(frozen=True)
class TaskCreateResult:
    workspace_id: uuid.UUID
    task_id: uuid.UUID
    task_key: str
    draft: TaskDraftRef
    response_status: int
    replayed: bool


@dataclass(frozen=True)
class TaskRevisionRef:
    id: uuid.UUID
    workspace_id: uuid.UUID
    task_id: uuid.UUID
    revision_number: int
    draft_version: int
    draft_fingerprint: str
    definition: dict[str, Any]
    rendered_task: str
    reason: str
    actor_principal_id: uuid.UUID
    caller_principal_id: uuid.UUID
    channel: AuditChannel
    previous_revision_id: uuid.UUID | None
    content_hash: str
    idempotency_record_id: uuid.UUID


@dataclass(frozen=True)
class TaskPublishResult:
    workspace_id: uuid.UUID
    task_id: uuid.UUID
    revision_id: uuid.UUID
    revision_number: int
    previous_revision_id: uuid.UUID | None
    draft_version: int
    draft_fingerprint: str
    content_hash: str
    materialization_id: uuid.UUID
    materialization_state: TaskMaterializationState
    response_status: int
    replayed: bool


@dataclass(frozen=True)
class TaskLifecycleResult:
    workspace_id: uuid.UUID
    task_id: uuid.UUID
    task_key: str
    previous_state: TaskLifecycleState
    lifecycle_state: TaskLifecycleState
    reason: str
    changed: bool
    idempotency_record_id: uuid.UUID
    audit_event_id: uuid.UUID | None
    response_status: int
    replayed: bool


class MembershipMutationAction(str, Enum):
    GRANTED = "granted"
    CHANGED = "changed"
    REVOKED = "revoked"
    UNCHANGED = "unchanged"


@dataclass(frozen=True)
class MembershipMutationResult:
    action: MembershipMutationAction
    workspace: WorkspaceRef
    principal: PrincipalRef
    binding_id: uuid.UUID | None
    previous_role: Role | None
    role: Role | None
    audit_event_id: uuid.UUID | None


@dataclass(frozen=True)
class WorkspaceMemberRef:
    principal_id: uuid.UUID
    email: str | None
    display_name: str | None
    role: Role
    is_active: bool


@dataclass(frozen=True)
class WorkspaceInvitationRef:
    id: uuid.UUID
    workspace: WorkspaceRef
    email: str
    role: Role
    status: WorkspaceInvitationStatus
    expires_at: datetime
    claimed_principal_id: uuid.UUID | None


@dataclass(frozen=True)
class WorkspaceMemberList:
    workspace: WorkspaceRef
    members: tuple[WorkspaceMemberRef, ...]
    invitations: tuple[WorkspaceInvitationRef, ...]


@dataclass(frozen=True)
class InvitationClaimRef:
    invitation_id: uuid.UUID
    workspace: WorkspaceRef
    role: Role
    principal_id: uuid.UUID
    membership_created: bool
    claim_status: str


@dataclass(frozen=True)
class WorkspaceInvitationResult:
    action: str
    invitation: WorkspaceInvitationRef
    claim_status: str
    response_status: int
    replayed: bool


@dataclass(frozen=True)
class IdempotentMembershipMutationResult:
    result: MembershipMutationResult
    response_status: int
    replayed: bool


class AuthorizationUnavailable(RuntimeError):
    pass


class AuthorizationDenied(RuntimeError):
    def __init__(self, decision: AuthorizationDecision):
        self.decision = decision
        super().__init__(f"authorization denied: {decision.reason.value}")


class IdentityTypeConflict(RuntimeError):
    pass


class IdempotencyConflict(RuntimeError):
    code = "idempotency_conflict"

    def __init__(self):
        super().__init__(self.code)


class TaskPreconditionRequired(RuntimeError):
    code = "task_precondition_required"

    def __init__(self):
        super().__init__(self.code)


class TaskPublishReasonRequired(ValueError):
    code = "task_publish_reason_required"

    def __init__(self):
        super().__init__(self.code)


class TaskDraftConflict(RuntimeError):
    code = "task_draft_conflict"

    def __init__(self, current_etag: str | None):
        self.current_etag = current_etag
        super().__init__(self.code)


class TaskDraftNotFound(RuntimeError):
    code = "task_draft_not_found"

    def __init__(self):
        super().__init__(self.code)


class TaskCreateConflict(RuntimeError):
    code = "task_create_conflict"

    def __init__(self):
        super().__init__(self.code)


class TaskCreateInProgress(RuntimeError):
    code = "task_create_in_progress"

    def __init__(self):
        super().__init__(self.code)


class TaskPublishInProgress(RuntimeError):
    code = "task_publish_in_progress"

    def __init__(self):
        super().__init__(self.code)


class TaskLifecycleReasonRequired(ValueError):
    code = "task_lifecycle_reason_required"

    def __init__(self):
        super().__init__(self.code)


class TaskLifecycleStateInvalid(ValueError):
    code = "task_lifecycle_state_invalid"

    def __init__(self, state: object):
        self.state = state
        super().__init__(f"invalid task lifecycle state: {state}")


class TaskLifecycleTransitionInvalid(RuntimeError):
    code = "task_lifecycle_transition_invalid"

    def __init__(self, previous_state: TaskLifecycleState, target_state: TaskLifecycleState):
        self.previous_state = previous_state
        self.target_state = target_state
        super().__init__(self.code)


class TaskLifecycleInProgress(RuntimeError):
    code = "task_lifecycle_in_progress"

    def __init__(self):
        super().__init__(self.code)


class TaskLifecycleNotActive(RuntimeError):
    code = "task_lifecycle_not_active"

    def __init__(self, state: TaskLifecycleState):
        self.state = state
        super().__init__(self.code)


class MembershipConflict(RuntimeError):
    pass


class MembershipNotFound(RuntimeError):
    pass


class InvitationConflict(RuntimeError):
    pass


class MembershipOperationInProgress(RuntimeError):
    code = "membership_operation_in_progress"


class LastWorkspaceAdmin(RuntimeError):
    pass


class DatabaseService:
    def __init__(self, session_factory, engine: Engine | None = None):
        self._session_factory = session_factory
        self._engine = engine

    @classmethod
    def from_url(cls, database_url: str | None = None) -> DatabaseService:
        engine = create_database_engine(database_url)
        return cls(create_session_factory(engine), engine)

    def close(self) -> None:
        if self._engine is not None:
            self._engine.dispose()

    @contextmanager
    def transaction(self) -> Iterator[DatabaseTransaction]:
        try:
            with self._session_factory() as session, session.begin():
                yield DatabaseTransaction(session)
        except AuthorizationUnavailable:
            raise
        except SQLAlchemyError as exc:
            raise AuthorizationUnavailable("database authorization state is unavailable") from exc

    def resolve_identity(self, identity: ExternalIdentity) -> PrincipalRef | None:
        with self.transaction() as transaction:
            return transaction.resolve_identity(identity)

    def require_workspace_member_identity(
        self,
        *,
        workspace_slug: str,
        principal_id: uuid.UUID | str,
        permission: Permission = Permission.ANNOTATION_WORK,
    ) -> ExternalIdentity:
        with self.transaction() as transaction:
            return transaction.require_workspace_member_identity(
                workspace_slug=workspace_slug,
                principal_id=principal_id,
                permission=permission,
            )

    def resolve_or_provision(
        self,
        identity: ExternalIdentity,
        principal_type: PrincipalType = PrincipalType.USER,
    ) -> PrincipalRef:
        with self.transaction() as transaction:
            return transaction.resolve_or_provision(identity, principal_type)

    def get_session(self, identity: ExternalIdentity) -> AuthorizationSession:
        with self.transaction() as transaction:
            return transaction.get_session(identity)

    def list_workspace_members(
        self,
        *,
        identity: ExternalIdentity,
        workspace_slug: str,
    ) -> WorkspaceMemberList:
        with self.transaction() as transaction:
            return transaction.list_workspace_members(
                identity=identity,
                workspace_slug=workspace_slug,
            )

    def create_workspace_invitation(
        self,
        *,
        workspace_slug: str,
        email: str,
        role: Role,
        actor_identity: ExternalIdentity,
        caller_identity: ExternalIdentity,
        idempotency_key: str,
        channel: AuditChannel,
        request_id: str | None = None,
    ) -> WorkspaceInvitationResult:
        with self.transaction() as transaction:
            return transaction.create_workspace_invitation(
                workspace_slug=workspace_slug,
                email=email,
                role=role,
                actor_identity=actor_identity,
                caller_identity=caller_identity,
                idempotency_key=idempotency_key,
                channel=channel,
                request_id=request_id,
            )

    def change_workspace_membership_by_principal_id(
        self,
        *,
        workspace_slug: str,
        principal_id: uuid.UUID,
        role: Role,
        actor_identity: ExternalIdentity,
        caller_identity: ExternalIdentity,
        idempotency_key: str,
        channel: AuditChannel,
        request_id: str | None = None,
    ) -> IdempotentMembershipMutationResult:
        with self.transaction() as transaction:
            return transaction.change_workspace_membership_by_principal_id(
                workspace_slug=workspace_slug,
                principal_id=principal_id,
                role=role,
                actor_identity=actor_identity,
                caller_identity=caller_identity,
                idempotency_key=idempotency_key,
                channel=channel,
                request_id=request_id,
            )

    def revoke_workspace_membership_by_principal_id(
        self,
        *,
        workspace_slug: str,
        principal_id: uuid.UUID,
        actor_identity: ExternalIdentity,
        caller_identity: ExternalIdentity,
        idempotency_key: str,
        channel: AuditChannel,
        request_id: str | None = None,
    ) -> IdempotentMembershipMutationResult:
        with self.transaction() as transaction:
            return transaction.revoke_workspace_membership_by_principal_id(
                workspace_slug=workspace_slug,
                principal_id=principal_id,
                actor_identity=actor_identity,
                caller_identity=caller_identity,
                idempotency_key=idempotency_key,
                channel=channel,
                request_id=request_id,
            )

    def list_authorized_workspaces(self, identity: ExternalIdentity) -> tuple[WorkspaceAccess, ...]:
        return self.get_session(identity).workspaces

    def list_authorized_tasks(
        self,
        identity: ExternalIdentity,
        workspace_slug: str,
        *,
        limit: int,
        after_task_key: str | None = None,
        include_archived: bool = False,
    ) -> TaskAccessPage:
        with self.transaction() as transaction:
            return transaction.list_authorized_tasks(
                identity,
                workspace_slug,
                limit=limit,
                after_task_key=after_task_key,
                include_archived=include_archived,
            )

    def authorize_workspace(
        self,
        identity: ExternalIdentity,
        workspace_slug: str,
        permission: Permission,
    ) -> AuthorizationDecision:
        with self.transaction() as transaction:
            return transaction.authorize_workspace(identity, workspace_slug, permission)

    def authorize_task(
        self,
        identity: ExternalIdentity,
        workspace_slug: str,
        task_key: str,
        permission: Permission,
    ) -> AuthorizationDecision:
        with self.transaction() as transaction:
            return transaction.authorize_task(identity, workspace_slug, task_key, permission)

    def require_workspace(
        self,
        identity: ExternalIdentity,
        workspace_slug: str,
        permission: Permission,
    ) -> AuthorizationDecision:
        decision = self.authorize_workspace(identity, workspace_slug, permission)
        if not decision.allowed:
            raise AuthorizationDenied(decision)
        return decision

    def require_task(
        self,
        identity: ExternalIdentity,
        workspace_slug: str,
        task_key: str,
        permission: Permission,
    ) -> AuthorizationDecision:
        decision = self.authorize_task(identity, workspace_slug, task_key, permission)
        if not decision.allowed:
            raise AuthorizationDenied(decision)
        return decision

    def get_task_lifecycle(
        self,
        *,
        identity: ExternalIdentity,
        workspace_slug: str,
        task_key: str,
    ) -> TaskLifecycleState:
        decision = self.require_task(identity, workspace_slug, task_key, Permission.TASK_READ)
        assert decision.task is not None
        return decision.task.lifecycle_state

    def create_task(
        self,
        *,
        actor_identity: ExternalIdentity,
        caller_identity: ExternalIdentity,
        workspace_slug: str,
        task_key: str,
        definition: dict[str, Any],
        rendered_task: str,
        idempotency_key: str,
        channel: AuditChannel,
        request_id: str | None = None,
    ) -> TaskCreateResult:
        with self.transaction() as transaction:
            return transaction.create_task(
                actor_identity=actor_identity,
                caller_identity=caller_identity,
                workspace_slug=workspace_slug,
                task_key=task_key,
                definition=definition,
                rendered_task=rendered_task,
                idempotency_key=idempotency_key,
                channel=channel,
                request_id=request_id,
            )

    def claim_idempotency(
        self,
        *,
        actor_identity: ExternalIdentity,
        caller_identity: ExternalIdentity,
        workspace_slug: str,
        required_permission: Permission,
        operation: str,
        idempotency_key: str,
        request_payload: Any,
        channel: AuditChannel,
        task_key: str | None = None,
    ) -> IdempotencyClaim:
        with self.transaction() as transaction:
            return transaction.claim_idempotency(
                actor_identity=actor_identity,
                caller_identity=caller_identity,
                workspace_slug=workspace_slug,
                required_permission=required_permission,
                operation=operation,
                idempotency_key=idempotency_key,
                request_payload=request_payload,
                channel=channel,
                task_key=task_key,
            )

    def complete_idempotency(
        self,
        claim: IdempotencyClaim,
        *,
        actor_identity: ExternalIdentity,
        caller_identity: ExternalIdentity,
        response_status: int,
        response_body: dict[str, Any] | None,
        succeeded: bool = True,
        request_id: str | None = None,
    ) -> IdempotencyClaim:
        with self.transaction() as transaction:
            return transaction.complete_idempotency(
                claim,
                actor_identity=actor_identity,
                caller_identity=caller_identity,
                response_status=response_status,
                response_body=response_body,
                succeeded=succeeded,
                request_id=request_id,
            )

    def grant_workspace_membership(
        self,
        *,
        workspace_slug: str,
        target_identity: ExternalIdentity,
        role: Role,
        actor_identity: ExternalIdentity,
        caller_identity: ExternalIdentity,
        channel: AuditChannel,
        request_id: str | None = None,
    ) -> MembershipMutationResult:
        with self.transaction() as transaction:
            return transaction.grant_workspace_membership(
                workspace_slug=workspace_slug,
                target_identity=target_identity,
                role=role,
                actor_identity=actor_identity,
                caller_identity=caller_identity,
                channel=channel,
                request_id=request_id,
            )

    def change_workspace_membership(
        self,
        *,
        workspace_slug: str,
        target_identity: ExternalIdentity,
        role: Role,
        actor_identity: ExternalIdentity,
        caller_identity: ExternalIdentity,
        channel: AuditChannel,
        request_id: str | None = None,
    ) -> MembershipMutationResult:
        with self.transaction() as transaction:
            return transaction.change_workspace_membership(
                workspace_slug=workspace_slug,
                target_identity=target_identity,
                role=role,
                actor_identity=actor_identity,
                caller_identity=caller_identity,
                channel=channel,
                request_id=request_id,
            )

    def revoke_workspace_membership(
        self,
        *,
        workspace_slug: str,
        target_identity: ExternalIdentity,
        actor_identity: ExternalIdentity,
        caller_identity: ExternalIdentity,
        channel: AuditChannel,
        request_id: str | None = None,
    ) -> MembershipMutationResult:
        with self.transaction() as transaction:
            return transaction.revoke_workspace_membership(
                workspace_slug=workspace_slug,
                target_identity=target_identity,
                actor_identity=actor_identity,
                caller_identity=caller_identity,
                channel=channel,
                request_id=request_id,
            )

    def append_audit(
        self,
        *,
        workspace_slug: str,
        event_type: str,
        actor_identity: ExternalIdentity,
        caller_identity: ExternalIdentity,
        channel: AuditChannel,
        required_permission: Permission,
        task_key: str | None = None,
        request_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditEventRef:
        with self.transaction() as transaction:
            return transaction.append_audit(
                workspace_slug=workspace_slug,
                event_type=event_type,
                actor_identity=actor_identity,
                caller_identity=caller_identity,
                channel=channel,
                required_permission=required_permission,
                task_key=task_key,
                request_id=request_id,
                details=details,
            )

    def get_task_draft(
        self,
        *,
        identity: ExternalIdentity,
        workspace_slug: str,
        task_key: str,
    ) -> TaskDraftRef | None:
        with self.transaction() as transaction:
            return transaction.get_task_draft(
                identity=identity,
                workspace_slug=workspace_slug,
                task_key=task_key,
            )

    def save_task_draft(
        self,
        *,
        actor_identity: ExternalIdentity,
        caller_identity: ExternalIdentity,
        workspace_slug: str,
        task_key: str,
        definition: dict[str, Any],
        rendered_task: str,
        if_match: str | None,
        channel: AuditChannel,
        if_none_match: str | None = None,
        request_id: str | None = None,
    ) -> TaskDraftSaveResult:
        with self.transaction() as transaction:
            return transaction.save_task_draft(
                actor_identity=actor_identity,
                caller_identity=caller_identity,
                workspace_slug=workspace_slug,
                task_key=task_key,
                definition=definition,
                rendered_task=rendered_task,
                if_match=if_match,
                channel=channel,
                if_none_match=if_none_match,
                request_id=request_id,
            )

    def get_task_revision(
        self,
        *,
        identity: ExternalIdentity,
        workspace_slug: str,
        task_key: str,
        revision_id: uuid.UUID,
    ) -> TaskRevisionRef | None:
        with self.transaction() as transaction:
            return transaction.get_task_revision(
                identity=identity,
                workspace_slug=workspace_slug,
                task_key=task_key,
                revision_id=revision_id,
            )

    def publish_task_draft(
        self,
        *,
        actor_identity: ExternalIdentity,
        caller_identity: ExternalIdentity,
        workspace_slug: str,
        task_key: str,
        if_match: str | None,
        reason: str,
        idempotency_key: str,
        channel: AuditChannel,
        request_id: str | None = None,
    ) -> TaskPublishResult:
        with self.transaction() as transaction:
            return transaction.publish_task_draft(
                actor_identity=actor_identity,
                caller_identity=caller_identity,
                workspace_slug=workspace_slug,
                task_key=task_key,
                if_match=if_match,
                reason=reason,
                idempotency_key=idempotency_key,
                channel=channel,
                request_id=request_id,
            )

    def transition_task_lifecycle(
        self,
        *,
        actor_identity: ExternalIdentity,
        caller_identity: ExternalIdentity,
        workspace_slug: str,
        task_key: str,
        target_state: TaskLifecycleState | str,
        reason: str,
        idempotency_key: str,
        channel: AuditChannel,
        request_id: str | None = None,
    ) -> TaskLifecycleResult:
        with self.transaction() as transaction:
            return transaction.transition_task_lifecycle(
                actor_identity=actor_identity,
                caller_identity=caller_identity,
                workspace_slug=workspace_slug,
                task_key=task_key,
                target_state=target_state,
                reason=reason,
                idempotency_key=idempotency_key,
                channel=channel,
                request_id=request_id,
            )

    def disable_task(self, **kwargs) -> TaskLifecycleResult:
        return self.transition_task_lifecycle(target_state=TaskLifecycleState.DISABLED, **kwargs)

    def archive_task(self, **kwargs) -> TaskLifecycleResult:
        return self.transition_task_lifecycle(target_state=TaskLifecycleState.ARCHIVED, **kwargs)

    def restore_task(self, **kwargs) -> TaskLifecycleResult:
        return self.transition_task_lifecycle(target_state=TaskLifecycleState.ACTIVE, **kwargs)


class DatabaseTransaction:
    def __init__(self, session: Session):
        self._session = session

    def resolve_identity(self, identity: ExternalIdentity) -> PrincipalRef | None:
        principal = self._find_principal(identity)
        return _principal_ref(principal) if principal is not None else None

    def require_workspace_member_identity(
        self,
        *,
        workspace_slug: str,
        principal_id: uuid.UUID | str,
        permission: Permission = Permission.ANNOTATION_WORK,
    ) -> ExternalIdentity:
        if permission not in TASK_PERMISSIONS:
            raise AuthorizationDenied(
                _decision(permission, AuthorizationReason.INVALID_PERMISSION_SCOPE)
            )
        normalized_principal_id = _principal_uuid(principal_id)
        workspace = self._session.scalar(
            select(Workspace)
            .where(Workspace.slug == workspace_slug)
            .execution_options(populate_existing=True),
        )
        if workspace is None:
            raise AuthorizationDenied(
                _decision(permission, AuthorizationReason.RESOURCE_NOT_VISIBLE)
            )
        workspace_ref = _workspace_ref(workspace)
        if not workspace.is_active:
            raise AuthorizationDenied(
                _decision(
                    permission,
                    AuthorizationReason.INACTIVE_WORKSPACE,
                    workspace=workspace_ref,
                )
            )

        principal = self._session.scalar(
            select(Principal)
            .where(Principal.id == normalized_principal_id)
            .execution_options(populate_existing=True),
        )
        if principal is None:
            raise AuthorizationDenied(
                _decision(permission, AuthorizationReason.RESOURCE_NOT_VISIBLE)
            )
        principal_ref = _principal_ref(principal)
        if not principal.is_active:
            raise AuthorizationDenied(
                _decision(
                    permission,
                    AuthorizationReason.INACTIVE_PRINCIPAL,
                    principal=principal_ref,
                    workspace=workspace_ref,
                )
            )

        roles = self._roles(principal.id, workspace.id)
        if not roles:
            raise AuthorizationDenied(
                _decision(
                    permission,
                    AuthorizationReason.RESOURCE_NOT_VISIBLE,
                    principal=principal_ref,
                    workspace=workspace_ref,
                )
            )
        decision = _role_decision(permission, principal_ref, workspace_ref, None, roles)
        self.require(decision)
        return ExternalIdentity(
            issuer=principal.issuer,
            subject=principal.subject,
            email_snapshot=principal.email_snapshot,
            display_name_snapshot=principal.display_name,
        )

    def resolve_or_provision(
        self,
        identity: ExternalIdentity,
        principal_type: PrincipalType = PrincipalType.USER,
    ) -> PrincipalRef:
        principal = self._find_principal(identity)
        if principal is None:
            try:
                with self._session.begin_nested():
                    principal = Principal(
                        issuer=identity.issuer,
                        subject=identity.subject,
                        principal_type=principal_type,
                        display_name=identity.display_name_snapshot,
                        email_snapshot=identity.email_snapshot,
                    )
                    self._session.add(principal)
                    self._session.flush()
            except IntegrityError:
                principal = self._find_principal(identity)
                if principal is None:
                    raise

        if principal.principal_type != principal_type:
            raise IdentityTypeConflict(
                f"principal {identity.issuer!r}/{identity.subject!r} already exists as {principal.principal_type.value}",
            )
        if identity.display_name_snapshot is not None:
            principal.display_name = identity.display_name_snapshot
        if identity.email_snapshot is not None:
            principal.email_snapshot = identity.email_snapshot
        self._session.flush()
        return _principal_ref(principal)

    def get_session(self, identity: ExternalIdentity) -> AuthorizationSession:
        invitation_claims = self._claim_pending_invitations(identity)
        principal = self._find_principal(identity)
        if principal is None:
            return AuthorizationSession(
                principal=None,
                workspaces=(),
                invitation_claims=invitation_claims,
            )
        principal_ref = _principal_ref(principal)
        if not principal.is_active:
            return AuthorizationSession(
                principal=principal_ref,
                workspaces=(),
                invitation_claims=invitation_claims,
            )
        return AuthorizationSession(
            principal=principal_ref,
            workspaces=self._list_authorized_workspaces(principal.id),
            invitation_claims=invitation_claims,
        )

    def list_workspace_members(
        self,
        *,
        identity: ExternalIdentity,
        workspace_slug: str,
    ) -> WorkspaceMemberList:
        decision = self.authorize_workspace(identity, workspace_slug, Permission.WORKSPACE_MANAGE)
        self.require(decision)
        assert decision.workspace is not None
        workspace = self._session.scalar(
            select(Workspace)
            .where(Workspace.id == decision.workspace.id)
            .execution_options(populate_existing=True),
        )
        if workspace is None:
            raise AuthorizationDenied(
                _decision(
                    Permission.WORKSPACE_MANAGE,
                    AuthorizationReason.RESOURCE_NOT_VISIBLE,
                )
            )
        self._expire_workspace_invitations(workspace.id)
        members = self._session.execute(
            select(RoleBinding, Principal)
            .join(Principal, Principal.id == RoleBinding.principal_id)
            .where(
                RoleBinding.workspace_id == workspace.id,
                RoleBinding.task_id.is_(None),
            )
            .order_by(Principal.email_snapshot, Principal.display_name, Principal.id)
        ).all()
        invitations = self._session.scalars(
            select(WorkspaceInvitation)
            .where(WorkspaceInvitation.workspace_id == workspace.id)
            .order_by(WorkspaceInvitation.email, WorkspaceInvitation.id),
        ).all()
        return WorkspaceMemberList(
            workspace=_workspace_ref(workspace),
            members=tuple(
                WorkspaceMemberRef(
                    principal_id=principal.id,
                    email=principal.email_snapshot,
                    display_name=principal.display_name,
                    role=binding.role,
                    is_active=principal.is_active,
                )
                for binding, principal in members
            ),
            invitations=tuple(
                _workspace_invitation_ref(invitation, workspace)
                for invitation in invitations
            ),
        )

    def _claim_pending_invitations(
        self,
        identity: ExternalIdentity,
    ) -> tuple[InvitationClaimRef, ...]:
        normalized_email = normalize_optional_invitation_email(identity.email_snapshot)
        if normalized_email is None:
            return ()
        now = datetime.now(timezone.utc)
        candidates = self._session.scalars(
            select(WorkspaceInvitation)
            .where(
                WorkspaceInvitation.email == normalized_email,
                WorkspaceInvitation.status == WorkspaceInvitationStatus.PENDING,
            )
            .order_by(WorkspaceInvitation.workspace_id, WorkspaceInvitation.id)
            .with_for_update()
            .execution_options(populate_existing=True),
        ).all()
        if not candidates:
            return ()
        expired = [
            invitation
            for invitation in candidates
            if _utc_datetime(invitation.expires_at) <= now
        ]
        for invitation in expired:
            self._expire_invitation(invitation)
        pending = [invitation for invitation in candidates if invitation not in expired]
        if not pending:
            self._session.flush()
            return ()

        principal_ref = self.resolve_or_provision(identity, PrincipalType.USER)
        principal = self._session.scalar(
            select(Principal)
            .where(Principal.id == principal_ref.id)
            .with_for_update()
            .execution_options(populate_existing=True),
        )
        if principal is None or not principal.is_active:
            return ()
        workspace_ids = sorted({invitation.workspace_id for invitation in pending}, key=str)
        workspaces = {
            workspace.id: workspace
            for workspace in self._session.scalars(
                select(Workspace)
                .where(Workspace.id.in_(workspace_ids))
                .order_by(Workspace.id)
                .with_for_update()
                .execution_options(populate_existing=True),
            ).all()
        }
        claims: list[InvitationClaimRef] = []
        for invitation in pending:
            workspace = workspaces.get(invitation.workspace_id)
            if workspace is None or not workspace.is_active:
                continue
            binding = self._workspace_binding(
                workspace.id,
                principal.id,
                for_update=True,
            )
            if binding is not None and binding.role != invitation.role:
                claims.append(
                    InvitationClaimRef(
                        invitation_id=invitation.id,
                        workspace=_workspace_ref(workspace),
                        role=invitation.role,
                        principal_id=principal.id,
                        membership_created=False,
                        claim_status="role_conflict",
                    )
                )
                continue
            membership_created = binding is None
            if binding is None:
                binding = RoleBinding(
                    workspace_id=workspace.id,
                    principal_id=principal.id,
                    role=invitation.role,
                    created_by_principal_id=principal.id,
                )
                self._session.add(binding)
                self._session.flush()
            invitation.status = WorkspaceInvitationStatus.CLAIMED
            invitation.claimed_principal_id = principal.id
            invitation.claimed_at = now
            append_audit_event(
                self._session,
                workspace_id=workspace.id,
                event_type="workspace.invitation_claimed",
                actor=principal,
                caller=principal,
                channel=AuditChannel.PANEL,
                resource_type="workspace_invitation",
                resource_id=invitation.id,
                details={
                    "invitation_id": str(invitation.id),
                    "target_principal_id": str(principal.id),
                    "role": invitation.role.value,
                    "membership_created": membership_created,
                },
            )
            claims.append(
                InvitationClaimRef(
                    invitation_id=invitation.id,
                    workspace=_workspace_ref(workspace),
                    role=invitation.role,
                    principal_id=principal.id,
                    membership_created=membership_created,
                    claim_status="claimed",
                )
            )
        self._session.flush()
        return tuple(claims)

    def _expire_workspace_invitations(
        self,
        workspace_id: uuid.UUID,
        *,
        email: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        statement = select(WorkspaceInvitation).where(
            WorkspaceInvitation.workspace_id == workspace_id,
            WorkspaceInvitation.status == WorkspaceInvitationStatus.PENDING,
            WorkspaceInvitation.expires_at <= now,
        )
        if email is not None:
            statement = statement.where(WorkspaceInvitation.email == normalize_invitation_email(email))
        invitations = self._session.scalars(
            statement.with_for_update().execution_options(populate_existing=True),
        ).all()
        for invitation in invitations:
            self._expire_invitation(invitation)
        if invitations:
            self._session.flush()

    def _expire_invitation(self, invitation: WorkspaceInvitation) -> None:
        invitation.status = WorkspaceInvitationStatus.EXPIRED
        append_audit_event(
            self._session,
            workspace_id=invitation.workspace_id,
            event_type="workspace.invitation_expired",
            actor=None,
            caller=None,
            channel=AuditChannel.SYSTEM,
            resource_type="workspace_invitation",
            resource_id=invitation.id,
            details={
                "invitation_id": str(invitation.id),
                "role": invitation.role.value,
            },
        )

    def list_authorized_tasks(
        self,
        identity: ExternalIdentity,
        workspace_slug: str,
        *,
        limit: int,
        after_task_key: str | None = None,
        include_archived: bool = False,
    ) -> TaskAccessPage:
        if limit < 1 or limit > MAX_AUTHORIZED_TASKS_PAGE_SIZE:
            raise ValueError(f"limit must be between 1 and {MAX_AUTHORIZED_TASKS_PAGE_SIZE}")
        principal = self._find_principal(identity)
        if principal is None or not principal.is_active:
            return TaskAccessPage(workspace=None, items=(), next_cursor=None)

        workspace = self._session.scalar(
            select(Workspace)
            .join(
                RoleBinding,
                and_(
                    RoleBinding.workspace_id == Workspace.id,
                    RoleBinding.principal_id == principal.id,
                    RoleBinding.task_id.is_(None),
                ),
            )
            .where(Workspace.slug == workspace_slug, Workspace.is_active.is_(True))
            .limit(1),
        )
        if workspace is None:
            return TaskAccessPage(workspace=None, items=(), next_cursor=None)

        workspace_roles = self._roles(principal.id, workspace.id)
        task_query = select(Task).where(Task.workspace_id == workspace.id)
        if not workspace_roles:
            task_query = task_query.join(
                RoleBinding,
                and_(
                    RoleBinding.workspace_id == Task.workspace_id,
                    RoleBinding.task_id == Task.id,
                    RoleBinding.principal_id == principal.id,
                ),
            )
        if not include_archived:
            task_query = task_query.where(Task.lifecycle_state != TaskLifecycleState.ARCHIVED)
        if after_task_key is not None:
            task_query = task_query.where(Task.task_key > after_task_key)
        tasks = self._session.scalars(
            task_query.order_by(Task.task_key).limit(limit + 1),
        ).all()
        has_more = len(tasks) > limit
        page_tasks = tasks[:limit]
        task_roles: dict[uuid.UUID, set[Role]] = {}
        if page_tasks:
            rows = self._session.execute(
                select(RoleBinding.task_id, RoleBinding.role).where(
                    RoleBinding.principal_id == principal.id,
                    RoleBinding.workspace_id == workspace.id,
                    RoleBinding.task_id.in_([task.id for task in page_tasks]),
                ),
            ).all()
            for task_id, role in rows:
                task_roles.setdefault(task_id, set()).add(role)

        drafts_by_task_id: dict[uuid.UUID, TaskDraft] = {}
        if page_tasks:
            drafts_by_task_id = {
                draft.task_id: draft
                for draft in self._session.scalars(
                    select(TaskDraft).where(
                        TaskDraft.workspace_id == workspace.id,
                        TaskDraft.task_id.in_([task.id for task in page_tasks]),
                    )
                ).all()
            }

        items: list[TaskAccess] = []
        for task in page_tasks:
            roles = _sorted_roles((*workspace_roles, *task_roles.get(task.id, set())))
            items.append(
                TaskAccess(
                    task=_task_ref(task, draft=drafts_by_task_id.get(task.id)),
                    roles=roles,
                    capabilities=_capabilities(roles, TASK_PERMISSIONS),
                )
            )
        next_cursor = page_tasks[-1].task_key if has_more and page_tasks else None
        return TaskAccessPage(
            workspace=_workspace_ref(workspace),
            items=tuple(items),
            next_cursor=next_cursor,
        )

    def authorize_workspace(
        self,
        identity: ExternalIdentity,
        workspace_slug: str,
        permission: Permission,
    ) -> AuthorizationDecision:
        if permission not in WORKSPACE_PERMISSIONS:
            return _decision(permission, AuthorizationReason.INVALID_PERMISSION_SCOPE)
        principal = self._find_principal(identity)
        if principal is None:
            return _decision(permission, AuthorizationReason.UNKNOWN_PRINCIPAL)
        principal_ref = _principal_ref(principal)
        if not principal.is_active:
            return _decision(
                permission,
                AuthorizationReason.INACTIVE_PRINCIPAL,
                principal=principal_ref,
            )

        workspace = self._session.scalar(
            select(Workspace)
            .join(
                RoleBinding,
                and_(
                    RoleBinding.workspace_id == Workspace.id,
                    RoleBinding.principal_id == principal.id,
                    RoleBinding.task_id.is_(None),
                ),
            )
            .where(Workspace.slug == workspace_slug)
            .execution_options(populate_existing=True),
        )
        if workspace is None:
            return _decision(
                permission,
                AuthorizationReason.RESOURCE_NOT_VISIBLE,
                principal=principal_ref,
            )
        roles = self._roles(principal.id, workspace.id)

        workspace_ref = _workspace_ref(workspace)
        if not workspace.is_active:
            return _decision(
                permission,
                AuthorizationReason.INACTIVE_WORKSPACE,
                principal=principal_ref,
                workspace=workspace_ref,
                roles=roles,
            )
        return _role_decision(permission, principal_ref, workspace_ref, None, roles)

    def authorize_task(
        self,
        identity: ExternalIdentity,
        workspace_slug: str,
        task_key: str,
        permission: Permission,
    ) -> AuthorizationDecision:
        if permission not in TASK_PERMISSIONS:
            return _decision(permission, AuthorizationReason.INVALID_PERMISSION_SCOPE)
        principal = self._find_principal(identity)
        if principal is None:
            return _decision(permission, AuthorizationReason.UNKNOWN_PRINCIPAL)
        principal_ref = _principal_ref(principal)
        if not principal.is_active:
            return _decision(
                permission,
                AuthorizationReason.INACTIVE_PRINCIPAL,
                principal=principal_ref,
            )

        row = self._session.execute(
            select(Workspace, Task)
            .join(Task, Task.workspace_id == Workspace.id)
            .join(
                RoleBinding,
                and_(
                    RoleBinding.workspace_id == Workspace.id,
                    RoleBinding.principal_id == principal.id,
                    RoleBinding.task_id.is_(None),
                ),
            )
            .where(Workspace.slug == workspace_slug, Task.task_key == task_key)
            .execution_options(populate_existing=True)
            .limit(1),
        ).first()
        if row is None:
            return _decision(
                permission,
                AuthorizationReason.RESOURCE_NOT_VISIBLE,
                principal=principal_ref,
            )
        workspace, task = row
        roles = self._roles(principal.id, workspace.id, task.id)

        workspace_ref = _workspace_ref(workspace)
        task_ref = _task_ref(task)
        if not workspace.is_active:
            return _decision(
                permission,
                AuthorizationReason.INACTIVE_WORKSPACE,
                principal=principal_ref,
                workspace=workspace_ref,
                task=task_ref,
                roles=roles,
            )
        return _role_decision(permission, principal_ref, workspace_ref, task_ref, roles)

    def require(self, decision: AuthorizationDecision) -> None:
        if not decision.allowed:
            raise AuthorizationDenied(decision)

    def create_task(
        self,
        *,
        actor_identity: ExternalIdentity,
        caller_identity: ExternalIdentity,
        workspace_slug: str,
        task_key: str,
        definition: dict[str, Any],
        rendered_task: str,
        idempotency_key: str,
        channel: AuditChannel,
        request_id: str | None = None,
    ) -> TaskCreateResult:
        normalized_task_key = _require_task_key(task_key)
        if not isinstance(definition, dict):
            raise ValueError("definition must be a JSON object")
        if str(definition.get("task_id") or "").strip() != normalized_task_key:
            raise ValueError("definition.task_id must match task_key")
        fingerprint = task_definition_fingerprint(definition, rendered_task)
        if not idempotency_key.strip():
            raise ValueError("idempotency_key must not be blank")
        _require_task_write_channel(channel)

        claim = self.claim_idempotency(
            actor_identity=actor_identity,
            caller_identity=caller_identity,
            workspace_slug=workspace_slug,
            required_permission=Permission.TASK_CREATE,
            operation="task.create",
            idempotency_key=idempotency_key,
            request_payload={
                "workspace": workspace_slug,
                "task_key": normalized_task_key,
                "definition": definition,
                "rendered_task": rendered_task,
                "channel": channel.value,
            },
            channel=channel,
        )
        if claim.status == IdempotencyClaimStatus.REPLAY:
            if claim.state != IdempotencyState.SUCCEEDED:
                raise IdempotencyConflict()
            return _task_create_result_from_claim(claim, replayed=True)
        if claim.status == IdempotencyClaimStatus.PENDING:
            raise TaskCreateInProgress()

        workspace = self._session.scalar(
            select(Workspace)
            .where(Workspace.id == claim.workspace_id)
            .with_for_update()
        )
        if workspace is None:
            raise AuthorizationUnavailable("authorized workspace disappeared during task creation")
        existing = self._session.scalar(
            select(Task)
            .where(Task.task_key == normalized_task_key)
            .with_for_update()
        )
        if existing is not None:
            raise TaskCreateConflict()

        actor = self._session.get(Principal, claim.actor_principal_id)
        caller = self._session.get(Principal, claim.caller_principal_id)
        if actor is None or caller is None:
            raise AuthorizationUnavailable("task creation principals disappeared during transaction")

        task = Task(
            workspace_id=workspace.id,
            task_key=normalized_task_key,
            name=str(definition.get("name") or normalized_task_key).strip() or normalized_task_key,
            description=(
                str(definition["description"]).strip()
                if definition.get("description") is not None
                else None
            ),
            created_by_principal_id=actor.id,
        )
        try:
            with self._session.begin_nested():
                self._session.add(task)
                self._session.flush()
        except IntegrityError as exc:
            raise TaskCreateConflict() from exc

        draft = TaskDraft(
            workspace_id=workspace.id,
            task_id=task.id,
            version=1,
            fingerprint=fingerprint,
            definition=definition,
            rendered_task=rendered_task,
        )
        self._session.add(draft)
        self._session.flush()
        append_audit_event(
            self._session,
            workspace_id=workspace.id,
            event_type="task.created",
            actor=actor,
            caller=caller,
            channel=channel,
            resource_type="task",
            resource_id=task.id,
            request_id=request_id,
            details={
                "task_key": task.task_key,
                "task_id": str(task.id),
                "draft_version": draft.version,
                "draft_fingerprint": draft.fingerprint,
            },
        )
        append_audit_event(
            self._session,
            workspace_id=workspace.id,
            event_type="task.draft_created",
            actor=actor,
            caller=caller,
            channel=channel,
            resource_type="task",
            resource_id=task.id,
            request_id=request_id,
            details={
                "task_key": task.task_key,
                "draft_version": draft.version,
                "draft_fingerprint": draft.fingerprint,
            },
        )
        completed = self.complete_idempotency(
            claim,
            actor_identity=actor_identity,
            caller_identity=caller_identity,
            response_status=201,
            response_body=_task_create_response(task, draft),
            request_id=request_id,
        )
        return _task_create_result_from_claim(completed, replayed=False)

    def get_task_draft(
        self,
        *,
        identity: ExternalIdentity,
        workspace_slug: str,
        task_key: str,
    ) -> TaskDraftRef | None:
        decision = self.authorize_task(identity, workspace_slug, task_key, Permission.TASK_EDIT)
        self.require(decision)
        draft = self._session.scalar(
            select(TaskDraft).where(
                TaskDraft.workspace_id == decision.workspace.id,
                TaskDraft.task_id == decision.task.id,
            ),
        )
        return _task_draft_ref(draft) if draft is not None else None

    def save_task_draft(
        self,
        *,
        actor_identity: ExternalIdentity,
        caller_identity: ExternalIdentity,
        workspace_slug: str,
        task_key: str,
        definition: dict[str, Any],
        rendered_task: str,
        if_match: str | None,
        channel: AuditChannel,
        if_none_match: str | None = None,
        request_id: str | None = None,
    ) -> TaskDraftSaveResult:
        if if_match is None and if_none_match is None:
            raise TaskPreconditionRequired()
        if if_match is not None and if_none_match is not None:
            raise ValueError("if_match and if_none_match are mutually exclusive")
        if if_none_match is not None and if_none_match != "*":
            raise ValueError("if_none_match must be '*'")
        fingerprint = task_definition_fingerprint(definition, rendered_task)
        _require_task_write_channel(channel)

        decision = self.authorize_task(actor_identity, workspace_slug, task_key, Permission.TASK_EDIT)
        self.require(decision)
        caller = self._require_caller(caller_identity, decision.principal.id, Permission.TASK_EDIT)
        task = self._lock_task(decision.task.id)
        draft = self._session.scalar(
            select(TaskDraft)
            .where(
                TaskDraft.workspace_id == task.workspace_id,
                TaskDraft.task_id == task.id,
            )
            .with_for_update(),
        )
        if if_none_match == "*":
            if draft is not None:
                raise TaskDraftConflict(current_etag=task_draft_etag(draft.version, draft.fingerprint))
        else:
            assert if_match is not None
            _require_draft_match(draft, if_match)

        if draft is not None and draft.fingerprint == fingerprint:
            return TaskDraftSaveResult(draft=_task_draft_ref(draft), changed=False)

        event_type = "task.draft_created"
        if draft is None:
            draft = TaskDraft(
                workspace_id=task.workspace_id,
                task_id=task.id,
                version=1,
                fingerprint=fingerprint,
                definition=definition,
                rendered_task=rendered_task,
            )
            self._session.add(draft)
        else:
            event_type = "task.draft_updated"
            draft.version += 1
            draft.fingerprint = fingerprint
            draft.definition = definition
            draft.rendered_task = rendered_task
        self._session.flush()

        actor = self._session.get(Principal, decision.principal.id)
        if actor is None:
            raise AuthorizationUnavailable("draft actor disappeared during transaction")
        append_audit_event(
            self._session,
            workspace_id=task.workspace_id,
            event_type=event_type,
            actor=actor,
            caller=caller,
            channel=channel,
            resource_type="task",
            resource_id=task.id,
            request_id=request_id,
            details={
                "task_key": task.task_key,
                "draft_version": draft.version,
                "draft_fingerprint": draft.fingerprint,
            },
        )
        self._session.flush()
        return TaskDraftSaveResult(draft=_task_draft_ref(draft), changed=True)

    def get_task_revision(
        self,
        *,
        identity: ExternalIdentity,
        workspace_slug: str,
        task_key: str,
        revision_id: uuid.UUID,
    ) -> TaskRevisionRef | None:
        decision = self.authorize_task(identity, workspace_slug, task_key, Permission.TASK_EDIT)
        self.require(decision)
        revision = self._session.scalar(
            select(TaskRevision).where(
                TaskRevision.workspace_id == decision.workspace.id,
                TaskRevision.task_id == decision.task.id,
                TaskRevision.id == revision_id,
            ),
        )
        return _task_revision_ref(revision) if revision is not None else None

    def publish_task_draft(
        self,
        *,
        actor_identity: ExternalIdentity,
        caller_identity: ExternalIdentity,
        workspace_slug: str,
        task_key: str,
        if_match: str | None,
        reason: str,
        idempotency_key: str,
        channel: AuditChannel,
        request_id: str | None = None,
    ) -> TaskPublishResult:
        if if_match is None:
            raise TaskPreconditionRequired()
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise TaskPublishReasonRequired()
        if not idempotency_key.strip():
            raise ValueError("idempotency_key must not be blank")
        _require_task_write_channel(channel)

        claim = self.claim_idempotency(
            actor_identity=actor_identity,
            caller_identity=caller_identity,
            workspace_slug=workspace_slug,
            required_permission=Permission.TASK_PUBLISH,
            operation="task.publish",
            idempotency_key=idempotency_key,
            request_payload={
                "workspace": workspace_slug,
                "task": task_key,
                "draft_etag": if_match,
                "reason": normalized_reason,
                "channel": channel.value,
            },
            channel=channel,
            task_key=task_key,
        )
        if claim.status == IdempotencyClaimStatus.REPLAY:
            if claim.state != IdempotencyState.SUCCEEDED:
                raise IdempotencyConflict()
            return _task_publish_result_from_claim(claim, replayed=True)
        if claim.status == IdempotencyClaimStatus.PENDING:
            raise TaskPublishInProgress()

        task = self._lock_task_by_id(claim.workspace_id, task_key)
        draft = self._session.scalar(
            select(TaskDraft)
            .where(
                TaskDraft.workspace_id == task.workspace_id,
                TaskDraft.task_id == task.id,
            )
            .with_for_update(),
        )
        if draft is None:
            raise TaskDraftNotFound()
        _require_draft_match(draft, if_match)

        revision_number = self._session.scalar(
            select(func.coalesce(func.max(TaskRevision.revision_number), 0)).where(
                TaskRevision.task_id == task.id,
            ),
        ) + 1
        revision = TaskRevision(
            workspace_id=task.workspace_id,
            task_id=task.id,
            revision_number=revision_number,
            draft_version=draft.version,
            draft_fingerprint=draft.fingerprint,
            definition=draft.definition,
            rendered_task=draft.rendered_task,
            reason=normalized_reason,
            actor_principal_id=claim.actor_principal_id,
            caller_principal_id=claim.caller_principal_id,
            channel=channel,
            previous_revision_id=task.current_revision_id,
            content_hash=task_definition_fingerprint(draft.definition, draft.rendered_task),
            idempotency_record_id=claim.record_id,
        )
        self._session.add(revision)
        self._session.flush()
        materialization = TaskRevisionMaterialization(
            workspace_id=task.workspace_id,
            task_id=task.id,
            revision_id=revision.id,
        )
        self._session.add(materialization)
        self._session.flush()

        actor = self._session.get(Principal, claim.actor_principal_id)
        caller = self._session.get(Principal, claim.caller_principal_id)
        if actor is None or caller is None:
            raise AuthorizationUnavailable("publish principals disappeared during transaction")
        append_audit_event(
            self._session,
            workspace_id=task.workspace_id,
            event_type="task.revision_published",
            actor=actor,
            caller=caller,
            channel=channel,
            resource_type="task_revision",
            resource_id=revision.id,
            request_id=request_id,
            details={
                "task_id": str(task.id),
                "task_key": task.task_key,
                "revision_number": revision.revision_number,
                "draft_version": revision.draft_version,
                "draft_fingerprint": revision.draft_fingerprint,
                "content_hash": revision.content_hash,
                "previous_revision_id": (
                    str(revision.previous_revision_id) if revision.previous_revision_id is not None else None
                ),
                "materialization_id": str(materialization.id),
            },
        )
        response_body = _task_publish_response(revision, materialization)
        completed = self.complete_idempotency(
            claim,
            actor_identity=actor_identity,
            caller_identity=caller_identity,
            response_status=202,
            response_body=response_body,
        )
        return _task_publish_result_from_claim(completed, replayed=False)

    def transition_task_lifecycle(
        self,
        *,
        actor_identity: ExternalIdentity,
        caller_identity: ExternalIdentity,
        workspace_slug: str,
        task_key: str,
        target_state: TaskLifecycleState | str,
        reason: str,
        idempotency_key: str,
        channel: AuditChannel,
        request_id: str | None = None,
    ) -> TaskLifecycleResult:
        normalized_task_key = _require_task_key(task_key)
        target = _task_lifecycle_state(target_state)
        if not isinstance(reason, str) or not reason.strip():
            raise TaskLifecycleReasonRequired()
        normalized_reason = reason.strip()
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("idempotency_key must not be blank")
        _require_task_write_channel(channel)

        claim = self.claim_idempotency(
            actor_identity=actor_identity,
            caller_identity=caller_identity,
            workspace_slug=workspace_slug,
            required_permission=Permission.TASK_EDIT,
            operation="task.lifecycle",
            idempotency_key=idempotency_key,
            request_payload={
                "workspace": workspace_slug,
                "task": normalized_task_key,
                "target_state": target.value,
                "reason": normalized_reason,
                "channel": channel.value,
            },
            channel=channel,
            task_key=normalized_task_key,
        )
        if claim.status == IdempotencyClaimStatus.REPLAY:
            if claim.state != IdempotencyState.SUCCEEDED:
                raise IdempotencyConflict()
            return _task_lifecycle_result_from_claim(claim, replayed=True)
        if claim.status == IdempotencyClaimStatus.PENDING:
            raise TaskLifecycleInProgress()

        task = self._lock_task_by_id(claim.workspace_id, normalized_task_key)
        previous = _task_lifecycle_state(task.lifecycle_state)
        changed = previous != target
        if changed and target not in _TASK_LIFECYCLE_TRANSITIONS[previous]:
            raise TaskLifecycleTransitionInvalid(previous, target)
        if changed:
            task.lifecycle_state = target
            self._session.flush()

        actor = self._session.get(Principal, claim.actor_principal_id)
        caller = self._session.get(Principal, claim.caller_principal_id)
        if actor is None or caller is None:
            raise AuthorizationUnavailable("lifecycle principals disappeared during transaction")
        audit_event = append_audit_event(
            self._session,
            workspace_id=task.workspace_id,
            event_type="task.lifecycle_changed" if changed else "task.lifecycle_unchanged",
            actor=actor,
            caller=caller,
            channel=channel,
            resource_type="task",
            resource_id=task.id,
            request_id=request_id,
            details={
                "task_id": str(task.id),
                "task_key": task.task_key,
                "previous_state": previous.value,
                "lifecycle_state": target.value,
                "reason": normalized_reason,
                "changed": changed,
                "idempotency_record_id": str(claim.record_id),
            },
        )
        self._session.flush()
        response_body = _task_lifecycle_response(
            task,
            previous_state=previous,
            target_state=target,
            reason=normalized_reason,
            changed=changed,
            idempotency_record_id=claim.record_id,
            audit_event_id=audit_event.id,
        )
        completed = self.complete_idempotency(
            claim,
            actor_identity=actor_identity,
            caller_identity=caller_identity,
            response_status=200,
            response_body=response_body,
            request_id=request_id,
        )
        return _task_lifecycle_result_from_claim(completed, replayed=False)

    def claim_idempotency(
        self,
        *,
        actor_identity: ExternalIdentity,
        caller_identity: ExternalIdentity,
        workspace_slug: str,
        required_permission: Permission,
        operation: str,
        idempotency_key: str,
        request_payload: Any,
        channel: AuditChannel,
        task_key: str | None = None,
    ) -> IdempotencyClaim:
        if _IDEMPOTENCY_OPERATION.fullmatch(operation) is None or not idempotency_key.strip():
            raise ValueError("operation must be a safe identifier and idempotency_key must not be blank")
        if channel == AuditChannel.SYSTEM:
            raise AuthorizationDenied(
                _decision(required_permission, AuthorizationReason.INVALID_AUDIT_CONTEXT),
            )
        if task_key is None:
            decision = self.authorize_workspace(actor_identity, workspace_slug, required_permission)
        else:
            decision = self.authorize_task(actor_identity, workspace_slug, task_key, required_permission)
        self.require(decision)
        caller = self._require_caller(caller_identity, decision.principal.id, required_permission)

        request_fingerprint = canonical_request_fingerprint(request_payload)
        key_hash = idempotency_key_hash(idempotency_key)
        resource_type = "task" if decision.task is not None else "workspace"
        resource_id = decision.task.id if decision.task is not None else decision.workspace.id
        candidate_id = uuid.uuid4()
        values = {
            "id": candidate_id,
            "workspace_id": decision.workspace.id,
            "actor_principal_id": decision.principal.id,
            "caller_principal_id": caller.id,
            "operation": operation,
            "idempotency_key_hash": key_hash,
            "request_fingerprint": request_fingerprint,
            "required_permission": required_permission.value,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "channel": channel,
            "state": IdempotencyState.PENDING,
        }
        dialect_name = self._session.get_bind().dialect.name
        if dialect_name == "postgresql":
            statement = postgresql_insert(IdempotencyRecord).values(**values).on_conflict_do_nothing(
                index_elements=["workspace_id", "operation", "idempotency_key_hash"],
            ).returning(IdempotencyRecord.id)
        elif dialect_name == "sqlite":
            statement = sqlite_insert(IdempotencyRecord).values(**values).on_conflict_do_nothing(
                index_elements=["workspace_id", "operation", "idempotency_key_hash"],
            ).returning(IdempotencyRecord.id)
        else:
            raise AuthorizationUnavailable(f"unsupported idempotency dialect: {dialect_name}")
        inserted = self._session.scalar(statement) == candidate_id
        record_statement = select(IdempotencyRecord).where(
            IdempotencyRecord.workspace_id == decision.workspace.id,
            IdempotencyRecord.operation == operation,
            IdempotencyRecord.idempotency_key_hash == key_hash,
        )
        if dialect_name != "postgresql":
            record_statement = record_statement.with_for_update()
        record = self._session.scalar(record_statement)
        if record is None:
            raise AuthorizationUnavailable("idempotency claim was not readable after insert")
        if (
            record.actor_principal_id != decision.principal.id
            or record.caller_principal_id != caller.id
            or record.request_fingerprint != request_fingerprint
            or record.required_permission != required_permission.value
            or record.resource_type != resource_type
            or record.resource_id != resource_id
            or record.channel != channel
        ):
            raise IdempotencyConflict()
        if inserted:
            status = IdempotencyClaimStatus.CLAIMED
        elif record.state == IdempotencyState.PENDING:
            status = IdempotencyClaimStatus.PENDING
        else:
            status = IdempotencyClaimStatus.REPLAY
        return _idempotency_claim(record, status)

    def complete_idempotency(
        self,
        claim: IdempotencyClaim,
        *,
        actor_identity: ExternalIdentity,
        caller_identity: ExternalIdentity,
        response_status: int,
        response_body: dict[str, Any] | None,
        succeeded: bool = True,
        request_id: str | None = None,
        _allow_membership_self_transition: bool = False,
    ) -> IdempotencyClaim:
        if type(response_status) is not int:
            raise ValueError("response_status must be an integer")
        if type(succeeded) is not bool:
            raise ValueError("succeeded must be a boolean")
        normalized_body = (
            None if response_body is None else normalize_sensitive_json_object(response_body)
        )
        canonical_body = (
            None if normalized_body is None else canonical_sensitive_json_bytes(normalized_body)
        )
        if self._session.get_bind().dialect.name == "postgresql":
            return self._complete_idempotency_postgresql(
                claim,
                actor_identity=actor_identity,
                caller_identity=caller_identity,
                response_status=response_status,
                canonical_body=canonical_body,
                succeeded=succeeded,
                request_id=request_id,
            )

        record = self._session.scalar(
            select(IdempotencyRecord)
            .where(IdempotencyRecord.id == claim.record_id)
            .execution_options(populate_existing=True),
        )
        if record is None or not _idempotency_claim_matches_record(claim, record):
            raise IdempotencyConflict()
        self._lock_idempotency_context(record)
        record = self._session.scalar(
            select(IdempotencyRecord)
            .where(IdempotencyRecord.id == claim.record_id)
            .with_for_update()
            .execution_options(populate_existing=True),
        )
        if record is None or not _idempotency_claim_matches_record(claim, record):
            raise IdempotencyConflict()
        actor, caller, _, channel = self._reauthorize_idempotency_completion(
            record,
            actor_identity=actor_identity,
            caller_identity=caller_identity,
            response_body=normalized_body,
            allow_membership_self_transition=_allow_membership_self_transition,
        )
        target_state = IdempotencyState.SUCCEEDED if succeeded else IdempotencyState.FAILED
        if record.state == IdempotencyState.PENDING:
            with sqlite_idempotency_completion_gate(self._session):
                record.state = target_state
                record.response_status = response_status
                record.response_body = normalized_body
                append_audit_event(
                    self._session,
                    workspace_id=record.workspace_id,
                    event_type="idempotency.completed",
                    actor=actor,
                    caller=caller,
                    channel=channel,
                    resource_type=record.resource_type,
                    resource_id=record.resource_id,
                    request_id=request_id,
                    details={
                        "idempotency_record_id": str(record.id),
                        "operation": record.operation,
                        "response_status": response_status,
                        "state": target_state.value,
                    },
                )
                self._session.flush()
        else:
            stored_body = (
                None
                if record.response_body is None
                else canonical_sensitive_json_bytes(record.response_body)
            )
            if (
                record.state != target_state
                or record.response_status != response_status
                or stored_body != canonical_body
            ):
                raise IdempotencyConflict()
        return _idempotency_claim(record, IdempotencyClaimStatus.REPLAY)

    def _complete_idempotency_postgresql(
        self,
        claim: IdempotencyClaim,
        *,
        actor_identity: ExternalIdentity,
        caller_identity: ExternalIdentity,
        response_status: int,
        canonical_body: bytes | None,
        succeeded: bool,
        request_id: str | None,
    ) -> IdempotencyClaim:
        actor = self._find_principal(actor_identity, populate_existing=True)
        caller = self._find_principal(caller_identity, populate_existing=True)
        if (
            actor is None
            or caller is None
            or actor.id != claim.actor_principal_id
            or caller.id != claim.caller_principal_id
        ):
            raise IdempotencyConflict()
        result = self._session.execute(
            text(
                """
                SELECT outcome, state, response_status, response_body, reason
                FROM public.lls_complete_idempotency(
                    :record_id,
                    :workspace_id,
                    :actor_principal_id,
                    :caller_principal_id,
                    :operation,
                    :idempotency_key_hash,
                    :request_fingerprint,
                    :required_permission,
                    :resource_type,
                    :resource_id,
                    :channel,
                    :response_status,
                    :response_body,
                    :succeeded,
                    :request_id
                )
                """
            ),
            {
                "record_id": claim.record_id,
                "workspace_id": claim.workspace_id,
                "actor_principal_id": claim.actor_principal_id,
                "caller_principal_id": claim.caller_principal_id,
                "operation": claim.operation,
                "idempotency_key_hash": claim.idempotency_key_hash,
                "request_fingerprint": claim.request_fingerprint,
                "required_permission": claim.required_permission.value,
                "resource_type": claim.resource_type,
                "resource_id": claim.resource_id,
                "channel": claim.channel.value,
                "response_status": response_status,
                "response_body": None if canonical_body is None else canonical_body.decode("utf-8"),
                "succeeded": succeeded,
                "request_id": request_id,
            },
        ).mappings().one()
        outcome = result["outcome"]
        if outcome == "conflict":
            raise IdempotencyConflict()
        if outcome == "denied":
            try:
                reason = AuthorizationReason(result["reason"])
            except ValueError as exc:
                raise AuthorizationUnavailable("database authorization state is unavailable") from exc
            raise AuthorizationDenied(_decision(claim.required_permission, reason))
        if outcome not in {"completed", "replay"}:
            raise AuthorizationUnavailable("database authorization state is unavailable")
        response_value = result["response_body"]
        if isinstance(response_value, str):
            response_value = json.loads(response_value)
        return IdempotencyClaim(
            record_id=claim.record_id,
            workspace_id=claim.workspace_id,
            actor_principal_id=claim.actor_principal_id,
            caller_principal_id=claim.caller_principal_id,
            operation=claim.operation,
            idempotency_key_hash=claim.idempotency_key_hash,
            request_fingerprint=claim.request_fingerprint,
            required_permission=claim.required_permission,
            resource_type=claim.resource_type,
            resource_id=claim.resource_id,
            channel=claim.channel,
            status=IdempotencyClaimStatus.REPLAY,
            state=IdempotencyState(result["state"]),
            response_status=result["response_status"],
            response_body=response_value,
        )

    def _lock_idempotency_context(self, record: IdempotencyRecord) -> None:
        self._session.scalar(
            select(Workspace)
            .where(Workspace.id == record.workspace_id)
            .with_for_update()
            .execution_options(populate_existing=True),
        )
        principal_ids = sorted(
            {record.actor_principal_id, record.caller_principal_id},
            key=str,
        )
        if principal_ids:
            self._session.scalars(
                select(Principal)
                .where(Principal.id.in_(principal_ids))
                .order_by(Principal.id)
                .with_for_update()
                .execution_options(populate_existing=True),
            ).all()
        binding_query = (
            select(RoleBinding)
            .where(
                RoleBinding.workspace_id == record.workspace_id,
                RoleBinding.principal_id.in_(principal_ids),
                or_(
                    RoleBinding.task_id.is_(None),
                    and_(
                        record.resource_type == "task",
                        RoleBinding.task_id == record.resource_id,
                    ),
                ),
            )
            .order_by(RoleBinding.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        self._session.scalars(binding_query).all()
        if record.resource_type == "task":
            self._session.scalar(
                select(Task)
                .where(Task.id == record.resource_id, Task.workspace_id == record.workspace_id)
                .with_for_update()
                .execution_options(populate_existing=True),
            )

    def _reauthorize_idempotency_completion(
        self,
        record: IdempotencyRecord,
        *,
        actor_identity: ExternalIdentity,
        caller_identity: ExternalIdentity,
        response_body: dict[str, Any] | None = None,
        allow_membership_self_transition: bool = False,
    ) -> tuple[Principal, Principal, Permission, AuditChannel]:
        try:
            permission = Permission(record.required_permission)
            channel = AuditChannel(record.channel)
        except ValueError as exc:
            raise IdempotencyConflict() from exc
        actor = self._find_principal(actor_identity, populate_existing=True)
        caller = self._find_principal(caller_identity, populate_existing=True)
        if (
            actor is None
            or caller is None
            or actor.id != record.actor_principal_id
            or caller.id != record.caller_principal_id
        ):
            raise IdempotencyConflict()

        if record.resource_type == "workspace":
            if record.resource_id != record.workspace_id:
                raise IdempotencyConflict()
            workspace = self._session.get(
                Workspace,
                record.workspace_id,
                populate_existing=True,
            )
            if workspace is None:
                raise AuthorizationDenied(
                    _decision(
                        permission,
                        AuthorizationReason.RESOURCE_NOT_VISIBLE,
                        principal=_principal_ref(actor),
                    )
                )
            decision = self.authorize_workspace(actor_identity, workspace.slug, permission)
        elif record.resource_type == "task":
            row = self._session.execute(
                select(Workspace, Task)
                .join(Task, Task.workspace_id == Workspace.id)
                .where(
                    Workspace.id == record.workspace_id,
                    Task.id == record.resource_id,
                )
                .execution_options(populate_existing=True),
            ).first()
            if row is None:
                raise AuthorizationDenied(
                    _decision(
                        permission,
                        AuthorizationReason.RESOURCE_NOT_VISIBLE,
                        principal=_principal_ref(actor),
                    )
                )
            workspace, task = row
            decision = self.authorize_task(
                actor_identity,
                workspace.slug,
                task.task_key,
                permission,
            )
        else:
            raise IdempotencyConflict()
        if not decision.allowed and not (
            allow_membership_self_transition
            and self._valid_membership_self_transition(record, actor, response_body)
        ):
            self.require(decision)
        authorized_caller = self._require_caller(caller_identity, actor.id, permission)
        if authorized_caller.id != record.caller_principal_id:
            raise IdempotencyConflict()
        return actor, authorized_caller, permission, channel

    def _valid_membership_self_transition(
        self,
        record: IdempotencyRecord,
        actor: Principal,
        response_body: dict[str, Any] | None,
    ) -> bool:
        if (
            record.resource_type != "workspace"
            or record.resource_id != record.workspace_id
            or record.required_permission != Permission.WORKSPACE_MANAGE.value
            or record.operation not in {
                "workspace.membership.role",
                "workspace.membership.revoke",
            }
            or response_body is None
        ):
            return False
        membership = response_body.get("membership")
        if not isinstance(membership, dict):
            return False
        principal = membership.get("principal")
        if not isinstance(principal, dict) or principal.get("principal_id") != str(actor.id):
            return False
        if membership.get("previous_role") != Role.ADMIN.value:
            return False
        workspace = self._session.get(Workspace, record.workspace_id, populate_existing=True)
        if workspace is None or not workspace.is_active:
            return False
        binding = self._workspace_binding(record.workspace_id, actor.id, for_update=False)
        if record.operation == "workspace.membership.role":
            return (
                membership.get("action") == MembershipMutationAction.CHANGED.value
                and membership.get("role") in {
                    Role.VIEWER.value,
                    Role.ANNOTATOR.value,
                    Role.EXPERIMENTER.value,
                }
                and binding is not None
                and binding.role.value == membership.get("role")
            )
        return (
            membership.get("action") == MembershipMutationAction.REVOKED.value
            and membership.get("role") is None
            and binding is None
        )

    def create_workspace_invitation(
        self,
        *,
        workspace_slug: str,
        email: str,
        role: Role,
        actor_identity: ExternalIdentity,
        caller_identity: ExternalIdentity,
        idempotency_key: str,
        channel: AuditChannel,
        request_id: str | None = None,
    ) -> WorkspaceInvitationResult:
        normalized_email = normalize_invitation_email(email)
        normalized_role = normalize_role(role)
        claim = self.claim_idempotency(
            actor_identity=actor_identity,
            caller_identity=caller_identity,
            workspace_slug=workspace_slug,
            required_permission=Permission.WORKSPACE_MANAGE,
            operation="workspace.invitation.create",
            idempotency_key=idempotency_key,
            request_payload={
                "invitee_email_hash": _invitation_email_fingerprint(normalized_email),
                "role": normalized_role.value,
            },
            channel=channel,
        )
        if claim.status == IdempotencyClaimStatus.REPLAY:
            return _workspace_invitation_result_from_claim(self._session, claim, replayed=True)
        if claim.status == IdempotencyClaimStatus.PENDING:
            raise MembershipOperationInProgress("workspace invitation is already in progress")

        workspace = self._session.scalar(
            select(Workspace)
            .where(Workspace.id == claim.workspace_id, Workspace.slug == workspace_slug)
            .with_for_update()
            .execution_options(populate_existing=True),
        )
        if workspace is None:
            raise AuthorizationDenied(
                _decision(
                    Permission.WORKSPACE_MANAGE,
                    AuthorizationReason.RESOURCE_NOT_VISIBLE,
                )
            )
        self._expire_workspace_invitations(workspace.id, email=normalized_email)
        invitation = self._session.scalar(
            select(WorkspaceInvitation)
            .where(
                WorkspaceInvitation.workspace_id == workspace.id,
                WorkspaceInvitation.email == normalized_email,
                WorkspaceInvitation.status == WorkspaceInvitationStatus.PENDING,
            )
            .with_for_update()
            .execution_options(populate_existing=True),
        )
        if invitation is not None and invitation.role != normalized_role:
            raise InvitationConflict("a pending invitation already exists with another role")
        action = "unchanged"
        if invitation is None:
            invitation = WorkspaceInvitation(
                workspace_id=workspace.id,
                email=normalized_email,
                role=normalized_role,
                status=WorkspaceInvitationStatus.PENDING,
                expires_at=datetime.now(timezone.utc) + DEFAULT_INVITATION_TTL,
                created_by_principal_id=claim.actor_principal_id,
            )
            self._session.add(invitation)
            self._session.flush()
            action = "created"
            append_audit_event(
                self._session,
                workspace_id=workspace.id,
                event_type="workspace.invitation_created",
                actor=self._session.get(Principal, claim.actor_principal_id),
                caller=self._session.get(Principal, claim.caller_principal_id),
                channel=channel,
                resource_type="workspace_invitation",
                resource_id=invitation.id,
                request_id=request_id,
                details={
                    "invitation_id": str(invitation.id),
                    "role": normalized_role.value,
                },
            )
            self._session.flush()

        response = _workspace_invitation_response(
            invitation,
            workspace,
            action=action,
            claim_status="pending",
        )
        completed = self.complete_idempotency(
            claim,
            actor_identity=actor_identity,
            caller_identity=caller_identity,
            response_status=201 if action == "created" else 200,
            response_body=response,
            request_id=request_id,
        )
        return WorkspaceInvitationResult(
            action=action,
            invitation=_workspace_invitation_ref(invitation, workspace),
            claim_status="pending",
            response_status=completed.response_status or (201 if action == "created" else 200),
            replayed=False,
        )

    def change_workspace_membership_by_principal_id(
        self,
        *,
        workspace_slug: str,
        principal_id: uuid.UUID,
        role: Role,
        actor_identity: ExternalIdentity,
        caller_identity: ExternalIdentity,
        idempotency_key: str,
        channel: AuditChannel,
        request_id: str | None = None,
    ) -> IdempotentMembershipMutationResult:
        normalized_principal_id = _principal_uuid(principal_id)
        normalized_role = normalize_role(role)
        claim = self.claim_idempotency(
            actor_identity=actor_identity,
            caller_identity=caller_identity,
            workspace_slug=workspace_slug,
            required_permission=Permission.WORKSPACE_MANAGE,
            operation="workspace.membership.role",
            idempotency_key=idempotency_key,
            request_payload={
                "principal_id": str(normalized_principal_id),
                "role": normalized_role.value,
            },
            channel=channel,
        )
        if claim.status == IdempotencyClaimStatus.REPLAY:
            return _membership_mutation_result_from_claim(self._session, claim, replayed=True)
        if claim.status == IdempotencyClaimStatus.PENDING:
            raise MembershipOperationInProgress("workspace membership role change is already in progress")
        target = self._target_member_for_workspace(
            workspace_slug=workspace_slug,
            principal_id=normalized_principal_id,
            actor_identity=actor_identity,
        )
        result = self.change_workspace_membership(
            workspace_slug=workspace_slug,
            target_identity=ExternalIdentity(target.issuer, target.subject),
            role=normalized_role,
            actor_identity=actor_identity,
            caller_identity=caller_identity,
            channel=channel,
            request_id=request_id,
        )
        response = {"membership": _membership_result_payload(result)}
        completed = self.complete_idempotency(
            claim,
            actor_identity=actor_identity,
            caller_identity=caller_identity,
            response_status=200,
            response_body=response,
            request_id=request_id,
            _allow_membership_self_transition=normalized_principal_id == claim.actor_principal_id,
        )
        return IdempotentMembershipMutationResult(
            result=result,
            response_status=completed.response_status or 200,
            replayed=False,
        )

    def revoke_workspace_membership_by_principal_id(
        self,
        *,
        workspace_slug: str,
        principal_id: uuid.UUID,
        actor_identity: ExternalIdentity,
        caller_identity: ExternalIdentity,
        idempotency_key: str,
        channel: AuditChannel,
        request_id: str | None = None,
    ) -> IdempotentMembershipMutationResult:
        normalized_principal_id = _principal_uuid(principal_id)
        claim = self.claim_idempotency(
            actor_identity=actor_identity,
            caller_identity=caller_identity,
            workspace_slug=workspace_slug,
            required_permission=Permission.WORKSPACE_MANAGE,
            operation="workspace.membership.revoke",
            idempotency_key=idempotency_key,
            request_payload={"principal_id": str(normalized_principal_id)},
            channel=channel,
        )
        if claim.status == IdempotencyClaimStatus.REPLAY:
            return _membership_mutation_result_from_claim(self._session, claim, replayed=True)
        if claim.status == IdempotencyClaimStatus.PENDING:
            raise MembershipOperationInProgress("workspace membership revoke is already in progress")
        target = self._target_member_for_workspace(
            workspace_slug=workspace_slug,
            principal_id=normalized_principal_id,
            actor_identity=actor_identity,
        )
        result = self.revoke_workspace_membership(
            workspace_slug=workspace_slug,
            target_identity=ExternalIdentity(target.issuer, target.subject),
            actor_identity=actor_identity,
            caller_identity=caller_identity,
            channel=channel,
            request_id=request_id,
        )
        response = {"membership": _membership_result_payload(result)}
        completed = self.complete_idempotency(
            claim,
            actor_identity=actor_identity,
            caller_identity=caller_identity,
            response_status=200,
            response_body=response,
            request_id=request_id,
            _allow_membership_self_transition=normalized_principal_id == claim.actor_principal_id,
        )
        return IdempotentMembershipMutationResult(
            result=result,
            response_status=completed.response_status or 200,
            replayed=False,
        )

    def _target_member_for_workspace(
        self,
        *,
        workspace_slug: str,
        principal_id: uuid.UUID,
        actor_identity: ExternalIdentity,
    ) -> Principal:
        decision = self.authorize_workspace(actor_identity, workspace_slug, Permission.WORKSPACE_MANAGE)
        self.require(decision)
        if decision.workspace is None:
            raise MembershipNotFound("workspace membership does not exist")
        target = self._session.scalar(
            select(Principal)
            .join(
                RoleBinding,
                and_(
                    RoleBinding.principal_id == Principal.id,
                    RoleBinding.workspace_id == decision.workspace.id,
                    RoleBinding.task_id.is_(None),
                ),
            )
            .where(Principal.id == principal_id)
            .execution_options(populate_existing=True),
        )
        if target is None:
            raise MembershipNotFound("workspace membership does not exist")
        return target

    def grant_workspace_membership(
        self,
        *,
        workspace_slug: str,
        target_identity: ExternalIdentity,
        role: Role,
        actor_identity: ExternalIdentity,
        caller_identity: ExternalIdentity,
        channel: AuditChannel,
        request_id: str | None = None,
    ) -> MembershipMutationResult:
        actor, caller, workspace, target, binding = self._membership_context(
            workspace_slug=workspace_slug,
            target_identity=target_identity,
            actor_identity=actor_identity,
            caller_identity=caller_identity,
            channel=channel,
            require_active_target=True,
        )
        if binding is not None:
            if binding.role != role:
                raise MembershipConflict("workspace membership already exists with another role")
            return _membership_result(
                MembershipMutationAction.UNCHANGED,
                workspace,
                target,
                binding=binding,
                previous_role=role,
                role=role,
            )

        binding = RoleBinding(
            workspace_id=workspace.id,
            principal_id=target.id,
            role=role,
            created_by_principal_id=actor.id,
        )
        self._session.add(binding)
        self._session.flush()
        audit_event = self._append_membership_audit(
            workspace=workspace,
            actor=actor,
            caller=caller,
            target=target,
            binding=binding,
            event_type="workspace.membership_granted",
            channel=channel,
            request_id=request_id,
            previous_role=None,
            role=role,
        )
        return _membership_result(
            MembershipMutationAction.GRANTED,
            workspace,
            target,
            binding=binding,
            previous_role=None,
            role=role,
            audit_event=audit_event,
        )

    def change_workspace_membership(
        self,
        *,
        workspace_slug: str,
        target_identity: ExternalIdentity,
        role: Role,
        actor_identity: ExternalIdentity,
        caller_identity: ExternalIdentity,
        channel: AuditChannel,
        request_id: str | None = None,
    ) -> MembershipMutationResult:
        actor, caller, workspace, target, binding = self._membership_context(
            workspace_slug=workspace_slug,
            target_identity=target_identity,
            actor_identity=actor_identity,
            caller_identity=caller_identity,
            channel=channel,
            require_active_target=True,
        )
        if binding is None:
            raise MembershipNotFound("workspace membership does not exist")
        previous_role = binding.role
        if previous_role == role:
            return _membership_result(
                MembershipMutationAction.UNCHANGED,
                workspace,
                target,
                binding=binding,
                previous_role=previous_role,
                role=role,
            )
        if previous_role == Role.ADMIN and role != Role.ADMIN:
            self._require_another_workspace_admin(workspace.id, binding.id)

        binding.role = role
        self._session.flush()
        audit_event = self._append_membership_audit(
            workspace=workspace,
            actor=actor,
            caller=caller,
            target=target,
            binding=binding,
            event_type="workspace.membership_changed",
            channel=channel,
            request_id=request_id,
            previous_role=previous_role,
            role=role,
        )
        return _membership_result(
            MembershipMutationAction.CHANGED,
            workspace,
            target,
            binding=binding,
            previous_role=previous_role,
            role=role,
            audit_event=audit_event,
        )

    def revoke_workspace_membership(
        self,
        *,
        workspace_slug: str,
        target_identity: ExternalIdentity,
        actor_identity: ExternalIdentity,
        caller_identity: ExternalIdentity,
        channel: AuditChannel,
        request_id: str | None = None,
    ) -> MembershipMutationResult:
        actor, caller, workspace, target, binding = self._membership_context(
            workspace_slug=workspace_slug,
            target_identity=target_identity,
            actor_identity=actor_identity,
            caller_identity=caller_identity,
            channel=channel,
            require_active_target=False,
        )
        if binding is None:
            return _membership_result(
                MembershipMutationAction.UNCHANGED,
                workspace,
                target,
                binding=None,
                previous_role=None,
                role=None,
            )
        previous_role = binding.role
        if previous_role == Role.ADMIN and target.is_active:
            self._require_another_workspace_admin(workspace.id, binding.id)

        binding_id = binding.id
        audit_event = self._append_membership_audit(
            workspace=workspace,
            actor=actor,
            caller=caller,
            target=target,
            binding=binding,
            event_type="workspace.membership_revoked",
            channel=channel,
            request_id=request_id,
            previous_role=previous_role,
            role=None,
        )
        self._session.delete(binding)
        self._session.flush()
        return MembershipMutationResult(
            action=MembershipMutationAction.REVOKED,
            workspace=_workspace_ref(workspace),
            principal=_principal_ref(target),
            binding_id=binding_id,
            previous_role=previous_role,
            role=None,
            audit_event_id=audit_event.id,
        )

    def append_audit(
        self,
        *,
        workspace_slug: str,
        event_type: str,
        actor_identity: ExternalIdentity,
        caller_identity: ExternalIdentity,
        channel: AuditChannel,
        required_permission: Permission,
        task_key: str | None = None,
        request_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditEventRef:
        if channel == AuditChannel.SYSTEM:
            raise AuthorizationDenied(
                _decision(required_permission, AuthorizationReason.INVALID_AUDIT_CONTEXT),
            )
        if task_key is None:
            decision = self.authorize_workspace(actor_identity, workspace_slug, required_permission)
        else:
            decision = self.authorize_task(actor_identity, workspace_slug, task_key, required_permission)
        self.require(decision)

        actor = self._find_principal(actor_identity)
        if actor is None or not actor.is_active:
            raise AuthorizationDenied(
                _decision(required_permission, AuthorizationReason.INVALID_AUDIT_CONTEXT),
            )
        caller = self._require_caller(caller_identity, actor.id, required_permission)
        resource_type = "task" if decision.task is not None else "workspace"
        resource_id = decision.task.id if decision.task is not None else decision.workspace.id

        event = append_audit_event(
            self._session,
            workspace_id=decision.workspace.id,
            event_type=event_type,
            actor=actor,
            caller=caller,
            channel=channel,
            resource_type=resource_type,
            resource_id=resource_id,
            request_id=request_id,
            details=details,
        )
        self._session.flush()
        return _audit_event_ref(event)

    def _membership_context(
        self,
        *,
        workspace_slug: str,
        target_identity: ExternalIdentity,
        actor_identity: ExternalIdentity,
        caller_identity: ExternalIdentity,
        channel: AuditChannel,
        require_active_target: bool,
    ) -> tuple[Principal, Principal, Workspace, Principal, RoleBinding | None]:
        if channel == AuditChannel.SYSTEM:
            raise AuthorizationDenied(
                _decision(Permission.WORKSPACE_MANAGE, AuthorizationReason.INVALID_AUDIT_CONTEXT),
            )

        initial_decision = self.authorize_workspace(
            actor_identity,
            workspace_slug,
            Permission.WORKSPACE_MANAGE,
        )
        self.require(initial_decision)
        actor = self._find_principal(actor_identity, populate_existing=True)
        if actor is None:
            raise AuthorizationDenied(
                _decision(Permission.WORKSPACE_MANAGE, AuthorizationReason.UNKNOWN_PRINCIPAL),
            )
        caller = self._find_principal(caller_identity, populate_existing=True)
        target = self._find_principal(target_identity, populate_existing=True)
        if target is None:
            raise MembershipNotFound("target principal does not exist")
        self._workspace_binding(
            initial_decision.workspace.id,
            target.id,
            for_update=False,
        )
        workspace = self._session.scalar(
            select(Workspace)
            .where(Workspace.slug == workspace_slug)
            .execution_options(populate_existing=True),
        )
        actor_ref = _principal_ref(actor)
        if workspace is None:
            raise AuthorizationDenied(
                _decision(
                    Permission.WORKSPACE_MANAGE,
                    AuthorizationReason.RESOURCE_NOT_VISIBLE,
                    principal=actor_ref,
                ),
            )

        workspace = self._session.scalar(
            select(Workspace)
            .where(Workspace.id == workspace.id)
            .with_for_update()
            .execution_options(populate_existing=True),
        )
        principal_ids = sorted(
            {
                principal.id
                for principal in (actor, caller, target)
                if principal is not None
            },
            key=str,
        )
        if principal_ids:
            self._session.scalars(
                select(Principal)
                .where(Principal.id.in_(principal_ids))
                .order_by(Principal.id)
                .with_for_update()
                .execution_options(populate_existing=True),
            ).all()
            self._session.scalars(
                select(RoleBinding)
                .where(
                    RoleBinding.workspace_id == workspace.id,
                    RoleBinding.principal_id.in_(principal_ids),
                )
                .order_by(RoleBinding.id)
                .with_for_update()
                .execution_options(populate_existing=True),
            ).all()

        actor = self._find_principal(actor_identity, populate_existing=True)
        caller = self._find_principal(caller_identity, populate_existing=True)
        target = self._find_principal(target_identity, populate_existing=True)
        workspace = self._session.scalar(
            select(Workspace)
            .where(Workspace.id == workspace.id)
            .execution_options(populate_existing=True),
        )
        if actor is None:
            raise AuthorizationDenied(
                _decision(Permission.WORKSPACE_MANAGE, AuthorizationReason.UNKNOWN_PRINCIPAL),
            )
        actor_ref = _principal_ref(actor)
        if not actor.is_active:
            raise AuthorizationDenied(
                _decision(
                    Permission.WORKSPACE_MANAGE,
                    AuthorizationReason.INACTIVE_PRINCIPAL,
                    principal=actor_ref,
                ),
            )
        if workspace is None:
            raise AuthorizationDenied(
                _decision(
                    Permission.WORKSPACE_MANAGE,
                    AuthorizationReason.RESOURCE_NOT_VISIBLE,
                    principal=actor_ref,
                ),
            )

        roles = self._roles(actor.id, workspace.id)
        workspace_ref = _workspace_ref(workspace)
        if not workspace.is_active:
            raise AuthorizationDenied(
                _decision(
                    Permission.WORKSPACE_MANAGE,
                    AuthorizationReason.INACTIVE_WORKSPACE,
                    principal=actor_ref,
                    workspace=workspace_ref,
                    roles=roles,
                ),
            )
        self.require(
            _role_decision(
                Permission.WORKSPACE_MANAGE,
                actor_ref,
                workspace_ref,
                None,
                roles,
            )
        )
        caller = self._require_caller(caller_identity, actor.id, Permission.WORKSPACE_MANAGE)
        if target is None or (require_active_target and not target.is_active):
            status = "active " if require_active_target else ""
            raise MembershipNotFound(f"{status}target principal does not exist")
        binding = self._workspace_binding(workspace.id, target.id)
        return actor, caller, workspace, target, binding

    def _workspace_binding(
        self,
        workspace_id: uuid.UUID,
        principal_id: uuid.UUID,
        *,
        for_update: bool = True,
    ) -> RoleBinding | None:
        statement = select(RoleBinding).where(
            RoleBinding.workspace_id == workspace_id,
            RoleBinding.principal_id == principal_id,
            RoleBinding.task_id.is_(None),
        )
        if for_update:
            statement = statement.with_for_update()
        return self._session.scalar(statement.execution_options(populate_existing=True))

    def _require_another_workspace_admin(
        self,
        workspace_id: uuid.UUID,
        excluded_binding_id: uuid.UUID,
    ) -> None:
        count = self._session.scalar(
            select(func.count())
            .select_from(RoleBinding)
            .join(Principal, Principal.id == RoleBinding.principal_id)
            .where(
                RoleBinding.workspace_id == workspace_id,
                RoleBinding.task_id.is_(None),
                RoleBinding.role == Role.ADMIN,
                RoleBinding.id != excluded_binding_id,
                Principal.is_active.is_(True),
            ),
        )
        if not count:
            raise LastWorkspaceAdmin("the last workspace admin cannot be revoked or downgraded")

    def _append_membership_audit(
        self,
        *,
        workspace: Workspace,
        actor: Principal,
        caller: Principal,
        target: Principal,
        binding: RoleBinding,
        event_type: str,
        channel: AuditChannel,
        request_id: str | None,
        previous_role: Role | None,
        role: Role | None,
    ) -> AuditEvent:
        event = append_audit_event(
            self._session,
            workspace_id=workspace.id,
            event_type=event_type,
            actor=actor,
            caller=caller,
            channel=channel,
            resource_type="workspace_membership",
            resource_id=binding.id,
            request_id=request_id,
            details={
                "target_principal_id": str(target.id),
                "previous_role": previous_role.value if previous_role is not None else None,
                "role": role.value if role is not None else None,
            },
        )
        self._session.flush()
        return event

    def _find_principal(
        self,
        identity: ExternalIdentity | None,
        *,
        populate_existing: bool = False,
    ) -> Principal | None:
        if identity is None:
            return None
        statement = select(Principal).where(
            Principal.issuer == identity.issuer,
            Principal.subject == identity.subject,
        )
        if populate_existing:
            statement = statement.execution_options(populate_existing=True)
        return self._session.scalar(statement)

    def _lock_task(self, task_id: uuid.UUID) -> Task:
        task = self._session.scalar(
            select(Task).where(Task.id == task_id).with_for_update(),
        )
        if task is None:
            raise AuthorizationUnavailable("authorized task disappeared during transaction")
        return task

    def _lock_task_by_id(self, workspace_id: uuid.UUID, task_key: str) -> Task:
        task = self._session.scalar(
            select(Task)
            .where(Task.workspace_id == workspace_id, Task.task_key == task_key)
            .with_for_update(),
        )
        if task is None:
            raise AuthorizationUnavailable("authorized task disappeared during publish")
        return task

    def _require_caller(
        self,
        identity: ExternalIdentity,
        actor_principal_id: uuid.UUID,
        permission: Permission,
    ) -> Principal:
        caller = self._find_principal(identity, populate_existing=True)
        if caller is None or not caller.is_active or (
            caller.id != actor_principal_id and caller.principal_type != PrincipalType.SERVICE
        ):
            raise AuthorizationDenied(
                _decision(permission, AuthorizationReason.INVALID_AUDIT_CONTEXT),
            )
        return caller

    def _roles(
        self,
        principal_id: uuid.UUID,
        workspace_id: uuid.UUID,
        task_id: uuid.UUID | None = None,
    ) -> tuple[Role, ...]:
        scope = RoleBinding.task_id.is_(None)
        if task_id is not None:
            scope = or_(scope, RoleBinding.task_id == task_id)
        values = self._session.scalars(
            select(RoleBinding.role).where(
                RoleBinding.principal_id == principal_id,
                RoleBinding.workspace_id == workspace_id,
                scope,
            ).execution_options(populate_existing=True),
        )
        return _sorted_roles(values)

    def _list_authorized_workspaces(self, principal_id: uuid.UUID) -> tuple[WorkspaceAccess, ...]:
        workspaces = self._session.scalars(
            select(Workspace)
            .join(
                RoleBinding,
                and_(
                    RoleBinding.workspace_id == Workspace.id,
                    RoleBinding.principal_id == principal_id,
                    RoleBinding.task_id.is_(None),
                ),
            )
            .where(Workspace.is_active.is_(True))
            .distinct()
            .order_by(Workspace.slug),
        ).all()
        role_rows = self._session.execute(
            select(RoleBinding.workspace_id, RoleBinding.role).where(
                RoleBinding.principal_id == principal_id,
                RoleBinding.task_id.is_(None),
            ),
        ).all()
        roles_by_workspace: dict[uuid.UUID, set[Role]] = {}
        for workspace_id, role in role_rows:
            roles_by_workspace.setdefault(workspace_id, set()).add(role)
        access: list[WorkspaceAccess] = []
        for workspace in workspaces:
            roles = _sorted_roles(roles_by_workspace.get(workspace.id, set()))
            access.append(
                WorkspaceAccess(
                    workspace=_workspace_ref(workspace),
                    roles=roles,
                    workspace_capabilities=_capabilities(roles, WORKSPACE_PERMISSIONS),
                    task_capabilities=_capabilities(roles, TASK_PERMISSIONS),
                )
            )
        return tuple(access)


def _role_decision(
    permission: Permission,
    principal: PrincipalRef,
    workspace: WorkspaceRef,
    task: TaskRef | None,
    roles: tuple[Role, ...],
) -> AuthorizationDecision:
    allowed = any(role_allows(role, permission) for role in roles)
    reason = AuthorizationReason.ALLOWED if allowed else AuthorizationReason.ROLE_DENIED
    return AuthorizationDecision(
        allowed=allowed,
        reason=reason,
        permission=permission,
        principal=principal,
        workspace=workspace,
        task=task,
        roles=roles,
    )


def _decision(
    permission: Permission,
    reason: AuthorizationReason,
    *,
    principal: PrincipalRef | None = None,
    workspace: WorkspaceRef | None = None,
    task: TaskRef | None = None,
    roles: tuple[Role, ...] = (),
) -> AuthorizationDecision:
    return AuthorizationDecision(
        allowed=False,
        reason=reason,
        permission=permission,
        principal=principal,
        workspace=workspace,
        task=task,
        roles=roles,
    )


def _sorted_roles(values) -> tuple[Role, ...]:
    order = {role: index for index, role in enumerate(Role)}
    return tuple(sorted(set(values), key=order.__getitem__))


def _capabilities(
    roles: tuple[Role, ...],
    allowed_permissions: frozenset[Permission],
) -> tuple[Permission, ...]:
    return tuple(
        permission
        for permission in Permission
        if permission in allowed_permissions and any(role_allows(role, permission) for role in roles)
    )


def _principal_ref(principal: Principal) -> PrincipalRef:
    return PrincipalRef(
        id=principal.id,
        issuer=principal.issuer,
        subject=principal.subject,
        principal_type=principal.principal_type,
        display_name=principal.display_name,
        email_snapshot=principal.email_snapshot,
        is_active=principal.is_active,
    )


def _workspace_ref(workspace: Workspace) -> WorkspaceRef:
    return WorkspaceRef(id=workspace.id, slug=workspace.slug, name=workspace.name)


def _task_ref(task: Task, *, draft: TaskDraft | None = None) -> TaskRef:
    return TaskRef(
        id=task.id,
        workspace_id=task.workspace_id,
        task_key=task.task_key,
        current_revision_id=task.current_revision_id,
        draft_version=draft.version if draft is not None else None,
        draft_fingerprint=draft.fingerprint if draft is not None else None,
        lifecycle_state=_task_lifecycle_state(task.lifecycle_state),
    )


def _audit_event_ref(event: AuditEvent) -> AuditEventRef:
    return AuditEventRef(
        id=event.id,
        workspace_id=event.workspace_id,
        actor_principal_id=event.actor_principal_id,
        caller_principal_id=event.caller_principal_id,
        channel=event.channel,
        event_type=event.event_type,
    )


def task_definition_fingerprint(definition: dict[str, Any], rendered_task: str) -> str:
    if not isinstance(definition, dict):
        raise ValueError("definition must be a JSON object")
    if not isinstance(rendered_task, str) or not rendered_task.strip():
        raise ValueError("rendered_task must not be blank")
    return canonical_request_fingerprint(
        {
            "definition": definition,
            "rendered_task": rendered_task,
        }
    )


def task_draft_etag(version: int, fingerprint: str) -> str:
    if version < 1 or len(fingerprint) != 64:
        raise ValueError("invalid task draft version or fingerprint")
    return f'"task-draft-v{version}-{fingerprint}"'


def _require_task_key(task_key: str) -> str:
    normalized = str(task_key or "").strip()
    if (
        not normalized
        or len(normalized) > 255
        or normalized in {".", ".."}
        or "/" in normalized
        or "\\" in normalized
        or ".." in normalized
        or any(ord(char) < 32 for char in normalized)
    ):
        raise ValueError("task_key must be a safe single-segment identifier")
    return normalized


def _principal_uuid(value: uuid.UUID | str) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value).strip())
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("principal_id must be a valid UUID") from exc


def _require_draft_match(draft: TaskDraft | None, if_match: str) -> None:
    if draft is None:
        raise TaskDraftConflict(current_etag=None)
    current_etag = task_draft_etag(draft.version, draft.fingerprint)
    if if_match not in {"*", current_etag}:
        raise TaskDraftConflict(current_etag=current_etag)


def _require_task_write_channel(channel: AuditChannel) -> None:
    if channel == AuditChannel.SYSTEM:
        raise ValueError("system channel cannot be used for task writes")


def _task_draft_ref(draft: TaskDraft) -> TaskDraftRef:
    return TaskDraftRef(
        id=draft.id,
        workspace_id=draft.workspace_id,
        task_id=draft.task_id,
        version=draft.version,
        fingerprint=draft.fingerprint,
        etag=task_draft_etag(draft.version, draft.fingerprint),
        definition=draft.definition,
        rendered_task=draft.rendered_task,
    )


def _task_create_response(task: Task, draft: TaskDraft) -> dict[str, Any]:
    return {
        "kind": "task_create_v1",
        "workspace_id": str(task.workspace_id),
        "task_id": str(task.id),
        "task_key": task.task_key,
        "draft": {
            "id": str(draft.id),
            "workspace_id": str(draft.workspace_id),
            "task_id": str(draft.task_id),
            "version": draft.version,
            "fingerprint": draft.fingerprint,
            "etag": task_draft_etag(draft.version, draft.fingerprint),
            "definition": draft.definition,
            "rendered_task": draft.rendered_task,
        },
    }


def _task_create_result_from_claim(
    claim: IdempotencyClaim,
    *,
    replayed: bool,
) -> TaskCreateResult:
    body = claim.response_body
    if claim.response_status != 201 or body is None or body.get("kind") != "task_create_v1":
        raise AuthorizationUnavailable("task create idempotency response is invalid")
    try:
        draft_body = body["draft"]
        draft = TaskDraftRef(
            id=uuid.UUID(draft_body["id"]),
            workspace_id=uuid.UUID(draft_body["workspace_id"]),
            task_id=uuid.UUID(draft_body["task_id"]),
            version=int(draft_body["version"]),
            fingerprint=draft_body["fingerprint"],
            etag=draft_body["etag"],
            definition=draft_body["definition"],
            rendered_task=draft_body["rendered_task"],
        )
        return TaskCreateResult(
            workspace_id=uuid.UUID(body["workspace_id"]),
            task_id=uuid.UUID(body["task_id"]),
            task_key=str(body["task_key"]),
            draft=draft,
            response_status=claim.response_status,
            replayed=replayed,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AuthorizationUnavailable("task create idempotency response is invalid") from exc


def _task_revision_ref(revision: TaskRevision) -> TaskRevisionRef:
    return TaskRevisionRef(
        id=revision.id,
        workspace_id=revision.workspace_id,
        task_id=revision.task_id,
        revision_number=revision.revision_number,
        draft_version=revision.draft_version,
        draft_fingerprint=revision.draft_fingerprint,
        definition=revision.definition,
        rendered_task=revision.rendered_task,
        reason=revision.reason,
        actor_principal_id=revision.actor_principal_id,
        caller_principal_id=revision.caller_principal_id,
        channel=revision.channel,
        previous_revision_id=revision.previous_revision_id,
        content_hash=revision.content_hash,
        idempotency_record_id=revision.idempotency_record_id,
    )


def _task_publish_response(
    revision: TaskRevision,
    materialization: TaskRevisionMaterialization,
) -> dict[str, Any]:
    return {
        "kind": "task_publish_v1",
        "workspace_id": str(revision.workspace_id),
        "task_id": str(revision.task_id),
        "revision_id": str(revision.id),
        "revision_number": revision.revision_number,
        "previous_revision_id": (
            str(revision.previous_revision_id) if revision.previous_revision_id is not None else None
        ),
        "draft_version": revision.draft_version,
        "draft_fingerprint": revision.draft_fingerprint,
        "content_hash": revision.content_hash,
        "materialization_id": str(materialization.id),
        "materialization_state": materialization.state.value,
    }


def _task_publish_result_from_claim(
    claim: IdempotencyClaim,
    *,
    replayed: bool,
) -> TaskPublishResult:
    body = claim.response_body
    if claim.response_status != 202 or body is None or body.get("kind") != "task_publish_v1":
        raise AuthorizationUnavailable("task publish idempotency response is invalid")
    try:
        return TaskPublishResult(
            workspace_id=uuid.UUID(body["workspace_id"]),
            task_id=uuid.UUID(body["task_id"]),
            revision_id=uuid.UUID(body["revision_id"]),
            revision_number=int(body["revision_number"]),
            previous_revision_id=(
                uuid.UUID(body["previous_revision_id"])
                if body.get("previous_revision_id") is not None
                else None
            ),
            draft_version=int(body["draft_version"]),
            draft_fingerprint=body["draft_fingerprint"],
            content_hash=body["content_hash"],
            materialization_id=uuid.UUID(body["materialization_id"]),
            materialization_state=TaskMaterializationState(body["materialization_state"]),
            response_status=claim.response_status,
            replayed=replayed,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AuthorizationUnavailable("task publish idempotency response is invalid") from exc


def _task_lifecycle_state(value: TaskLifecycleState | str) -> TaskLifecycleState:
    if isinstance(value, TaskLifecycleState):
        return value
    try:
        return TaskLifecycleState(value)
    except (TypeError, ValueError) as exc:
        raise TaskLifecycleStateInvalid(value) from exc


def _task_lifecycle_response(
    task: Task,
    *,
    previous_state: TaskLifecycleState,
    target_state: TaskLifecycleState,
    reason: str,
    changed: bool,
    idempotency_record_id: uuid.UUID,
    audit_event_id: uuid.UUID,
) -> dict[str, Any]:
    return {
        "kind": "task_lifecycle_v1",
        "workspace_id": str(task.workspace_id),
        "task_id": str(task.id),
        "task_key": task.task_key,
        "previous_state": previous_state.value,
        "lifecycle_state": target_state.value,
        "reason": reason,
        "changed": changed,
        "idempotency_record_id": str(idempotency_record_id),
        "audit_event_id": str(audit_event_id),
    }


def _task_lifecycle_result_from_claim(
    claim: IdempotencyClaim,
    *,
    replayed: bool,
) -> TaskLifecycleResult:
    body = claim.response_body
    if claim.response_status != 200 or body is None or body.get("kind") != "task_lifecycle_v1":
        raise AuthorizationUnavailable("task lifecycle idempotency response is invalid")
    try:
        return TaskLifecycleResult(
            workspace_id=uuid.UUID(body["workspace_id"]),
            task_id=uuid.UUID(body["task_id"]),
            task_key=str(body["task_key"]),
            previous_state=TaskLifecycleState(body["previous_state"]),
            lifecycle_state=TaskLifecycleState(body["lifecycle_state"]),
            reason=str(body["reason"]),
            changed=bool(body["changed"]),
            idempotency_record_id=uuid.UUID(body["idempotency_record_id"]),
            audit_event_id=(
                uuid.UUID(body["audit_event_id"])
                if body.get("audit_event_id") is not None
                else None
            ),
            response_status=claim.response_status,
            replayed=replayed,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AuthorizationUnavailable("task lifecycle idempotency response is invalid") from exc


def _membership_result(
    action: MembershipMutationAction,
    workspace: Workspace,
    principal: Principal,
    *,
    binding: RoleBinding | None,
    previous_role: Role | None,
    role: Role | None,
    audit_event: AuditEvent | None = None,
) -> MembershipMutationResult:
    return MembershipMutationResult(
        action=action,
        workspace=_workspace_ref(workspace),
        principal=_principal_ref(principal),
        binding_id=binding.id if binding is not None else None,
        previous_role=previous_role,
        role=role,
        audit_event_id=audit_event.id if audit_event is not None else None,
    )


def normalize_invitation_email(value: str) -> str:
    normalized = str(value or "").strip().casefold()
    if len(normalized) > 320 or _EMAIL.fullmatch(normalized) is None:
        raise ValueError("invitation email must be a valid email address")
    return normalized


def normalize_optional_invitation_email(value: str | None) -> str | None:
    if value is None or not str(value).strip():
        return None
    try:
        return normalize_invitation_email(value)
    except ValueError:
        return None


def normalize_role(value: Role | str) -> Role:
    try:
        return value if isinstance(value, Role) else Role(str(value).strip().lower())
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid workspace role") from exc


def _invitation_email_fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _principal_uuid(value: uuid.UUID | str) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value).strip())
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("principal_id must be a valid UUID") from exc


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _workspace_invitation_ref(
    invitation: WorkspaceInvitation,
    workspace: Workspace,
) -> WorkspaceInvitationRef:
    return WorkspaceInvitationRef(
        id=invitation.id,
        workspace=_workspace_ref(workspace),
        email=invitation.email,
        role=invitation.role,
        status=invitation.status,
        expires_at=_utc_datetime(invitation.expires_at),
        claimed_principal_id=invitation.claimed_principal_id,
    )


def _workspace_invitation_payload(
    invitation: WorkspaceInvitation,
    workspace: Workspace,
    *,
    action: str,
    claim_status: str,
) -> dict[str, Any]:
    reference = _workspace_invitation_ref(invitation, workspace)
    return {
        "action": action,
        "invitation": {
            "invitation_id": str(reference.id),
            "workspace": reference.workspace.slug,
            "role": reference.role.value,
            "status": reference.status.value,
            "expires_at": reference.expires_at.isoformat(),
        },
        "claim": {"status": claim_status},
    }


def _workspace_invitation_response(
    invitation: WorkspaceInvitation,
    workspace: Workspace,
    *,
    action: str,
    claim_status: str,
) -> dict[str, Any]:
    return _workspace_invitation_payload(
        invitation,
        workspace,
        action=action,
        claim_status=claim_status,
    )


def _workspace_invitation_result_from_claim(
    session: Session,
    claim: IdempotencyClaim,
    *,
    replayed: bool,
) -> WorkspaceInvitationResult:
    body = claim.response_body
    if not isinstance(body, dict):
        raise AuthorizationUnavailable("workspace invitation idempotency response is invalid")
    invitation_payload = body.get("invitation")
    if not isinstance(invitation_payload, dict):
        raise AuthorizationUnavailable("workspace invitation idempotency response is invalid")
    try:
        invitation_id = uuid.UUID(str(invitation_payload["invitation_id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise AuthorizationUnavailable("workspace invitation idempotency response is invalid") from exc
    invitation = session.get(WorkspaceInvitation, invitation_id)
    workspace = session.get(Workspace, claim.workspace_id)
    if invitation is None or workspace is None:
        raise AuthorizationUnavailable("workspace invitation disappeared during replay")
    return WorkspaceInvitationResult(
        action=str(body.get("action") or "unchanged"),
        invitation=_workspace_invitation_ref(invitation, workspace),
        claim_status=(
            "claimed"
            if invitation.status == WorkspaceInvitationStatus.CLAIMED
            else str((body.get("claim") or {}).get("status") or invitation.status.value)
        ),
        response_status=claim.response_status or 200,
        replayed=replayed,
    )


def _membership_result_payload(result: MembershipMutationResult) -> dict[str, Any]:
    return {
        "action": result.action.value,
        "workspace": {
            "slug": result.workspace.slug,
            "name": result.workspace.name,
        },
        "principal": {
            "principal_id": str(result.principal.id),
            "display_name": result.principal.display_name,
        },
        "binding_id": str(result.binding_id) if result.binding_id is not None else None,
        "previous_role": result.previous_role.value if result.previous_role is not None else None,
        "role": result.role.value if result.role is not None else None,
        "audit_event_id": str(result.audit_event_id) if result.audit_event_id is not None else None,
    }


def _membership_mutation_result_from_claim(
    session: Session,
    claim: IdempotencyClaim,
    *,
    replayed: bool,
) -> IdempotentMembershipMutationResult:
    body = claim.response_body
    payload = body.get("membership") if isinstance(body, dict) else None
    if not isinstance(payload, dict):
        raise AuthorizationUnavailable("membership idempotency response is invalid")
    principal_payload = payload.get("principal")
    if not isinstance(principal_payload, dict):
        raise AuthorizationUnavailable("membership idempotency response is invalid")
    try:
        principal_id = uuid.UUID(str(principal_payload["principal_id"]))
        binding_id = (
            uuid.UUID(str(payload["binding_id"]))
            if payload.get("binding_id") is not None
            else None
        )
        audit_event_id = (
            uuid.UUID(str(payload["audit_event_id"]))
            if payload.get("audit_event_id") is not None
            else None
        )
        action = MembershipMutationAction(str(payload["action"]))
        previous_role = (
            Role(str(payload["previous_role"]))
            if payload.get("previous_role") is not None
            else None
        )
        role = Role(str(payload["role"])) if payload.get("role") is not None else None
    except (KeyError, TypeError, ValueError) as exc:
        raise AuthorizationUnavailable("membership idempotency response is invalid") from exc
    workspace = session.get(Workspace, claim.workspace_id)
    principal = session.get(Principal, principal_id)
    if workspace is None or principal is None:
        raise AuthorizationUnavailable("membership target disappeared during replay")
    result = MembershipMutationResult(
        action=action,
        workspace=_workspace_ref(workspace),
        principal=_principal_ref(principal),
        binding_id=binding_id,
        previous_role=previous_role,
        role=role,
        audit_event_id=audit_event_id,
    )
    return IdempotentMembershipMutationResult(
        result=result,
        response_status=claim.response_status or 200,
        replayed=replayed,
    )


def canonical_request_fingerprint(request_payload: Any) -> str:
    return hashlib.sha256(canonical_sensitive_json_bytes(request_payload)).hexdigest()


def idempotency_key_hash(idempotency_key: str) -> str:
    return hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()


def _idempotency_claim(
    record: IdempotencyRecord,
    status: IdempotencyClaimStatus,
) -> IdempotencyClaim:
    return IdempotencyClaim(
        record_id=record.id,
        workspace_id=record.workspace_id,
        actor_principal_id=record.actor_principal_id,
        caller_principal_id=record.caller_principal_id,
        operation=record.operation,
        idempotency_key_hash=record.idempotency_key_hash,
        request_fingerprint=record.request_fingerprint,
        required_permission=Permission(record.required_permission),
        resource_type=record.resource_type,
        resource_id=record.resource_id,
        channel=AuditChannel(record.channel),
        status=status,
        state=record.state,
        response_status=record.response_status if status == IdempotencyClaimStatus.REPLAY else None,
        response_body=record.response_body if status == IdempotencyClaimStatus.REPLAY else None,
    )


def _idempotency_claim_matches_record(
    claim: IdempotencyClaim,
    record: IdempotencyRecord,
) -> bool:
    try:
        record_permission = Permission(record.required_permission)
        record_channel = AuditChannel(record.channel)
    except ValueError:
        return False
    return (
        record.workspace_id == claim.workspace_id
        and record.actor_principal_id == claim.actor_principal_id
        and record.caller_principal_id == claim.caller_principal_id
        and record.operation == claim.operation
        and record.idempotency_key_hash == claim.idempotency_key_hash
        and record.request_fingerprint == claim.request_fingerprint
        and record_permission == claim.required_permission
        and record.resource_type == claim.resource_type
        and record.resource_id == claim.resource_id
        and record_channel == claim.channel
    )
