from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any

from ..config import TaskConfig, build_text
from ..io import read_jsonl, write_jsonl


def _load_argilla():
    try:
        import argilla as rg
    except ImportError as exc:
        raise RuntimeError("Argilla integration requires `pip install -e '.[argilla]'`") from exc
    return rg


def _client(api_url: str | None = None, api_key: str | None = None):
    rg = _load_argilla()
    return rg.Argilla(
        api_url=api_url or os.environ.get("ARGILLA_API_URL", "http://localhost:6900"),
        api_key=api_key or os.environ.get("ARGILLA_API_KEY", "argilla.apikey"),
    )


def _api_url(api_url: str | None = None) -> str:
    return api_url or os.environ.get("ARGILLA_API_URL", "http://localhost:6900")


def _all_label_fields(task: TaskConfig) -> list[dict[str, Any]]:
    return [task.primary_label, *task.auxiliary_labels]


def _question_title(label: dict[str, Any]) -> str:
    return str(label.get("title") or label.get("description") or label["name"])


def _question_required(label: dict[str, Any]) -> bool:
    return bool(label.get("required", True))


def _make_question(cls, **kwargs):
    try:
        return cls(**kwargs)
    except TypeError:
        kwargs.pop("required", None)
        return cls(**kwargs)


def _value_label(value_labels: dict[str, Any], value: Any) -> str | None:
    item = value_labels.get(value)
    if item is None:
        item = value_labels.get(str(value))
    if item is None:
        return None
    if isinstance(item, dict):
        text = item.get("label") or item.get("title") or item.get("name")
    else:
        text = item
    if text in (None, ""):
        return None
    return str(text)


def _label_values(label: dict[str, Any]) -> list[str] | dict[str, str]:
    values = label.get("values", [])
    value_labels = label.get("value_labels")
    if isinstance(value_labels, dict) and values:
        mapped: dict[str, str] = {}
        for value in values:
            key = str(value)
            mapped[key] = _value_label(value_labels, value) or key
        return mapped
    return [str(value) for value in values]


def _questions_for_task(rg, task: TaskConfig) -> list:
    questions = []
    for label in _all_label_fields(task):
        name = label["name"]
        title = _question_title(label)
        label_type = label.get("type", "string")
        required = _question_required(label)
        if label_type == "categorical":
            questions.append(_make_question(
                rg.LabelQuestion,
                name=name,
                title=title,
                labels=_label_values(label),
                required=required,
            ))
        elif label_type == "integer" and "values" in label:
            questions.append(_make_question(
                rg.LabelQuestion,
                name=name,
                title=title,
                labels=_label_values(label),
                required=required,
            ))
        elif label_type == "boolean":
            questions.append(_make_question(
                rg.LabelQuestion,
                name=name,
                title=title,
                labels=["true", "false"],
                required=required,
            ))
        else:
            text_question = getattr(rg, "TextQuestion", None)
            if text_question is None:
                raise RuntimeError("Argilla SDK missing TextQuestion; cannot sync free-text label fields")
            questions.append(_make_question(text_question, name=name, title=title, required=required))
    return questions


def _response_value(raw):
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw.get("value")
    if hasattr(raw, "value"):
        return raw.value
    return raw


def _suggestion_entries(params: dict[str, Any]) -> list[dict[str, Any]]:
    entries = params.get("suggestions")
    if entries is None and params.get("suggestions_path"):
        entries = read_jsonl(params["suggestions_path"])
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise ValueError("suggestions 必须是列表或 suggestions_path 指向的 JSONL")
    out: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("每条 suggestion 必须是 JSON object")
        out.append(entry)
    return out


