from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
from typing import Any

from .annotation import get_provider
from .config import TaskConfig, build_text
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


def _suggestion_values_from_entry(task: TaskConfig, entry: dict[str, Any]) -> dict[str, Any]:
    source = entry.get("suggestions")
    if not isinstance(source, dict):
        source = entry.get("values")
    if not isinstance(source, dict):
        source = entry.get("prediction")
    if not isinstance(source, dict):
        source = entry
    return _suggestion_values(task, source)


def _rows_sha256(rows: list[dict[str, Any]]) -> str:
    payload = "\n".join(json_dumps(row) for row in rows) + "\n"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def json_dumps(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _provider_results(task: TaskConfig, rows: list[dict], provider_name: str) -> list[dict]:
    provider = get_provider(provider_name)
    payload = provider.annotate_batch(rows, task)
    if not isinstance(payload, dict):
        raise ValueError(f"provider {provider_name} 必须返回 dict payload")
    results = payload.get("results")
    if not isinstance(results, list):
        raise ValueError(f"provider {provider_name} 没有返回 results 列表")
    if len(results) != len(rows):
        raise ValueError(f"provider {provider_name} 返回 {len(results)} 条结果, 但输入有 {len(rows)} 条")
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


def _annotation_context(runs_root: str | Path, task: TaskConfig, annotation_id: str) -> tuple[Path, dict[str, Any], Path, str]:
    root = Path(runs_root) / task.task_id
    annotation_manifest_path = root / "annotation_jobs" / annotation_id / "manifest.json"
    if not annotation_manifest_path.is_file():
        raise ValueError(f"标注任务 manifest 不存在: {annotation_manifest_path}")
    annotation_manifest = read_json(annotation_manifest_path)
    dispatch_path = Path(annotation_manifest.get("dispatch_path") or "")
    if not dispatch_path.is_file():
        raise ValueError(f"标注任务分发文件不存在: {dispatch_path}")
    dataset = str(annotation_manifest.get("argilla_dataset") or annotation_manifest.get("dataset") or "").strip()
    return root, annotation_manifest, dispatch_path, dataset


def _suggestion_paths(root: Path, annotation_id: str, suggestion_id: str) -> tuple[Path, Path, Path, Path, Path]:
    output_dir = root / "suggestions" / annotation_id / suggestion_id
    return (
        output_dir,
        output_dir / "suggestions_template.jsonl",
        output_dir / "suggestions.jsonl",
        output_dir / "schema.json",
        output_dir / "manifest.json",
    )


def _base_manifest(
    task: TaskConfig,
    annotation_id: str,
    suggestion_id: str,
    annotation_manifest: dict[str, Any],
    dispatch_path: Path,
    dataset: str,
    provider: str,
    prompt_version: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "task_id": task.task_id,
        "task_revision": task.raw.get("revision"),
        "annotation_id": annotation_id,
        "argilla_dataset": dataset,
        "batch_plan_id": annotation_manifest.get("batch_plan_id"),
        "batch_ids": annotation_manifest.get("batch_ids") or [],
        "provider": provider,
        "prompt_version": prompt_version,
        "agent": f"{provider}:{prompt_version}",
        "input_path": str(dispatch_path),
        "input_sha256": _file_sha256(dispatch_path),
        "suggestion_id": suggestion_id,
        "created_at": _now(),
    }


def _label_schema(task: TaskConfig) -> dict[str, Any]:
    return {
        "id_field": task.id_field,
        "primary": task.primary_label,
        "auxiliary": task.auxiliary_labels,
        "label_names": _all_label_names(task),
        "annotation_guidelines": task.annotation_guidelines,
    }


def export_suggestion_template(
    runs_root: str | Path,
    task: TaskConfig,
    annotation_id: str,
    suggestion_id: str,
    *,
    provider: str = "external",
    prompt_version: str = "v001",
) -> dict[str, Any]:
    annotation_id = str(annotation_id or "").strip()
    suggestion_id = str(suggestion_id or "").strip()
    if not _safe_segment(annotation_id) or not _safe_segment(suggestion_id):
        raise ValueError("annotation_id 和 suggestion_id 只能使用单段名称")

    root, annotation_manifest, dispatch_path, dataset = _annotation_context(runs_root, task, annotation_id)
    output_dir, template_path, suggestions_path, schema_path, manifest_path = _suggestion_paths(root, annotation_id, suggestion_id)
    input_sha256 = _file_sha256(dispatch_path)
    if manifest_path.exists():
        existing = read_json(manifest_path)
        same_input = existing.get("input_sha256") == input_sha256
        same_provider = existing.get("provider") == provider
        same_prompt = existing.get("prompt_version") == prompt_version
        if same_input and same_provider and same_prompt and template_path.exists():
            return {"kind": "suggestion_template", "action": "reused", "idempotent": True, **existing}
        raise ValueError(f"suggestion_id 已存在且输入或参数不同: {suggestion_id}")

    rows = read_jsonl(dispatch_path)
    label_names = _all_label_names(task)
    template_rows = []
    for row in rows:
        template_rows.append({
            task.id_field: str(row[task.id_field]),
            "record_id": str(row[task.id_field]),
            "argilla_record_id": _record_argilla_id(task, row),
            "batch_id": row.get("__lls_batch_id"),
            "batch_plan_id": row.get("__lls_batch_plan_id") or annotation_manifest.get("batch_plan_id"),
            "text": build_text(row, task),
            "suggestions": {name: None for name in label_names},
            "agent": f"{provider}:{prompt_version}",
        })
    write_jsonl(template_rows, template_path)
    write_json(_label_schema(task), schema_path)
    manifest = {
        **_base_manifest(task, annotation_id, suggestion_id, annotation_manifest, dispatch_path, dataset, provider, prompt_version),
        "status": "template_exported",
        "records": 0,
        "template_rows": len(template_rows),
        "template_path": str(template_path),
        "schema_path": str(schema_path),
        "suggestions_path": str(suggestions_path),
        "manifest_path": str(manifest_path),
    }
    write_json(manifest, manifest_path)
    return {"kind": "suggestion_template", "action": "created", "idempotent": False, **manifest}


def _dispatch_lookup(task: TaskConfig, rows: list[dict[str, Any]]) -> tuple[dict[str, dict], dict[str, dict], set[str]]:
    by_argilla: dict[str, dict] = {}
    by_record: dict[str, dict] = {}
    duplicate_record_ids: set[str] = set()
    for row in rows:
        argilla_id = _record_argilla_id(task, row)
        by_argilla[argilla_id] = row
        record_id = str(row[task.id_field])
        if record_id in by_record:
            duplicate_record_ids.add(record_id)
        else:
            by_record[record_id] = row
    return by_argilla, by_record, duplicate_record_ids


def _normalize_external_suggestions(
    task: TaskConfig,
    annotation_manifest: dict[str, Any],
    dispatch_rows: list[dict[str, Any]],
    uploaded_rows: list[dict[str, Any]],
    *,
    agent: str,
) -> list[dict[str, Any]]:
    by_argilla, by_record, duplicate_record_ids = _dispatch_lookup(task, dispatch_rows)
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for index, entry in enumerate(uploaded_rows, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"第 {index} 行必须是 JSON 对象")
        argilla_id = str(entry.get("argilla_record_id") or entry.get("__lls_argilla_record_id") or "").strip()
        source = by_argilla.get(argilla_id) if argilla_id else None
        if source is None:
            record_id = str(entry.get(task.id_field) or entry.get("record_id") or entry.get("id") or "").strip()
            if not record_id:
                raise ValueError(f"第 {index} 行缺少 record_id 或 argilla_record_id")
            if record_id in duplicate_record_ids:
                raise ValueError(f"第 {index} 行 record_id={record_id} 在批次中重复，必须提供 argilla_record_id")
            source = by_record.get(record_id)
        if source is None:
            raise ValueError(f"第 {index} 行不属于该标注任务分发文件")
        resolved_argilla_id = _record_argilla_id(task, source)
        if resolved_argilla_id in seen:
            raise ValueError(f"重复的 argilla_record_id: {resolved_argilla_id}")
        seen.add(resolved_argilla_id)
        suggestions = _suggestion_values_from_entry(task, entry)
        if not suggestions:
            continue
        item = {
            task.id_field: str(source[task.id_field]),
            "record_id": str(source[task.id_field]),
            "argilla_record_id": resolved_argilla_id,
            "batch_id": source.get("__lls_batch_id"),
            "batch_plan_id": source.get("__lls_batch_plan_id") or annotation_manifest.get("batch_plan_id"),
            "suggestions": suggestions,
            "agent": str(entry.get("agent") or agent),
        }
        if isinstance(entry.get("scores"), dict):
            item["scores"] = entry["scores"]
        elif entry.get("score") not in (None, ""):
            item["score"] = entry["score"]
        out.append(item)
    if not out:
        raise ValueError("上传文件没有包含任何可用 suggestions")
    return out


def _publish_existing(
    task: TaskConfig,
    annotation_manifest: dict[str, Any],
    dispatch_path: Path,
    dataset: str,
    suggestions_path: Path,
    manifest_path: Path,
    argilla: dict[str, Any] | None,
) -> dict[str, Any]:
    if not dataset:
        raise ValueError("写入 Argilla Suggestions 需要 annotation manifest 记录 argilla_dataset")
    manifest = read_json(manifest_path)
    publish_params = _argilla_publish_params(annotation_manifest, argilla)
    try:
        manifest["publish"] = push_suggestions(task, dispatch_path, dataset, suggestions_path, publish_params)
        manifest["status"] = "published"
        manifest["published_at"] = _now()
        manifest.pop("error", None)
    except Exception as exc:
        manifest["status"] = "publish_failed"
        manifest["error"] = str(exc)
        write_json(manifest, manifest_path)
        raise
    write_json(manifest, manifest_path)
    return manifest


def import_external_suggestions(
    runs_root: str | Path,
    task: TaskConfig,
    annotation_id: str,
    suggestion_id: str,
    rows: list[dict[str, Any]],
    *,
    provider: str = "external",
    prompt_version: str = "v001",
    publish: bool = False,
    argilla: dict[str, Any] | None = None,
) -> dict[str, Any]:
    annotation_id = str(annotation_id or "").strip()
    suggestion_id = str(suggestion_id or "").strip()
    if not _safe_segment(annotation_id) or not _safe_segment(suggestion_id):
        raise ValueError("annotation_id 和 suggestion_id 只能使用单段名称")
    if not rows:
        raise ValueError("上传文件没有内容")

    root, annotation_manifest, dispatch_path, dataset = _annotation_context(runs_root, task, annotation_id)
    output_dir, template_path, suggestions_path, schema_path, manifest_path = _suggestion_paths(root, annotation_id, suggestion_id)
    input_sha256 = _file_sha256(dispatch_path)
    source_sha256 = _rows_sha256(rows)
    if manifest_path.exists():
        existing = read_json(manifest_path)
        same_input = existing.get("input_sha256") == input_sha256
        same_provider = existing.get("provider") == provider
        same_prompt = existing.get("prompt_version") == prompt_version
        same_source = existing.get("source_sha256") == source_sha256
        has_suggestions = suggestions_path.exists() and existing.get("status") != "template_exported"
        if has_suggestions and same_input and same_provider and same_prompt and same_source:
            result = {"kind": "suggestions", "action": "reused", "idempotent": True, **existing}
            if publish:
                result = {**result, **_publish_existing(task, annotation_manifest, dispatch_path, dataset, suggestions_path, manifest_path, argilla)}
            return result
        if has_suggestions:
            raise ValueError(f"suggestion_id 已存在且输入或参数不同: {suggestion_id}")

    dispatch_rows = read_jsonl(dispatch_path)
    normalized = _normalize_external_suggestions(
        task,
        annotation_manifest,
        dispatch_rows,
        rows,
        agent=f"{provider}:{prompt_version}",
    )
    write_jsonl(normalized, suggestions_path)
    if not schema_path.exists():
        write_json(_label_schema(task), schema_path)
    manifest = {
        **_base_manifest(task, annotation_id, suggestion_id, annotation_manifest, dispatch_path, dataset, provider, prompt_version),
        "status": "generated",
        "records": len(normalized),
        "template_path": str(template_path) if template_path.exists() else None,
        "schema_path": str(schema_path),
        "suggestions_path": str(suggestions_path),
        "manifest_path": str(manifest_path),
        "source_sha256": source_sha256,
        "imported_at": _now(),
    }
    write_json({k: v for k, v in manifest.items() if v is not None}, manifest_path)
    if publish:
        manifest = _publish_existing(task, annotation_manifest, dispatch_path, dataset, suggestions_path, manifest_path, argilla)
    return {"kind": "suggestions", "action": "imported", "idempotent": False, **manifest}


def publish_suggestions_for_annotation_job(
    runs_root: str | Path,
    task: TaskConfig,
    annotation_id: str,
    suggestion_id: str,
    *,
    argilla: dict[str, Any] | None = None,
) -> dict[str, Any]:
    annotation_id = str(annotation_id or "").strip()
    suggestion_id = str(suggestion_id or "").strip()
    if not _safe_segment(annotation_id) or not _safe_segment(suggestion_id):
        raise ValueError("annotation_id 和 suggestion_id 只能使用单段名称")
    root, annotation_manifest, dispatch_path, dataset = _annotation_context(runs_root, task, annotation_id)
    _, _, suggestions_path, _, manifest_path = _suggestion_paths(root, annotation_id, suggestion_id)
    if not manifest_path.is_file() or not suggestions_path.is_file():
        raise ValueError(f"机器建议产物不存在: {annotation_id}/{suggestion_id}")
    manifest = _publish_existing(task, annotation_manifest, dispatch_path, dataset, suggestions_path, manifest_path, argilla)
    return {"kind": "suggestions", "action": "published", "idempotent": False, **manifest}


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
                try:
                    push_result = push_suggestions(task, dispatch_path, dataset, suggestions_path, publish_params)
                except Exception as exc:
                    failed = {**existing, "status": "publish_failed", "error": str(exc)}
                    write_json(failed, manifest_path)
                    raise
                result["publish"] = push_result
                published = {**existing, "status": "published", "publish": push_result, "published_at": _now()}
                published.pop("error", None)
                write_json(published, manifest_path)
            return result
        raise ValueError(f"suggestion_id 已存在且输入或参数不同: {suggestion_id}")

    rows = read_jsonl(dispatch_path)
    results = _provider_results(task, rows, provider)
    suggestion_rows: list[dict[str, Any]] = []
    agent = f"{provider}:{prompt_version}"
    for row, result in zip(rows, results, strict=True):
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
        "status": "generated",
    }
    write_json(manifest, manifest_path)
    if publish:
        publish_params = _argilla_publish_params(annotation_manifest, argilla)
        try:
            manifest["publish"] = push_suggestions(task, dispatch_path, dataset, suggestions_path, publish_params)
            manifest["status"] = "published"
            manifest["published_at"] = _now()
            manifest.pop("error", None)
        except Exception as exc:
            manifest["status"] = "publish_failed"
            manifest["error"] = str(exc)
            write_json(manifest, manifest_path)
            raise
    write_json(manifest, manifest_path)
    return {"kind": "suggestions", "action": "created", "idempotent": False, **manifest}
