from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..pipeline import build_task_raw_from_spec
from .audit import append_audit_event
from .enums import AuditChannel, Role
from .models import Principal, Task, TaskDraft, Workspace
from .service import ExternalIdentity, task_definition_fingerprint


class LegacyImportError(RuntimeError):
    pass


def import_legacy_registry(
    session: Session,
    *,
    registry_path: str | Path,
    workspace_slug: str,
    actor_identity: ExternalIdentity,
    caller_identity: ExternalIdentity,
    confirm: bool,
) -> dict[str, Any]:
    if not confirm:
        raise LegacyImportError("legacy registry 导入必须显式设置 confirm=true")
    path = Path(registry_path).expanduser()
    if not path.is_file():
        raise LegacyImportError(f"legacy registry 不存在: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LegacyImportError(f"legacy registry 读取失败: {path}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("tasks"), dict):
        raise LegacyImportError("legacy registry 必须包含对象 tasks")

    workspace = session.scalar(select(Workspace).where(Workspace.slug == workspace_slug))
    actor = session.scalar(
        select(Principal).where(
            Principal.issuer == actor_identity.issuer,
            Principal.subject == actor_identity.subject,
        ),
    )
    caller = session.scalar(
        select(Principal).where(
            Principal.issuer == caller_identity.issuer,
            Principal.subject == caller_identity.subject,
        ),
    )
    if workspace is None or not workspace.is_active:
        raise LegacyImportError(f"workspace 不存在或未启用: {workspace_slug}")
    if actor is None or not actor.is_active or caller is None or not caller.is_active:
        raise LegacyImportError("legacy registry 导入要求 active actor/caller principal")
    from .models import RoleBinding

    binding = session.scalar(
        select(RoleBinding).where(
            RoleBinding.workspace_id == workspace.id,
            RoleBinding.principal_id == actor.id,
            RoleBinding.task_id.is_(None),
            RoleBinding.role == Role.ADMIN,
        ),
    )
    if binding is None:
        raise LegacyImportError("legacy registry 导入要求 actor 是 workspace admin")

    source_bytes = path.read_bytes()
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    imported: list[str] = []
    skipped: list[str] = []
    conflicts: list[str] = []
    for task_key, record in sorted(raw["tasks"].items()):
        if not isinstance(record, dict) or not isinstance(record.get("draft_spec"), dict):
            raise LegacyImportError(f"legacy task 缺少 draft_spec，拒绝导入: {task_key}")
        spec = dict(record["draft_spec"])
        spec["task_id"] = str(task_key)
        try:
            definition = build_task_raw_from_spec(spec)
        except Exception as exc:
            raise LegacyImportError(f"legacy task 定义无效: {task_key}") from exc
        rendered_task = yaml.safe_dump(definition, allow_unicode=True, sort_keys=False)
        fingerprint = task_definition_fingerprint(spec, rendered_task)
        task = session.scalar(select(Task).where(Task.task_key == str(task_key)))
        if task is not None:
            if task.workspace_id != workspace.id:
                conflicts.append(str(task_key))
                continue
            draft = session.scalar(select(TaskDraft).where(TaskDraft.task_id == task.id))
            if draft is not None and draft.fingerprint == fingerprint:
                skipped.append(str(task_key))
                continue
            conflicts.append(str(task_key))
            continue

        task = Task(
            workspace_id=workspace.id,
            task_key=str(task_key),
            name=str(spec.get("name") or task_key),
            description=str(spec.get("description") or "legacy registry import"),
            created_by_principal_id=actor.id,
        )
        session.add(task)
        session.flush()
        draft = TaskDraft(
            workspace_id=workspace.id,
            task_id=task.id,
            version=1,
            fingerprint=fingerprint,
            definition=spec,
            rendered_task=rendered_task,
        )
        session.add(draft)
        session.flush()
        append_audit_event(
            session,
            workspace_id=workspace.id,
            event_type="task.legacy_imported",
            actor=actor,
            caller=caller,
            channel=AuditChannel.CLI,
            resource_type="task",
            resource_id=task.id,
            details={
                "task_key": str(task_key),
                "source_path": str(path),
                "source_sha256": source_sha256,
                "legacy_status": record.get("status"),
                "legacy_revision": record.get("revision"),
                "imported_as": "draft_only",
            },
        )
        imported.append(str(task_key))

    if conflicts:
        raise LegacyImportError(
            "legacy registry 与现有 DB task 冲突，未提交本次导入: " + ", ".join(conflicts),
        )
    session.flush()
    return {
        "source_path": str(path),
        "source_sha256": source_sha256,
        "workspace": workspace_slug,
        "imported": imported,
        "skipped": skipped,
        "published_revision_required": True,
    }
