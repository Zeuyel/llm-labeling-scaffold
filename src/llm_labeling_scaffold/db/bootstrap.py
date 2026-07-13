from __future__ import annotations

import hashlib
import uuid
from dataclasses import asdict, dataclass

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .audit import append_audit_event
from .enums import AuditChannel, PrincipalType, Role
from .models import Principal, RoleBinding, Workspace


class BootstrapConflictError(RuntimeError):
    pass


@dataclass(frozen=True)
class BootstrapResult:
    principal_id: uuid.UUID
    workspace_id: uuid.UUID
    role_binding_id: uuid.UUID
    principal_created: bool
    workspace_created: bool
    role_binding_created: bool
    changed: bool

    def to_dict(self) -> dict[str, str | bool]:
        return {
            key: str(value) if isinstance(value, uuid.UUID) else value
            for key, value in asdict(self).items()
        }


def bootstrap_admin(
    session: Session,
    *,
    issuer: str,
    subject: str,
    workspace_slug: str,
    workspace_name: str,
    principal_type: PrincipalType = PrincipalType.USER,
    display_name: str | None = None,
    email: str | None = None,
) -> BootstrapResult:
    for name, value in {
        "issuer": issuer,
        "subject": subject,
        "workspace_slug": workspace_slug,
        "workspace_name": workspace_name,
    }.items():
        if not value.strip():
            raise ValueError(f"{name} must not be blank")

    principal_created = False
    workspace_created = False
    role_binding_created = False
    changed_fields: list[str] = []

    with session.begin():
        _acquire_bootstrap_lock(session, issuer, subject, workspace_slug)
        principal, principal_created = _get_or_create_principal(
            session,
            issuer=issuer,
            subject=subject,
            principal_type=principal_type,
            display_name=display_name,
            email=email,
        )
        if principal.principal_type != principal_type:
            raise BootstrapConflictError(
                f"principal {issuer!r}/{subject!r} already exists as {principal.principal_type.value}",
            )
        if principal_created:
            changed_fields.append("principal")
        else:
            if display_name is not None and principal.display_name != display_name:
                principal.display_name = display_name
                changed_fields.append("display_name")
            if email is not None and principal.email_snapshot != email:
                principal.email_snapshot = email
                changed_fields.append("email_snapshot")
            if not principal.is_active:
                principal.is_active = True
                changed_fields.append("principal_active")

        workspace, workspace_created = _get_or_create_workspace(
            session,
            slug=workspace_slug,
            name=workspace_name,
        )
        if workspace.name != workspace_name:
            raise BootstrapConflictError(
                f"workspace slug {workspace_slug!r} already belongs to {workspace.name!r}",
            )
        if workspace_created:
            changed_fields.append("workspace")
        elif not workspace.is_active:
            workspace.is_active = True
            changed_fields.append("workspace_active")

        binding, role_binding_created = _get_or_create_admin_binding(
            session,
            workspace_id=workspace.id,
            principal_id=principal.id,
        )
        if role_binding_created:
            changed_fields.append("admin_role")
        elif binding.role != Role.ADMIN:
            binding.role = Role.ADMIN
            changed_fields.append("admin_role")

        if changed_fields:
            append_audit_event(
                session,
                workspace_id=workspace.id,
                event_type="workspace.admin_bootstrapped",
                actor=None,
                caller=None,
                channel=AuditChannel.CLI,
                resource_type="workspace",
                resource_id=workspace.id,
                details={
                    "changes": changed_fields,
                    "granted_principal_id": str(principal.id),
                    "granted_role": Role.ADMIN.value,
                    "workspace_id": str(workspace.id),
                },
            )

    return BootstrapResult(
        principal_id=principal.id,
        workspace_id=workspace.id,
        role_binding_id=binding.id,
        principal_created=principal_created,
        workspace_created=workspace_created,
        role_binding_created=role_binding_created,
        changed=bool(changed_fields),
    )


def _acquire_bootstrap_lock(session: Session, issuer: str, subject: str, workspace_slug: str) -> None:
    if session.get_bind().dialect.name != "postgresql":
        return
    digest = hashlib.blake2b(
        f"{issuer}\0{subject}\0{workspace_slug}".encode("utf-8"),
        digest_size=8,
    ).digest()
    lock_key = int.from_bytes(digest, byteorder="big", signed=True)
    session.scalar(select(func.pg_advisory_xact_lock(lock_key)))


def _get_or_create_principal(
    session: Session,
    *,
    issuer: str,
    subject: str,
    principal_type: PrincipalType,
    display_name: str | None,
    email: str | None,
) -> tuple[Principal, bool]:
    principal = session.scalar(
        select(Principal).where(Principal.issuer == issuer, Principal.subject == subject),
    )
    if principal is not None:
        return principal, False
    try:
        with session.begin_nested():
            principal = Principal(
                issuer=issuer,
                subject=subject,
                principal_type=principal_type,
                display_name=display_name,
                email_snapshot=email,
            )
            session.add(principal)
            session.flush()
        return principal, True
    except IntegrityError:
        principal = session.scalar(
            select(Principal).where(Principal.issuer == issuer, Principal.subject == subject),
        )
        if principal is None:
            raise
        return principal, False


def _get_or_create_workspace(session: Session, *, slug: str, name: str) -> tuple[Workspace, bool]:
    workspace = session.scalar(select(Workspace).where(Workspace.slug == slug))
    if workspace is not None:
        return workspace, False
    try:
        with session.begin_nested():
            workspace = Workspace(slug=slug, name=name)
            session.add(workspace)
            session.flush()
        return workspace, True
    except IntegrityError:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == slug))
        if workspace is None:
            raise
        return workspace, False


def _get_or_create_admin_binding(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    principal_id: uuid.UUID,
) -> tuple[RoleBinding, bool]:
    binding = session.scalar(
        select(RoleBinding).where(
            RoleBinding.workspace_id == workspace_id,
            RoleBinding.principal_id == principal_id,
            RoleBinding.task_id.is_(None),
        ),
    )
    if binding is not None:
        return binding, False
    try:
        with session.begin_nested():
            binding = RoleBinding(
                workspace_id=workspace_id,
                principal_id=principal_id,
                role=Role.ADMIN,
                created_by_principal_id=principal_id,
            )
            session.add(binding)
            session.flush()
        return binding, True
    except IntegrityError:
        binding = session.scalar(
            select(RoleBinding).where(
                RoleBinding.workspace_id == workspace_id,
                RoleBinding.principal_id == principal_id,
                RoleBinding.task_id.is_(None),
            ),
        )
        if binding is None:
            raise
        return binding, False
