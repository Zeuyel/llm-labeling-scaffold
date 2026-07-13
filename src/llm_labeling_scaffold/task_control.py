from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import threading
from typing import Any

import yaml

from .config import load_task
from .io import read_json, write_json, write_text_atomic
from .pipeline import build_task_raw_from_spec


REGISTRY_SCHEMA_VERSION = 2
_LOCK = threading.Lock()


class TaskConflictError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_segment(value: str) -> bool:
    return bool(value) and ".." not in value and "/" not in value and "\\" not in value


def _idempotency_key_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validate_idempotency_key(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("发布任务必须提供 idempotency_key")
    if len(text) > 200 or any(ord(char) < 32 for char in text):
        raise ValueError("idempotency_key 必须是长度不超过 200 的可打印字符串")
    return text


def _validate_draft_fingerprint(value: str | None) -> str:
    text = str(value or "").strip().lower()
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ValueError("必须提供有效的 expected_draft_fingerprint")
    return text


def _draft_fingerprint(record: dict[str, Any]) -> str:
    try:
        encoded = json.dumps(record.get("draft_spec") or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("任务草稿无法生成幂等指纹") from exc
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _record_with_fingerprint(record: dict[str, Any]) -> dict[str, Any]:
    out = dict(record)
    out["draft_fingerprint"] = _draft_fingerprint(record)
    return out


def _canonical_draft_spec(spec: dict[str, Any], task_id: str) -> dict[str, Any]:
    out = dict(spec)
    out["task_id"] = task_id
    return out


def _published_revision(record: dict[str, Any], revision: int) -> dict[str, Any] | None:
    for item in record.get("revisions", []):
        if isinstance(item, dict) and int(item.get("revision") or 0) == revision:
            return dict(item)
    return None


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
        "draft_fingerprint": _draft_fingerprint(record),
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
    return _record_with_fingerprint(record)


def create_draft(runs_root: str | Path, tasks_root: str | Path, spec: dict[str, Any], *, actor: str = "panel") -> dict[str, Any]:
    raw = _validate_spec(spec)
    task_id = str(raw["task_id"])
    draft_spec = _canonical_draft_spec(spec, task_id)
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
            "draft_spec": draft_spec,
            "created_at": now,
            "updated_at": now,
            "created_by": actor,
            "updated_by": actor,
            "revisions": [],
        }
        registry["tasks"][task_id] = record
        _write_registry(runs_root, registry)
        return {"action": "created", "task": _task_summary_from_record(record), "record": _record_with_fingerprint(record)}


def update_draft(
    runs_root: str | Path,
    task_id: str,
    spec: dict[str, Any],
    *,
    expected_draft_fingerprint: str,
    actor: str = "panel",
) -> dict[str, Any]:
    raw = _validate_spec(spec, task_id=task_id)
    expected = _validate_draft_fingerprint(expected_draft_fingerprint)
    with _LOCK:
        registry = _read_registry(runs_root)
        record = registry["tasks"].get(task_id)
        if not record:
            raise ValueError(f"任务单不存在: {task_id}")
        current = _draft_fingerprint(record)
        if current != expected:
            raise TaskConflictError("任务草稿已被其他操作修改，请重新加载后再保存")
        record["draft_spec"] = _canonical_draft_spec(spec, str(raw["task_id"]))
        record["status"] = "draft" if int(record.get("revision") or 0) <= 0 else "published_with_draft"
        record["updated_at"] = _now()
        record["updated_by"] = actor
        registry["tasks"][task_id] = record
        _write_registry(runs_root, registry)
        return {"action": "updated", "task": _task_summary_from_record(record), "record": _record_with_fingerprint(record)}


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


def publish_task(
    runs_root: str | Path,
    tasks_root: str | Path,
    task_id: str,
    *,
    expected_draft_fingerprint: str,
    actor: str = "panel",
    idempotency_key: str,
) -> dict[str, Any]:
    if not _safe_segment(task_id):
        raise ValueError("非法任务编号")
    key = _validate_idempotency_key(idempotency_key)
    expected = _validate_draft_fingerprint(expected_draft_fingerprint)
    with _LOCK:
        registry = _read_registry(runs_root)
        record = registry["tasks"].get(task_id)
        if not record:
            raise ValueError(f"任务单不存在: {task_id}")
        idempotency_records = record.get("publish_idempotency")
        if idempotency_records is None:
            idempotency_records = {}
        if not isinstance(idempotency_records, dict):
            raise ValueError("任务发布幂等记录必须是 JSON 对象")
        key_digest = _idempotency_key_digest(key)
        existing = idempotency_records.get(key_digest)
        if isinstance(existing, dict):
            if existing.get("draft_fingerprint") != expected:
                raise ValueError("幂等 key 已用于不同任务草稿，请使用新的 idempotency_key")
            published = _published_revision(record, int(existing.get("revision") or 0))
            if published is None:
                raise ValueError("幂等发布记录缺少对应 revision，请使用新的 idempotency_key")
            return {
                "action": "published",
                "idempotent": True,
                "task": _task_summary_from_record(record),
                "record": _record_with_fingerprint(record),
                "published": published,
            }
        draft_fingerprint = _draft_fingerprint(record)
        if draft_fingerprint != expected:
            raise TaskConflictError("任务草稿已被其他操作修改，请重新加载后再发布")
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
        idempotency_records[key_digest] = {
            "draft_fingerprint": draft_fingerprint,
            "revision": revision,
            "published_at": published["published_at"],
        }
        record["publish_idempotency"] = idempotency_records
        registry["tasks"][task_id] = record
        _write_registry(runs_root, registry)
        return {
            "action": "published",
            "idempotent": False,
            "task": _task_summary_from_record(record),
            "record": _record_with_fingerprint(record),
            "published": published,
        }
