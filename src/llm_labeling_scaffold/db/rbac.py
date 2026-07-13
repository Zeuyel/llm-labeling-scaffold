from __future__ import annotations

from enum import Enum

from .enums import Role


class Permission(str, Enum):
    TASK_CREATE = "task:create"
    TASK_READ = "task:read"
    TASK_EDIT = "task:edit"
    TASK_PUBLISH = "task:publish"
    IMPORT_SUBMIT = "import:submit"
    ANNOTATION_WORK = "annotation:work"
    ANNOTATION_REVIEW = "annotation:review"
    AUDIT_VIEW = "audit:view"
    WORKSPACE_MANAGE = "workspace:manage"


TASK_PERMISSIONS = frozenset(
    {
        Permission.TASK_READ,
        Permission.TASK_EDIT,
        Permission.TASK_PUBLISH,
        Permission.IMPORT_SUBMIT,
        Permission.ANNOTATION_WORK,
        Permission.ANNOTATION_REVIEW,
    }
)

WORKSPACE_PERMISSIONS = frozenset(
    {
        Permission.TASK_CREATE,
        Permission.AUDIT_VIEW,
        Permission.WORKSPACE_MANAGE,
    }
)


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
            Permission.TASK_CREATE,
            Permission.AUDIT_VIEW,
        }
    ),
    Role.ADMIN: frozenset(Permission),
}


def role_allows(role: Role, permission: Permission) -> bool:
    return permission in ROLE_PERMISSIONS[role]
