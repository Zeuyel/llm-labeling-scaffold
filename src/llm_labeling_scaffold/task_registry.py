from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import uuid
from typing import Any

from .config import load_task
from .data_lake import DEFAULT_REGISTRY_URI, DataLakeError, _normalize_registry_uri, copy_uri_to_path
from .io import write_text_atomic


TASK_CACHE_METADATA = ".task_source.json"


@dataclass(frozen=True)
class SyncedTaskRegistry:
    registry_uri: str
    registry: dict[str, Any]
    tasks: dict[str, dict[str, Any]]


def task_registry_uri(value: str | None = None) -> str:
    return _normalize_registry_uri(value or os.environ.get("LLS_TASK_REGISTRY_URI") or DEFAULT_REGISTRY_URI)


def _task_roots(tasks_root: str | Path) -> list[Path]:
    parts: list[str] = []
    for chunk in str(tasks_root).split(os.pathsep):
        parts.extend(item.strip() for item in chunk.split(","))
    return [Path(item) for item in parts if item]


def _cache_root(tasks_root: str | Path) -> Path:
    roots = _task_roots(tasks_root)
    return roots[-1] if roots else Path("tasks")


def _safe_segment(value: str) -> bool:
    return bool(value) and ".." not in value and "/" not in value and "\\" not in value


def _file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _replace_directory(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if target.exists():
        backup = target.with_name(f".{target.name}.old.{os.getpid()}.{uuid.uuid4().hex}")
        os.replace(target, backup)
    try:
        os.replace(source, target)
    except Exception:
        if backup is not None and backup.exists() and not target.exists():
            os.replace(backup, target)
        raise
    if backup is not None:
        shutil.rmtree(backup, ignore_errors=True)


def _active_task_entries(registry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    tasks = registry.get("tasks")
    if not isinstance(tasks, dict):
        raise DataLakeError("数据湖登记表缺少 tasks；生产任务必须登记 task_uri")
    out: dict[str, dict[str, Any]] = {}
    for task_id, raw in tasks.items():
        if not isinstance(raw, dict):
            raise DataLakeError(f"任务登记必须是对象: {task_id}")
        task_key = str(task_id).strip()
        if not _safe_segment(task_key):
            raise DataLakeError(f"非法任务编号: {task_id}")
        status = str(raw.get("status") or "active").strip().lower()
        if status != "active":
            continue
        task_uri = str(raw.get("task_uri") or "").strip()
        if not task_uri:
            raise DataLakeError(f"启用任务缺少 task_uri: {task_key}")
        out[task_key] = dict(raw, task_uri=task_uri)
    return out


def _validate_task_cache(task_id: str, task_path: Path, spec: dict[str, Any], registry_uri: str) -> dict[str, Any]:
    task = load_task(task_path)
    if task.task_id != task_id:
        raise DataLakeError(f"task_uri 内容与登记任务编号不一致: registry={task_id}, task={task.task_id}")

    expected_dataset = str(spec.get("source_dataset_id") or "").strip()
    if expected_dataset:
        actual_dataset = str(task.data_lake.get("source_dataset_id") or "").strip()
        if actual_dataset != expected_dataset:
            raise DataLakeError(
                f"任务 data_lake.source_dataset_id 与 registry 不一致: task={actual_dataset or '-'}, registry={expected_dataset}"
            )

    task_registry = str(task.data_lake.get("lake_registry_uri") or "").strip()
    if task_registry:
        normalized = task_registry_uri(task_registry)
        if normalized != registry_uri:
            raise DataLakeError(f"任务 lake_registry_uri 与当前 registry 不一致: task={normalized}, registry={registry_uri}")

    return {
        "task_id": task.task_id,
        "profile": task.profile,
        "id_field": task.id_field,
        "source_dataset_id": expected_dataset or task.data_lake.get("source_dataset_id"),
    }


def sync_tasks_from_registry(tasks_root: str | Path, registry_uri: str | None = None) -> SyncedTaskRegistry:
    from .data_lake import read_yaml_uri

    normalized_registry_uri = task_registry_uri(registry_uri)
    registry = read_yaml_uri(normalized_registry_uri)
    active = _active_task_entries(registry)
    cache_root = _cache_root(tasks_root)
    cache_root.mkdir(parents=True, exist_ok=True)
    staging_root = cache_root / "_staging" / "task_registry"
    staging_root.mkdir(parents=True, exist_ok=True)

    synced: dict[str, dict[str, Any]] = {}
    for task_id, spec in active.items():
        task_uri = str(spec["task_uri"])
        staging_dir = staging_root / f"{task_id}.{os.getpid()}.{uuid.uuid4().hex}"
        staging_dir.mkdir(parents=True, exist_ok=False)
        downloaded = staging_dir / "task.yaml"
        try:
            copy_uri_to_path(task_uri, downloaded)
            task_meta = _validate_task_cache(task_id, downloaded, spec, normalized_registry_uri)
            checksum = _file_sha256(downloaded)
            publish_dir = staging_dir / "publish"
            publish_dir.mkdir(parents=True, exist_ok=False)
            os.replace(downloaded, publish_dir / "task.yaml")
            metadata = {
                "source": "r2_data_lake_task_registry",
                "registry_uri": normalized_registry_uri,
                "task_uri": task_uri,
                "task_id": task_id,
                "task_sha256": checksum,
                "source_dataset_id": task_meta.get("source_dataset_id"),
                "synced_at": datetime.now(timezone.utc).isoformat(),
            }
            write_text_atomic(
                json.dumps({k: v for k, v in metadata.items() if v not in (None, "")}, ensure_ascii=False, indent=2)
                + "\n",
                publish_dir / TASK_CACHE_METADATA,
            )
            target_dir = cache_root / task_id
            target_path = target_dir / "task.yaml"
            _replace_directory(publish_dir, target_dir)
            synced[task_id] = {
                **task_meta,
                "path": str(target_path),
                "task_uri": task_uri,
                "registry_uri": normalized_registry_uri,
                "task_sha256": checksum,
            }
        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)

    return SyncedTaskRegistry(registry_uri=normalized_registry_uri, registry=registry, tasks=synced)
