from __future__ import annotations

import hashlib
import json
import unicodedata
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Any

from sqlalchemy import Engine, and_, or_, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from .audit import append_audit_event
from .database import create_database_engine, create_session_factory
from .enums import AuditChannel, IdempotencyState, PrincipalType, Role
from .models import AuditEvent, IdempotencyRecord, Principal, RoleBinding, Task, Workspace
from .rbac import TASK_PERMISSIONS, WORKSPACE_PERMISSIONS, Permission, role_allows


MAX_AUTHORIZED_TASKS_PAGE_SIZE = 100


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
    status: IdempotencyClaimStatus
    state: IdempotencyState
    response_status: int | None
    response_body: dict[str, Any] | None


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

    def list_authorized_workspaces(self, identity: ExternalIdentity) -> tuple[WorkspaceAccess, ...]:
        return self.get_session(identity).workspaces

    def list_authorized_tasks(
        self,
        identity: ExternalIdentity,
        workspace_slug: str,
        *,
        limit: int,
        after_task_key: str | None = None,
    ) -> TaskAccessPage:
        with self.transaction() as transaction:
            return transaction.list_authorized_tasks(
                identity,
                workspace_slug,
                limit=limit,
                after_task_key=after_task_key,
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
                task_key=task_key,
            )

    def complete_idempotency(
        self,
        claim: IdempotencyClaim,
        *,
        response_status: int,
        response_body: dict[str, Any] | None,
        succeeded: bool = True,
    ) -> IdempotencyClaim:
        with self.transaction() as transaction:
            return transaction.complete_idempotency(
                claim,
                response_status=response_status,
                response_body=response_body,
                succeeded=succeeded,
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


class DatabaseTransaction:
    def __init__(self, session: Session):
        self._session = session

    def resolve_identity(self, identity: ExternalIdentity) -> PrincipalRef | None:
        principal = self._find_principal(identity)
        return _principal_ref(principal) if principal is not None else None

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
        principal = self._find_principal(identity)
        if principal is None:
            return AuthorizationSession(principal=None, workspaces=())
        principal_ref = _principal_ref(principal)
        if not principal.is_active:
            return AuthorizationSession(principal=principal_ref, workspaces=())
        return AuthorizationSession(
            principal=principal_ref,
            workspaces=self._list_authorized_workspaces(principal.id),
        )

    def list_authorized_tasks(
        self,
        identity: ExternalIdentity,
        workspace_slug: str,
        *,
        limit: int,
        after_task_key: str | None = None,
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

        items: list[TaskAccess] = []
        for task in page_tasks:
            roles = _sorted_roles((*workspace_roles, *task_roles.get(task.id, set())))
            items.append(
                TaskAccess(
                    task=_task_ref(task),
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
            .where(Workspace.slug == workspace_slug),
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
                    or_(RoleBinding.task_id.is_(None), RoleBinding.task_id == Task.id),
                ),
            )
            .where(Workspace.slug == workspace_slug, Task.task_key == task_key)
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
        task_key: str | None = None,
    ) -> IdempotencyClaim:
        if not operation.strip() or not idempotency_key.strip():
            raise ValueError("operation and idempotency_key must not be blank")
        if task_key is None:
            decision = self.authorize_workspace(actor_identity, workspace_slug, required_permission)
        else:
            decision = self.authorize_task(actor_identity, workspace_slug, task_key, required_permission)
        self.require(decision)
        caller = self._require_caller(caller_identity, decision.principal.id, required_permission)

        request_fingerprint = canonical_request_fingerprint(request_payload)
        key_hash = idempotency_key_hash(idempotency_key)
        candidate_id = uuid.uuid4()
        values = {
            "id": candidate_id,
            "workspace_id": decision.workspace.id,
            "actor_principal_id": decision.principal.id,
            "caller_principal_id": caller.id,
            "operation": operation,
            "idempotency_key_hash": key_hash,
            "request_fingerprint": request_fingerprint,
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
        record = self._session.scalar(
            select(IdempotencyRecord)
            .where(
                IdempotencyRecord.workspace_id == decision.workspace.id,
                IdempotencyRecord.operation == operation,
                IdempotencyRecord.idempotency_key_hash == key_hash,
            )
            .with_for_update(),
        )
        if record is None:
            raise AuthorizationUnavailable("idempotency claim was not readable after insert")
        if (
            record.actor_principal_id != decision.principal.id
            or record.caller_principal_id != caller.id
            or record.request_fingerprint != request_fingerprint
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
        response_status: int,
        response_body: dict[str, Any] | None,
        succeeded: bool = True,
    ) -> IdempotencyClaim:
        record = self._session.scalar(
            select(IdempotencyRecord).where(IdempotencyRecord.id == claim.record_id).with_for_update(),
        )
        if record is None or (
            record.workspace_id != claim.workspace_id
            or record.actor_principal_id != claim.actor_principal_id
            or record.caller_principal_id != claim.caller_principal_id
            or record.operation != claim.operation
            or record.idempotency_key_hash != claim.idempotency_key_hash
            or record.request_fingerprint != claim.request_fingerprint
        ):
            raise IdempotencyConflict()
        if record.state == IdempotencyState.PENDING:
            record.state = IdempotencyState.SUCCEEDED if succeeded else IdempotencyState.FAILED
            record.response_status = response_status
            record.response_body = response_body
            self._session.flush()
        elif record.response_status != response_status or record.response_body != response_body:
            raise IdempotencyConflict()
        return _idempotency_claim(record, IdempotencyClaimStatus.REPLAY)

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

    def _find_principal(self, identity: ExternalIdentity | None) -> Principal | None:
        if identity is None:
            return None
        return self._session.scalar(
            select(Principal).where(
                Principal.issuer == identity.issuer,
                Principal.subject == identity.subject,
            ),
        )

    def _require_caller(
        self,
        identity: ExternalIdentity,
        actor_principal_id: uuid.UUID,
        permission: Permission,
    ) -> Principal:
        caller = self._find_principal(identity)
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
            ),
        )
        return _sorted_roles(values)

    def _list_authorized_workspaces(self, principal_id: uuid.UUID) -> tuple[WorkspaceAccess, ...]:
        workspaces = self._session.scalars(
            select(Workspace)
            .join(RoleBinding, RoleBinding.workspace_id == Workspace.id)
            .where(RoleBinding.principal_id == principal_id, Workspace.is_active.is_(True))
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


def _task_ref(task: Task) -> TaskRef:
    return TaskRef(id=task.id, workspace_id=task.workspace_id, task_key=task.task_key)


def _audit_event_ref(event: AuditEvent) -> AuditEventRef:
    return AuditEventRef(
        id=event.id,
        workspace_id=event.workspace_id,
        actor_principal_id=event.actor_principal_id,
        caller_principal_id=event.caller_principal_id,
        channel=event.channel,
        event_type=event.event_type,
    )


def canonical_request_fingerprint(request_payload: Any) -> str:
    try:
        canonical_json = json.dumps(
            request_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("request_payload must be canonical JSON data") from exc
    normalized = unicodedata.normalize("NFC", canonical_json)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


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
        status=status,
        state=record.state,
        response_status=record.response_status if status == IdempotencyClaimStatus.REPLAY else None,
        response_body=record.response_body if status == IdempotencyClaimStatus.REPLAY else None,
    )
