from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Any

from sqlalchemy import Engine, and_, or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from .audit import append_audit_event
from .database import create_database_engine, create_session_factory
from .enums import AuditChannel, PrincipalType, Role
from .models import AuditEvent, Principal, RoleBinding, Task, Workspace
from .rbac import Permission, role_allows


class AuthorizationReason(str, Enum):
    ALLOWED = "allowed"
    UNKNOWN_PRINCIPAL = "unknown_principal"
    INACTIVE_PRINCIPAL = "inactive_principal"
    RESOURCE_NOT_VISIBLE = "resource_not_visible"
    INACTIVE_WORKSPACE = "inactive_workspace"
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
    capabilities: tuple[Permission, ...]
    tasks: tuple[TaskAccess, ...]


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


class AuthorizationUnavailable(RuntimeError):
    pass


class AuthorizationDenied(RuntimeError):
    def __init__(self, decision: AuthorizationDecision):
        self.decision = decision
        super().__init__(f"authorization denied: {decision.reason.value}")


class IdentityTypeConflict(RuntimeError):
    pass


class AuditActorNotFound(RuntimeError):
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

    def append_audit(
        self,
        *,
        workspace_slug: str,
        event_type: str,
        actor_identity: ExternalIdentity | None,
        channel: AuditChannel,
        caller_identity: ExternalIdentity | None = None,
        resource_type: str | None = None,
        resource_id: str | uuid.UUID | None = None,
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
                resource_type=resource_type,
                resource_id=resource_id,
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

    def authorize_workspace(
        self,
        identity: ExternalIdentity,
        workspace_slug: str,
        permission: Permission,
    ) -> AuthorizationDecision:
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

    def append_audit(
        self,
        *,
        workspace_slug: str,
        event_type: str,
        actor_identity: ExternalIdentity | None,
        channel: AuditChannel,
        caller_identity: ExternalIdentity | None = None,
        resource_type: str | None = None,
        resource_id: str | uuid.UUID | None = None,
        request_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditEventRef:
        workspace = self._session.scalar(select(Workspace).where(Workspace.slug == workspace_slug))
        if workspace is None:
            raise AuditActorNotFound("audit target or actor is unavailable")

        actor = self._find_principal(actor_identity) if actor_identity is not None else None
        if actor_identity is not None and actor is None:
            raise AuditActorNotFound("audit target or actor is unavailable")

        effective_caller_identity = caller_identity if caller_identity is not None else actor_identity
        caller = self._find_principal(effective_caller_identity) if effective_caller_identity is not None else None
        if effective_caller_identity is not None and caller is None:
            raise AuditActorNotFound("audit target or actor is unavailable")

        event = append_audit_event(
            self._session,
            workspace_id=workspace.id,
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
        rows = self._session.execute(
            select(RoleBinding, Workspace)
            .join(Workspace, Workspace.id == RoleBinding.workspace_id)
            .where(RoleBinding.principal_id == principal_id, Workspace.is_active.is_(True)),
        ).all()
        grouped: dict[uuid.UUID, dict[str, Any]] = {}
        for binding, workspace in rows:
            entry = grouped.setdefault(
                workspace.id,
                {
                    "workspace": workspace,
                    "workspace_roles": set(),
                    "task_roles": {},
                },
            )
            if binding.task_id is None:
                entry["workspace_roles"].add(binding.role)
            else:
                entry["task_roles"].setdefault(binding.task_id, set()).add(binding.role)

        access: list[WorkspaceAccess] = []
        for entry in grouped.values():
            workspace = entry["workspace"]
            workspace_roles = _sorted_roles(entry["workspace_roles"])
            task_roles: dict[uuid.UUID, set[Role]] = entry["task_roles"]
            task_query = select(Task).where(Task.workspace_id == workspace.id)
            if not workspace_roles:
                task_query = task_query.where(Task.id.in_(tuple(task_roles)))
            tasks = self._session.scalars(task_query.order_by(Task.task_key)).all()
            task_access = []
            for task in tasks:
                roles = _sorted_roles((*workspace_roles, *task_roles.get(task.id, set())))
                task_access.append(
                    TaskAccess(
                        task=_task_ref(task),
                        roles=roles,
                        capabilities=_capabilities(roles),
                    )
                )
            access.append(
                WorkspaceAccess(
                    workspace=_workspace_ref(workspace),
                    roles=workspace_roles,
                    capabilities=_capabilities(workspace_roles),
                    tasks=tuple(task_access),
                )
            )
        return tuple(sorted(access, key=lambda item: item.workspace.slug))


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


def _capabilities(roles: tuple[Role, ...]) -> tuple[Permission, ...]:
    return tuple(permission for permission in Permission if any(role_allows(role, permission) for role in roles))


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
