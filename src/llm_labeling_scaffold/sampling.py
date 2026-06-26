from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
from datetime import datetime, timezone
from pathlib import Path
import uuid

from .config import TaskConfig
from .io import append_jsonl, iter_jsonl, read_json, read_jsonl, write_json, write_jsonl


def _safe_segment(value: str) -> bool:
    return bool(value) and ".." not in value and "/" not in value and "\\" not in value


def _canonical_row(row: dict) -> str:
    return json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _rows_hash(rows: list[dict]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(_canonical_row(row).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _jsonl_hash(path: Path) -> str:
    digest = hashlib.sha256()
    for row in iter_jsonl(path):
        digest.update(_canonical_row(row).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fsync_dir(path: str | Path) -> None:
    try:
        fd = os.open(Path(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _staging_dir(task: TaskConfig, sample_id: str) -> Path:
    return task.runs_dir / "_staging" / "samples" / f"{sample_id}.{os.getpid()}.{uuid.uuid4().hex}"


def _publish_directory(staging: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(f"目标目录已存在: {target}")
    _fsync_dir(staging)
    os.replace(staging, target)
    _fsync_dir(target.parent)


def _archived_sample_exists(task: TaskConfig, sample_id: str) -> bool:
    archive = task.runs_dir / "_archive" / "samples"
    return archive.is_dir() and any(path.name.startswith(f"{sample_id}__") for path in archive.iterdir())


def _record_audit(task: TaskConfig, event: str, sample_id: str, *, status: str = "succeeded",
                  details: dict | None = None) -> None:
    if not _safe_segment(task.task_id):
        raise ValueError("非法任务编号")
    append_jsonl(
        {
            "event": event,
            "task_id": task.task_id,
            "asset_type": "sample",
            "asset_id": sample_id,
            "status": status,
            "actor": "system",
            "details": details or {},
            "created_at": _now(),
        },
        task.runs_dir / "_audit" / "events.jsonl",
    )


def sample_records(
    task: TaskConfig,
    rows: int,
    sample_id: str,
    strategy: str = "random",
    seed: int = 20260617,
    source_path: str | Path | None = None,
    source_import_id: str | None = None,
) -> Path:
    try:
        if not _safe_segment(sample_id):
            raise ValueError("样本编号只能使用单段名称，不能包含路径分隔符")
        if rows < 1:
            raise ValueError("样本行数必须大于 0")
        input_path = Path(source_path) if source_path else task.input_path
        source = read_jsonl(input_path)
        if strategy == "head":
            picked = source[:rows]
        elif strategy == "random":
            rng = random.Random(seed)
            picked = source[:]
            rng.shuffle(picked)
            picked = picked[:rows]
        else:
            raise ValueError(f"unsupported sampling strategy in MVP: {strategy}")
        sample_dir = task.runs_dir / "samples" / sample_id
        sample_path = sample_dir / "sample.jsonl"
        content_sha256 = _rows_hash(picked)
        manifest_path = sample_dir / "manifest.json"
        if not sample_dir.exists() and _archived_sample_exists(task, sample_id):
            raise ValueError(f"样本编号已归档，不能复用: {sample_id}。请使用新的样本编号。")
        if sample_dir.exists() and not sample_path.exists():
            raise ValueError(f"样本编号目录已存在但缺少 sample.jsonl: {sample_id}")
        if sample_path.exists():
            manifest = read_json(manifest_path) if manifest_path.exists() else {}
            if manifest.get("state") == "archived":
                raise ValueError(f"样本编号已归档，不能复用: {sample_id}。请使用新的样本编号。")
            existing_hash = manifest.get("content_sha256") or _jsonl_hash(sample_path)
            if existing_hash == content_sha256:
                _record_audit(
                    task,
                    "sample.reuse",
                    sample_id,
                    details={"path": str(sample_path), "content_sha256": content_sha256},
                )
                return sample_path
            raise ValueError(f"样本编号已存在且内容不同: {sample_id}。请使用新的样本编号，系统不会覆盖已有样本。")
        manifest = {
            "task_id": task.task_id,
            "sample_id": sample_id,
            "input_path": str(input_path),
            "id_field": task.id_field,
            "strategy": strategy,
            "seed": seed,
            "rows": len(picked),
            "content_sha256": content_sha256,
            "created_at": _now(),
            "state": "active",
            "schema_version": 1,
        }
        if source_import_id:
            manifest["source_import_id"] = source_import_id
        staging = _staging_dir(task, sample_id)
        try:
            write_jsonl(picked, staging / "sample.jsonl")
            write_json(manifest, staging / "manifest.json")
            _publish_directory(staging, sample_dir)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        _record_audit(
            task,
            "sample.create",
            sample_id,
            details={
                "path": str(sample_path),
                "rows": len(picked),
                "strategy": strategy,
                "source": str(input_path),
                "source_import_id": source_import_id,
                "content_sha256": content_sha256,
            },
        )
        return sample_path
    except Exception as exc:
        if _safe_segment(sample_id):
            _record_audit(
                task,
                "sample.save",
                sample_id,
                status="failed",
                details={"error": str(exc), "source": str(source_path or task.input_path)},
            )
        raise
