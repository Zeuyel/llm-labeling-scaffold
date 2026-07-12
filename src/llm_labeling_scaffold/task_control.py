from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import shutil
import threading
from typing import Any

import yaml

from .config import load_task
from .io import read_json, write_json, write_text_atomic
from .pipeline import build_task_raw_from_spec


REGISTRY_SCHEMA_VERSION = 1
_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_segment(value: str) -> bool:
    return bool(value) and ".." not in value and "/" not in value and "\\" not in value


def control_root(runs_root: str | Path) -> Path:
    return Path(runs_root) / "_system" / "task_control"


def registry_path(runs_root: str | Path) -> Path:
    return control_root(runs_root) / "registry.json"


def _empty_registry() -> dict[str, Any]:
    return {"schema_version": REGISTRY_SCHEMA_VERSION, "tasks": {}}


def _read_registry(runs_root: str | Path) -> dict[str, Any]:
    path = registry_path(runs_root)
    if not path.exists():
        return _empty_registry()
    data = read_json(path)
    if not isinstance(data, dict):
        raise ValueError(f"任务控制面 registry 必须是 JSON 对象: {path}")
    tasks = data.setdefault("tasks", {})
    if not isinstance(tasks, dict):
        raise ValueError(f"任务控制面 registry.tasks 必须是 JSON 对象: {path}")
    data.setdefault("schema_version", REGISTRY_SCHEMA_VERSION)
    return data


def _write_registry(runs_root: str | Path, registry: dict[str, Any]) -> None:
    registry["schema_version"] = REGISTRY_SCHEMA_VERSION
    write_json(registry, registry_path(runs_root), indent=2)


def _task_path(tasks_root: str | Path, task_id: str) -> Path:
    return Path(tasks_root) / task_id / "task.yaml"


def _snapshot_dir(runs_root: str | Path, task_id: str, revision: int) -> Path:
    return control_root(runs_root) / "task_snapshots" / task_id / f"revision_{revision:06d}"


def _validate_spec(
    spec: dict[str, Any],
    *,
    task_id: str | None = None,
    revision: int | None = None,
    require_data_lake: bool = False,
) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise ValueError("任务单必须是 JSON 对象")
    raw = build_task_raw_from_spec(spec, revision=revision)
    if task_id is not None and raw["task_id"] != task_id:
        raise ValueError(f"任务编号不能变更: {task_id} -> {raw['task_id']}")
    if require_data_lake:
        data_lake = raw.get("data_lake") if isinstance(raw.get("data_lake"), dict) else {}
        required = ("source_dataset_id", "source_object_path", "default_import_id", "output_base_uri")
        missing = [field for field in required if not str(data_lake.get(field) or "").strip()]
        if missing:
            raise ValueError(f"发布任务前必须配置数据湖字段: {', '.join(missing)}")
    return raw


def _task_summary_from_record(record: dict[str, Any]) -> dict[str, Any]:
    draft_spec = record.get("draft_spec") if isinstance(record.get("draft_spec"), dict) else {}
    published = record.get("published") if isinstance(record.get("published"), dict) else {}
    out = {
        "task_id": record.get("task_id"),
        "status": record.get("status", "draft"),
        "source": "scaffold 控制面",
        "source_type": "control",
        "deletable": False,
        "editable": True,
        "revision": record.get("revision", 0),
        "updated_at": record.get("updated_at"),
        "published_at": record.get("published_at"),
        "path": published.get("path"),
        "id_field": draft_spec.get("id_field", "record_id"),
        "profile": draft_spec.get("profile", "manual_labeling_cv_v1"),
    }
    primary = draft_spec.get("primary_label_name")
    if primary:
        out["primary_label"] = {
            "name": primary,
            "title": draft_spec.get("primary_label_title", ""),
            "type": draft_spec.get("primary_label_type", "categorical"),
            "values": draft_spec.get("primary_label_values", []),
        }
    return out


def list_control_tasks(runs_root: str | Path) -> list[dict[str, Any]]:
    registry = _read_registry(runs_root)
    return [
        _task_summary_from_record(record)
        for record in sorted(registry["tasks"].values(), key=lambda item: str(item.get("task_id") or ""))
    ]


def get_task_record(runs_root: str | Path, task_id: str) -> dict[str, Any]:
    if not _safe_segment(task_id):
        raise ValueError("非法任务编号")
    registry = _read_registry(runs_root)
    record = registry["tasks"].get(task_id)
    if not record:
        raise ValueError(f"任务单不存在: {task_id}")
    return dict(record)


