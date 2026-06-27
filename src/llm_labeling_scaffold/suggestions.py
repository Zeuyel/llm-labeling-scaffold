from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
from typing import Any

from .annotation import get_provider
from .config import TaskConfig
from .integrations.argilla import push_suggestions
from .io import read_json, read_jsonl, write_json, write_jsonl


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_segment(value: str) -> bool:
    return bool(value) and ".." not in value and "/" not in value and "\\" not in value


def _file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _all_label_names(task: TaskConfig) -> list[str]:
    labels = [task.primary_label, *task.auxiliary_labels]
    return [str(label["name"]) for label in labels]


def _record_argilla_id(task: TaskConfig, row: dict) -> str:
    value = row.get("__lls_argilla_record_id")
    if value not in (None, ""):
        return str(value)
    original_id = str(row[task.id_field])
    batch_id = row.get("__lls_batch_id")
    if batch_id not in (None, ""):
        return f"{original_id}__{batch_id}"
    return original_id


def _suggestion_values(task: TaskConfig, result: dict) -> dict[str, Any]:
    names = set(_all_label_names(task))
    return {name: result[name] for name in names if result.get(name) not in (None, "")}


def _provider_results(task: TaskConfig, rows: list[dict], provider_name: str) -> list[dict]:
    if provider_name == "codex_exec":
        raise ValueError("codex_exec provider 尚未实现；当前先支持本地 suggestions 产物和 Argilla Suggestions 写入边界")
    provider = get_provider(provider_name)
    payload = provider.annotate_batch(rows, task)
    results = payload.get("results")
    if not isinstance(results, list):
        raise ValueError(f"provider {provider_name} 没有返回 results 列表")
    if len(results) != len(rows):
        raise ValueError(f"provider {provider_name} 返回 {len(results)} 条结果，但输入有 {len(rows)} 条")
    return [dict(item) for item in results]


def _argilla_publish_params(annotation_manifest: dict[str, Any], params: dict[str, Any] | None) -> dict[str, Any]:
    out = dict(params or {})
    record_id_policy = annotation_manifest.get("record_id_policy")
    if isinstance(record_id_policy, dict) and record_id_policy.get("strategy"):
        out.setdefault("record_id_strategy", record_id_policy["strategy"])
    for key in ("dispatch_mode", "batch_plan_id", "batch_manifest_path"):
        value = annotation_manifest.get(key)
        if value not in (None, "", [], {}):
            out.setdefault(key, value)
    return out


def generate_suggestions_for_annotation_job(
    runs_root: str | Path,
    task: TaskConfig,
    annotation_id: str,
    suggestion_id: str,
    *,
    provider: str = "local_stub",
    prompt_version: str = "v001",
    publish: bool = False,
    argilla: dict[str, Any] | None = None,
) -> dict[str, Any]:
    annotation_id = str(annotation_id or "").strip()
    suggestion_id = str(suggestion_id or "").strip()
    if not _safe_segment(annotation_id) or not _safe_segment(suggestion_id):
        raise ValueError("annotation_id 和 suggestion_id 只能使用单段名称")

    root = Path(runs_root) / task.task_id
    annotation_manifest_path = root / "annotation_jobs" / annotation_id / "manifest.json"
    if not annotation_manifest_path.is_file():
        raise ValueError(f"标注任务 manifest 不存在: {annotation_manifest_path}")
    annotation_manifest = read_json(annotation_manifest_path)
    dispatch_path = Path(annotation_manifest.get("dispatch_path") or "")
    if not dispatch_path.is_file():
        raise ValueError(f"标注任务分发文件不存在: {dispatch_path}")
    dataset = str(annotation_manifest.get("argilla_dataset") or annotation_manifest.get("dataset") or "").strip()
    if publish and not dataset:
        raise ValueError("写入 Argilla Suggestions 需要 annotation manifest 记录 argilla_dataset")

    output_dir = root / "suggestions" / annotation_id / suggestion_id
    suggestions_path = output_dir / "suggestions.jsonl"
    manifest_path = output_dir / "manifest.json"
    input_sha256 = _file_sha256(dispatch_path)
    if manifest_path.exists():
        existing = read_json(manifest_path)
        same_input = existing.get("input_sha256") == input_sha256
        same_provider = existing.get("provider") == provider
        same_prompt = existing.get("prompt_version") == prompt_version
        if same_input and same_provider and same_prompt and suggestions_path.exists():
            result = {"kind": "suggestions", "action": "reused", "idempotent": True, **existing}
            if publish:
                publish_params = _argilla_publish_params(annotation_manifest, argilla)
                push_result = push_suggestions(task, dispatch_path, dataset, suggestions_path, publish_params)
                result["publish"] = push_result
            return result
        raise ValueError(f"suggestion_id 已存在且输入或参数不同: {suggestion_id}")

    rows = read_jsonl(dispatch_path)
    results = _provider_results(task, rows, provider)
    suggestion_rows: list[dict[str, Any]] = []
    agent = f"{provider}:{prompt_version}"
    for row, result in zip(rows, results):
        suggestions = _suggestion_values(task, result)
        if not suggestions:
            continue
        suggestion_rows.append({
            task.id_field: str(row[task.id_field]),
            "record_id": str(row[task.id_field]),
            "argilla_record_id": _record_argilla_id(task, row),
            "batch_id": row.get("__lls_batch_id"),
            "batch_plan_id": row.get("__lls_batch_plan_id") or annotation_manifest.get("batch_plan_id"),
            "suggestions": suggestions,
            "agent": agent,
        })

    write_jsonl(suggestion_rows, suggestions_path)
    manifest = {
        "schema_version": 1,
        "task_id": task.task_id,
        "task_revision": task.raw.get("revision"),
        "annotation_id": annotation_id,
        "argilla_dataset": dataset,
        "batch_plan_id": annotation_manifest.get("batch_plan_id"),
        "batch_ids": annotation_manifest.get("batch_ids") or [],
        "provider": provider,
        "prompt_version": prompt_version,
        "agent": agent,
        "input_path": str(dispatch_path),
        "input_sha256": input_sha256,
        "suggestion_id": suggestion_id,
        "suggestions_path": str(suggestions_path),
        "records": len(suggestion_rows),
        "created_at": _now(),
        "status": "published" if publish else "generated",
    }
    if publish:
        publish_params = _argilla_publish_params(annotation_manifest, argilla)
        manifest["publish"] = push_suggestions(task, dispatch_path, dataset, suggestions_path, publish_params)
    write_json(manifest, manifest_path)
    return {"kind": "suggestions", "action": "created", "idempotent": False, **manifest}