def _suggestion_entries_by_id(task: TaskConfig, params: dict[str, Any]) -> dict[str, dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for entry in _suggestion_entries(params):
        ids = [
            entry.get("argilla_record_id"),
            entry.get("__lls_argilla_record_id"),
            entry.get("record_id"),
            entry.get("id"),
            entry.get(task.id_field),
            entry.get("__lls_original_id"),
        ]
        for value in ids:
            if value in (None, ""):
                continue
            by_id.setdefault(str(value), entry)
    return by_id


def _suggestion_source(entry: dict[str, Any]) -> dict[str, Any]:
    for key in ("suggestions", "values", "prediction", "human_label"):
        value = entry.get(key)
        if isinstance(value, dict):
            return value
    return entry


def _suggestion_values(task: TaskConfig, entry: dict[str, Any]) -> dict[str, Any]:
    source = _suggestion_source(entry)
    out: dict[str, Any] = {}
    for label in _all_label_fields(task):
        name = label["name"]
        if name not in source:
            continue
        value = source.get(name)
        if value in (None, ""):
            continue
        out[name] = _cast_value(label, _response_value(value))
    return out


def _suggestion_score(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    return score if math.isfinite(score) else None


def _make_suggestion(rg, *, question_name: str, value: Any, score: Any = None, agent: str | None = None):
    suggestion_cls = getattr(rg, "Suggestion", None)
    if suggestion_cls is None:
        raise RuntimeError("Argilla SDK missing Suggestion; cannot sync machine suggestions")
    kwargs = {"question_name": question_name, "value": value}
    resolved_score = _suggestion_score(score)
    if resolved_score is not None:
        kwargs["score"] = resolved_score
    if agent:
        kwargs["agent"] = agent
    try:
        return suggestion_cls(**kwargs)
    except TypeError:
        kwargs.pop("agent", None)
        try:
            return suggestion_cls(**kwargs)
        except TypeError:
            kwargs.pop("score", None)
            return suggestion_cls(**kwargs)


def _record_suggestions(
    rg,
    task: TaskConfig,
    row: dict,
    record_id: str,
    params: dict[str, Any],
    suggestions_by_id: dict[str, dict[str, Any]],
) -> list:
    original_id = str(row[task.id_field])
    entry = (
        suggestions_by_id.get(record_id)
        or suggestions_by_id.get(str(row.get("__lls_argilla_record_id") or ""))
        or suggestions_by_id.get(original_id)
        or suggestions_by_id.get(str(row.get("__lls_original_id") or ""))
    )
    if not entry:
        return []
    values = _suggestion_values(task, entry)
    scores = entry.get("scores") if isinstance(entry.get("scores"), dict) else {}
    agent = str(entry.get("agent") or params.get("suggestions_agent") or params.get("agent") or "machine_suggestion")
    return [
        _make_suggestion(
            rg,
            question_name=name,
            value=value,
            score=scores.get(name, entry.get("score")),
            agent=agent,
        )
        for name, value in values.items()
    ]


def _cast_value(label: dict[str, Any], value):
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    label_type = label.get("type", "string")
    if label_type == "integer":
        return int(value)
    if label_type == "number":
        return float(value)
    if label_type == "boolean":
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().lower()
        if normalized in {"1", "true", "yes", "y"}:
            return True
        if normalized in {"0", "false", "no", "n"}:
            return False
        raise ValueError(f"invalid boolean value: {value}")
    if label_type == "categorical":
        return str(value)
    return str(value)


def _human_label_from_values(task: TaskConfig, values: dict) -> dict:
    human_label = {}
    for label in _all_label_fields(task):
        name = label["name"]
        raw_value = _response_value(values.get(name))
        if raw_value is None:
            continue
        try:
            value = _cast_value(label, raw_value)
        except (TypeError, ValueError):
            value = raw_value
        if value is not None:
            human_label[name] = value
    return human_label


def _guidelines_for_task(task: TaskConfig, params: dict[str, Any]) -> str:
    return str(params.get("guidelines") or task.annotation_guidelines or f"Label records for task {task.task_id}.")


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _annotation_argilla_options(task: TaskConfig) -> dict[str, Any]:
    value = task.annotation.get("argilla")
    return dict(value) if isinstance(value, dict) else {}


def _workspace_name(value) -> str:
    if isinstance(value, str):
        return value
    if hasattr(value, "name"):
        return str(value.name)
    return str(value)


def _existing_dataset(client, dataset_name: str, workspace: str):
    for dataset in client.datasets.list():
        if getattr(dataset, "name", None) == dataset_name and _workspace_name(getattr(dataset, "workspace", "")) == workspace:
            return dataset
    return None


def _prepare_dataset(client, dataset, dataset_name: str, workspace: str, if_exists: str):
    if_exists = if_exists or "fail"
    if if_exists not in {"fail", "append", "replace"}:
        raise ValueError("Argilla 同名数据集策略只能是 fail、append 或 replace")
    existing = _existing_dataset(client, dataset_name, workspace)
    if not existing:
        return dataset.create(), "created"
    if if_exists == "fail":
        raise ValueError(f"Argilla 数据集已存在: {workspace}/{dataset_name}")
    if if_exists == "append":
        return existing, "appended"
    existing.delete()
    return dataset.create(), "replaced"


def _record_source_id(record, task: TaskConfig) -> str:
    metadata = getattr(record, "metadata", None) or {}
    if isinstance(metadata, dict) and task.id_field in metadata:
        return str(metadata[task.id_field])
    return str(record.id)


_BATCH_ID_ROW_FIELD = "__lls_batch_id"
_CONTEXT_METADATA_FIELDS = (
    "dispatch_mode",
    "batch_plan_id",
    "batch_id",
    "batch_manifest_path",
    "overlap_role",
)
_ROW_CONTEXT_METADATA_FIELDS = {
    "__lls_dispatch_mode": "dispatch_mode",
    "__lls_batch_plan_id": "batch_plan_id",
    _BATCH_ID_ROW_FIELD: "batch_id",
    "__lls_batch_manifest_path": "batch_manifest_path",
    "__lls_overlap_role": "overlap_role",
}
_VISIBLE_BATCH_CONTEXT_FIELDS = (
    "dispatch_mode",
    "batch_plan_id",
    "batch_id",
    "overlap_role",
)


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _record_id_strategy(params: dict[str, Any]) -> str:
    strategy = str(params.get("record_id_strategy") or "original").strip().lower()
    if strategy in {"", "original", "source", "source_id", "task_id_field"}:
        return "original"
    if strategy == "batch_scoped":
        return strategy
    raise ValueError("Argilla record_id_strategy 目前仅支持 original 或 batch_scoped")


def _duplicate_values(values: list[str]) -> list[str]:
    seen: set[str] = set()
    duplicate_seen: set[str] = set()
    duplicates: list[str] = []
    for value in values:
        if value in seen and value not in duplicate_seen:
            duplicates.append(value)
            duplicate_seen.add(value)
        seen.add(value)
    return duplicates


def _duplicate_pairs(pairs: list[tuple[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    duplicate_seen: set[tuple[str, str]] = set()
    duplicates: list[dict[str, str]] = []
    for batch_id, original_id in pairs:
        key = (batch_id, original_id)
        if key in seen and key not in duplicate_seen:
            duplicates.append({"batch_id": batch_id, "id": original_id})
            duplicate_seen.add(key)
        seen.add(key)
    return duplicates


def _batch_id(row: dict) -> str | None:
    value = row.get(_BATCH_ID_ROW_FIELD)
    if value in (None, ""):
        return None
    return str(value)


def _record_id_for_row(row: dict, task: TaskConfig, strategy: str) -> str:
    original_id = str(row[task.id_field])
    if strategy == "batch_scoped":
        batch_id = _batch_id(row)
        if batch_id:
            return f"{original_id}__{batch_id}"
    return original_id


def _record_context_metadata(row: dict, params: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for field in _CONTEXT_METADATA_FIELDS:
        if field in params and params[field] not in (None, ""):
            metadata[field] = params[field]
    for row_field, metadata_field in _ROW_CONTEXT_METADATA_FIELDS.items():
        if metadata_field not in metadata and row.get(row_field) not in (None, ""):
            metadata[metadata_field] = row[row_field]
    return metadata


def _record_metadata(
    task: TaskConfig,
    row: dict,
    sample_path: str | Path,
    params: dict[str, Any],
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        task.id_field: str(row[task.id_field]),
        "task_id": task.task_id,
        "sample_path": str(sample_path),
    }
    metadata.update({field: row[field] for field in task.metadata_fields if field in row})
    metadata.update(_record_context_metadata(row, params))
    return metadata


def _argilla_context_field_name(task: TaskConfig, params: dict[str, Any]) -> str:
    options = _annotation_argilla_options(task)
    return str(
        params.get("context_field")
        or params.get("context_field_name")
        or options.get("context_field")
        or task.annotation.get("context_field")
        or "context"
    )


def _argilla_context_title(task: TaskConfig, params: dict[str, Any]) -> str:
    options = _annotation_argilla_options(task)
    return str(
        params.get("context_title")
        or options.get("context_title")
        or task.annotation.get("context_title")
        or "Context"
    )


def _argilla_context_fields(task: TaskConfig, params: dict[str, Any]) -> list[str]:
    options = _annotation_argilla_options(task)
    explicit = (
        params.get("context_fields")
        if "context_fields" in params
        else options.get("context_fields", task.annotation.get("context_fields"))
    )
    if explicit is False:
        return []
    fields = _string_list(explicit)
    if not fields:
        fields = [task.id_field, *task.metadata_fields]
    for field in _VISIBLE_BATCH_CONTEXT_FIELDS:
        if field not in fields:
            fields.append(field)
    deduped: list[str] = []
    seen: set[str] = set()
    for field in fields:
        if field in seen:
            continue
        seen.add(field)
        deduped.append(field)
    return deduped


def _truncate_context_value(value: Any, max_chars: int) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        text = "; ".join(str(item) for item in value)
    elif isinstance(value, dict):
        text = "; ".join(f"{key}={val}" for key, val in value.items())
    else:
        text = str(value)
    text = text.strip()
    if max_chars > 0 and len(text) > max_chars:
        return text[: max_chars - 3].rstrip() + "..."
    return text


def _build_visible_context(task: TaskConfig, row: dict, sample_path: str | Path, params: dict[str, Any]) -> str:
    fields = _argilla_context_fields(task, params)
    if not fields:
        return ""
    options = _annotation_argilla_options(task)
    max_chars = int(params.get("context_value_max_chars") or options.get("context_value_max_chars") or 500)
    metadata = _record_metadata(task, row, sample_path, params)
    lines: list[str] = []
    for field in fields:
        value = metadata.get(field)
        if value in (None, ""):
            value = row.get(field)
        text = _truncate_context_value(value, max_chars)
        if text:
            lines.append(f"{field}: {text}")
    return "\n".join(lines)


def _record_fields(
    task: TaskConfig,
    row: dict,
    sample_path: str | Path,
    text_field: str,
    params: dict[str, Any],
) -> dict[str, str]:
    fields = {text_field: build_text(row, task)}
    context_field = _argilla_context_field_name(task, params)
    if context_field and context_field != text_field:
        context = _build_visible_context(task, row, sample_path, params)
        if context:
            fields[context_field] = context
    return fields


def _argilla_text_fields(rg, task: TaskConfig, text_field: str, params: dict[str, Any]) -> list:
    fields = [rg.TextField(name=text_field, title=str(params.get("text_title") or "Text"))]
    context_field = _argilla_context_field_name(task, params)
    if context_field and context_field != text_field and _argilla_context_fields(task, params):
        fields.append(rg.TextField(name=context_field, title=_argilla_context_title(task, params)))
    return fields


def _record_id_policy(task: TaskConfig, params: dict[str, Any]) -> dict[str, Any]:
    strategy = _record_id_strategy(params)
    policy: dict[str, Any] = {
        "strategy": strategy,
        "id_field": task.id_field,
        "allow_duplicate_record_ids": _truthy(params.get("allow_duplicate_record_ids")),
    }
    if strategy == "batch_scoped":
        policy.update({
            "batch_id_field": _BATCH_ID_ROW_FIELD,
            "format": "{original_id}__{batch_id}",
        })
    if "record_id_template" in params:
        policy["record_id_template"] = params["record_id_template"]
    if "record_id_fields" in params:
        policy["record_id_fields"] = params["record_id_fields"]
    return policy


def _duplicate_record_id_info(rows: list[dict], task: TaskConfig, strategy: str) -> dict[str, Any]:
    original_ids = [str(row[task.id_field]) for row in rows]
    record_ids = [_record_id_for_row(row, task, strategy) for row in rows]
    info: dict[str, Any] = {
        "original_ids": _duplicate_values(original_ids),
        "record_ids": _duplicate_values(record_ids),
    }
    if strategy == "batch_scoped":
        batch_pairs = [(_batch_id(row) or "", str(row[task.id_field])) for row in rows]
        info["same_batch_original_ids"] = _duplicate_pairs(batch_pairs)
    return info


def _validate_record_ids(
    task: TaskConfig,
    policy: dict[str, Any],
    duplicate_record_ids: dict[str, Any],
) -> None:
    strategy = str(policy["strategy"])
    allow_duplicates = bool(policy["allow_duplicate_record_ids"])
    same_batch_duplicates = duplicate_record_ids.get("same_batch_original_ids") or []
    if same_batch_duplicates:
        preview = ", ".join(
            f"{item['id']}@{item['batch_id'] or '<missing_batch>'}"
            for item in same_batch_duplicates[:10]
        )
        raise ValueError(
            f"batch_scoped record id 策略检测到同一 batch 内重复 {task.id_field}: {preview}；请先对该批次去重。"
        )
    if allow_duplicates:
        return
    duplicate_ids = duplicate_record_ids.get("record_ids") or []
    if duplicate_ids:
        preview = ", ".join(str(value) for value in duplicate_ids[:10])
        if strategy == "batch_scoped":
            raise ValueError(
                f"Argilla Record.id 仍存在重复: {preview}；请检查 {_BATCH_ID_ROW_FIELD} 或显式去重策略。"
            )
        raise ValueError(
            f"Argilla Record.id 存在重复: {preview}。输入可能是 overlap 批次的全量合并；"
            "请推送单个批次，或使用 record_id_strategy='batch_scoped' / 显式去重策略后再推送。"
        )


def _prepare_records_for_push(
    rg,
    task: TaskConfig,
    sample_path: str | Path,
    text_field: str,
    params: dict[str, Any],
) -> tuple[list, dict[str, Any], dict[str, Any]]:
    rows = read_jsonl(sample_path)
    policy = _record_id_policy(task, params)
    duplicate_record_ids = _duplicate_record_id_info(rows, task, str(policy["strategy"]))
    _validate_record_ids(task, policy, duplicate_record_ids)
    suggestions_by_id = _suggestion_entries_by_id(task, params)
    records = []
    for row in rows:
        record_id = _record_id_for_row(row, task, str(policy["strategy"]))
        kwargs = {
            "id": record_id,
            "fields": _record_fields(task, row, sample_path, text_field, params),
            "metadata": _record_metadata(task, row, sample_path, params),
        }
        suggestions = _record_suggestions(rg, task, row, record_id, params, suggestions_by_id)
        if suggestions:
            kwargs["suggestions"] = suggestions
        records.append(rg.Record(**kwargs))
    return records, policy, duplicate_record_ids


def _record_suggestion_count(records: list) -> int:
    total = 0
    for record in records:
        total += len(getattr(record, "suggestions", []) or [])
    return total


def push_sample(task: TaskConfig, sample_path: str | Path, dataset_name: str, params: dict[str, Any] | None = None) -> dict:
    params = params or {}
    rg = _load_argilla()
    workspace = params.get("workspace") or os.environ.get("ARGILLA_WORKSPACE") or "argilla"
    text_field = params.get("text_field", "text")
    min_submitted = int(params.get("min_submitted", 1))
    if_exists = str(params.get("if_exists") or params.get("dataset_policy") or "fail")
    records, record_id_policy, duplicate_record_ids = _prepare_records_for_push(
        rg,
        task,
        sample_path,
        text_field,
        params,
    )

    client = _client(params.get("api_url"), params.get("api_key"))
    settings = rg.Settings(
        guidelines=_guidelines_for_task(task, params),
        fields=_argilla_text_fields(rg, task, text_field, params),
        questions=_questions_for_task(rg, task),
        distribution=rg.TaskDistribution(min_submitted=min_submitted),
        allow_extra_metadata=True,
    )
    dataset = rg.Dataset(name=dataset_name, workspace=workspace, settings=settings)
    dataset, dataset_action = _prepare_dataset(client, dataset, dataset_name, workspace, if_exists)

    dataset.records.log(records)
    return {
        "backend": "argilla",
        "workspace": workspace,
        "dataset": dataset_name,
        "dataset_action": dataset_action,
        "if_exists": if_exists,
        "records": len(records),
        "suggestions": _record_suggestion_count(records),
        "record_id_policy": record_id_policy,
        "duplicate_record_ids": duplicate_record_ids,
        "visible_fields": list(records[0].fields.keys()) if records else [text_field],
        "url": params.get("ui_url"),
    }


def push_suggestions(
    task: TaskConfig,
    sample_path: str | Path,
    dataset_name: str,
    suggestions_path: str | Path,
    params: dict[str, Any] | None = None,
) -> dict:
    params = dict(params or {})
    params["suggestions_path"] = str(suggestions_path)
    rg = _load_argilla()
    workspace = params.get("workspace") or os.environ.get("ARGILLA_WORKSPACE") or "argilla"
    text_field = params.get("text_field", "text")
    records, record_id_policy, duplicate_record_ids = _prepare_records_for_push(
        rg,
        task,
        sample_path,
        text_field,
        params,
    )
    suggestion_count = _record_suggestion_count(records)
    if suggestion_count <= 0:
        raise ValueError("没有可写入 Argilla 的 suggestions")

    client = _client(params.get("api_url"), params.get("api_key"))
    dataset = client.datasets(dataset_name, workspace=workspace)
    dataset.records.log(records)
    return {
        "backend": "argilla",
        "workspace": workspace,
        "dataset": dataset_name,
        "records": len(records),
        "suggestions": suggestion_count,
        "record_id_policy": record_id_policy,
        "duplicate_record_ids": duplicate_record_ids,
    }


def pull_responses(task: TaskConfig, dataset_name: str, output_path: str | Path, params: dict[str, Any] | None = None) -> dict:
    params = params or {}
    client = _client(params.get("api_url"), params.get("api_key"))
    workspace = params.get("workspace") or os.environ.get("ARGILLA_WORKSPACE") or "argilla"
    dataset = client.datasets(dataset_name, workspace=workspace)
    rows = []
    for record in dataset.records:
        record_id = _record_source_id(record, task)
        for response in getattr(record, "responses", []) or []:
            values = getattr(response, "values", {}) or {}
            human_label = _human_label_from_values(task, values)
            if not human_label:
                continue
            rows.append({
                task.id_field: record_id,
                "human_label": human_label,
                "source": "argilla",
                "user_id": str(getattr(response, "user_id", "")),
                "status": str(getattr(response, "status", "")),
            })
    write_jsonl(rows, output_path)
    return {
        "backend": "argilla",
        "workspace": workspace,
        "dataset": dataset_name,
        "responses": len(rows),
        "artifact": str(output_path),
    }


def test_connection(params: dict[str, Any] | None = None) -> dict:
    params = params or {}
    client = _client(params.get("api_url"), params.get("api_key"))
    workspace = params.get("workspace") or os.environ.get("ARGILLA_WORKSPACE") or "argilla"
    user = client.me
    workspaces = [_workspace_name(item) for item in client.workspaces.list()]
    return {
        "ok": True,
        "api_url": _api_url(params.get("api_url")),
        "workspace": workspace,
        "workspace_exists": workspace in workspaces,
        "workspaces": workspaces,
        "user": {
            "username": str(getattr(user, "username", "")),
            "role": str(getattr(getattr(user, "role", ""), "value", getattr(user, "role", ""))),
        },
    }