def create_draft(runs_root: str | Path, tasks_root: str | Path, spec: dict[str, Any], *, actor: str = "panel") -> dict[str, Any]:
    raw = _validate_spec(spec)
    task_id = str(raw["task_id"])
    with _LOCK:
        registry = _read_registry(runs_root)
        if task_id in registry["tasks"]:
            raise ValueError(f"任务单已存在: {task_id}")
        if _task_path(tasks_root, task_id).exists():
            raise ValueError(f"任务配置已存在，不能创建同名控制面任务: {task_id}")
        now = _now()
        record = {
            "task_id": task_id,
            "status": "draft",
            "revision": 0,
            "draft_spec": dict(spec),
            "created_at": now,
            "updated_at": now,
            "created_by": actor,
            "updated_by": actor,
            "revisions": [],
        }
        registry["tasks"][task_id] = record
        _write_registry(runs_root, registry)
        return {"action": "created", "task": _task_summary_from_record(record), "record": record}


def update_draft(runs_root: str | Path, task_id: str, spec: dict[str, Any], *, actor: str = "panel") -> dict[str, Any]:
    _validate_spec(spec, task_id=task_id)
    with _LOCK:
        registry = _read_registry(runs_root)
        record = registry["tasks"].get(task_id)
        if not record:
            raise ValueError(f"任务单不存在: {task_id}")
        record["draft_spec"] = dict(spec)
        record["status"] = "draft" if int(record.get("revision") or 0) <= 0 else "published_with_draft"
        record["updated_at"] = _now()
        record["updated_by"] = actor
        registry["tasks"][task_id] = record
        _write_registry(runs_root, registry)
        return {"action": "updated", "task": _task_summary_from_record(record), "record": record}


def _write_published_task(
    runs_root: str | Path,
    tasks_root: str | Path,
    record: dict[str, Any],
    raw: dict[str, Any],
    *,
    revision: int,
    actor: str,
) -> dict[str, Any]:
    task_id = str(raw["task_id"])
    task_dir = Path(tasks_root) / task_id
    task_path = task_dir / "task.yaml"
    prompt = str(record.get("draft_spec", {}).get("prompt") or "").strip()

    snapshot_dir = _snapshot_dir(runs_root, task_id, revision)
    snapshot_task_path = snapshot_dir / "task.yaml"
    if snapshot_dir.exists():
        recorded_revisions = {
            int(item.get("revision") or 0)
            for item in record.get("revisions", [])
            if isinstance(item, dict)
        }
        if revision in recorded_revisions or not snapshot_dir.is_dir():
            raise ValueError(f"任务 revision 快照已存在，拒绝覆盖: {snapshot_dir}")
        shutil.rmtree(snapshot_dir)
    snapshot_raw = dict(raw)
    live_raw = dict(raw)
    live_prompt_path: Path | None = None
    if prompt:
        snapshot_raw["prompt"] = "prompt.md"
        live_prompt_path = task_dir / f"prompt.revision_{revision:06d}.md"
        live_raw["prompt"] = live_prompt_path.name

    snapshot_dir.mkdir(parents=True, exist_ok=False)
    write_text_atomic(yaml.safe_dump(snapshot_raw, allow_unicode=True, sort_keys=False), snapshot_task_path)
    if prompt:
        write_text_atomic(prompt + "\n", snapshot_dir / "prompt.md")
    write_json(record.get("draft_spec", {}), snapshot_dir / "draft_spec.json", indent=2)

    task_dir.mkdir(parents=True, exist_ok=True)
    if live_prompt_path is not None:
        write_text_atomic(prompt + "\n", live_prompt_path)
    write_text_atomic(yaml.safe_dump(live_raw, allow_unicode=True, sort_keys=False), task_path)

    loaded = load_task(task_path)
    return {
        "revision": revision,
        "path": str(task_path),
        "snapshot_path": str(snapshot_task_path),
        "published_at": _now(),
        "published_by": actor,
        "task_id": loaded.task_id,
        "profile": loaded.profile,
        "id_field": loaded.id_field,
        "primary_label": loaded.primary_label,
    }


def publish_task(runs_root: str | Path, tasks_root: str | Path, task_id: str, *, actor: str = "panel") -> dict[str, Any]:
    if not _safe_segment(task_id):
        raise ValueError("非法任务编号")
    with _LOCK:
        registry = _read_registry(runs_root)
        record = registry["tasks"].get(task_id)
        if not record:
            raise ValueError(f"任务单不存在: {task_id}")
        revision = int(record.get("revision") or 0) + 1
        raw = _validate_spec(
            record.get("draft_spec") or {},
            task_id=task_id,
            revision=revision,
            require_data_lake=True,
        )
        published = _write_published_task(runs_root, tasks_root, record, raw, revision=revision, actor=actor)
        record["revision"] = revision
        record["status"] = "published"
        record["published"] = published
        record["published_at"] = published["published_at"]
        record["updated_at"] = published["published_at"]
        record["published_by"] = actor
        record.setdefault("revisions", []).append(published)
        registry["tasks"][task_id] = record
        _write_registry(runs_root, registry)
        return {"action": "published", "task": _task_summary_from_record(record), "record": record, "published": published}
