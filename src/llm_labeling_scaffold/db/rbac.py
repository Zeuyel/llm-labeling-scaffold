from __future__ import annotations

import uuid
from enum import Enum

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .enums import Role
from .models import RoleBinding, Task


class Permission(str, Enum):
    TASK_READ = "task:read"
    TASK_EDIT = "task:edit"
    TASK_PUBLISH = "task:publish"
    IMPORT_SUBMIT = "import:submit"
    ANNOTATION_WORK = "annotation:work"
    ANNOTATION_REVIEW = "annotation:review"
    AUDIT_VIEW = "audit:view"
    WORKSPACE_MANAGE = "workspace:manage"


ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.VIEWER: frozenset({Permission.TASK_READ}),
    Role.ANNOTATOR: frozenset({Permission.TASK_READ, Permission.ANNOTATION_WORK}),
    Role.EXPERIMENTER: frozenset(
        {
            Permission.TASK_READ,
            Permission.TASK_EDIT,
            Permission.TASK_PUBLISH,
            Permission.IMPORT_SUBMIT,
            Permission.ANNOTATION_WORK,
            Permission.ANNOTATION_REVIEW,
            Permission.AUDIT_VIEW,
        }
    ),
    Role.ADMIN: frozenset(Permission),
}


class PermissionDenied(RuntimeError):
    pass


def role_allows(role: Role, permission: Permission) -> bool:
    return permission in ROLE_PERMISSIONS[role]


def effective_roles(
    session: Session,
    principal_id: uuid.UUID,
    workspace_id: uuid.UUID,
    task_id: uuid.UUID | None = None,
) -> frozenset[Role]:
    if task_id is not None:
        task_exists = session.scalar(
            select(Task.id).where(Task.id == task_id, Task.workspace_id == workspace_id),
        )
        if task_exists is None:
            return frozenset()

    scope = RoleBinding.task_id.is_(None)
    if task_id is not None:
        scope = or_(scope, RoleBinding.task_id == task_id)
    roles = session.scalars(
        select(RoleBinding.role).where(
            RoleBinding.principal_id == principal_id,
            RoleBinding.workspace_id == workspace_id,
            scope,
        ),
    )
    return frozenset(roles)


def has_permission(
    session: Session,
    principal_id: uuid.UUID,
    workspace_id: uuid.UUID,
    permission: Permission,
    task_id: uuid.UUID | None = None,
) -> bool:
    return any(role_allows(role, permission) for role in effective_roles(session, principal_id, workspace_id, task_id))


def require_permission(
    session: Session,
    principal_id: uuid.UUID,
    workspace_id: uuid.UUID,
    permission: Permission,
    task_id: uuid.UUID | None = None,
) -> None:
    if not has_permission(session, principal_id, workspace_id, permission, task_id):
        raise PermissionDenied(
            f"principal {principal_id} lacks {permission.value} in workspace {workspace_id}",
        )
