from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
from typing import Any
import urllib.request
from uuid import UUID

from ..config import TaskConfig, build_text
from ..io import read_json, read_jsonl, write_jsonl


_ARGILLA_CONTRACT_SCHEMA_VERSION = 1
_ARGILLA_PUSH_FINGERPRINT_FIELD = "__lls_push_fingerprint"
_ARGILLA_FINGERPRINT_NAMES = ("task", "sample", "batch", "plan")
_ARGILLA_VERSION_PATTERN = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?")


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


def _require_argilla_2_8_version(component: str, value: Any) -> str:
    version = str(value or "").strip()
    match = _ARGILLA_VERSION_PATTERN.match(version)
    if not match or (int(match.group(1)), int(match.group(2))) != (2, 8):
        raise RuntimeError(f"{component} 必须是 Argilla 2.8.x，实际版本: {version or '<missing>'}")
    return version


def _server_version(api_url: str, timeout: float = 5.0) -> str:
    url = f"{api_url.rstrip('/')}/api/v1/version"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except Exception as exc:
        raise RuntimeError("无法读取 Argilla server version endpoint") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Argilla server version endpoint 返回了非 JSON object")
    return _require_argilla_2_8_version("Argilla server", payload.get("version"))


def _runtime_versions(rg, api_url: str) -> dict[str, str]:
    return {
        "sdk": _require_argilla_2_8_version("Argilla SDK", getattr(rg, "__version__", None)),
        "server": _server_version(api_url),
    }


def _stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _jsonl_fingerprint(path: str | Path) -> str:
    digest = hashlib.sha256()
    for row in read_jsonl(path):
        digest.update(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _task_fingerprint(task: TaskConfig) -> str:
    task_definition = dict(task.raw)
    task_definition.pop("runs_dir", None)
    return _stable_hash(task_definition)


def _push_fingerprints(task: TaskConfig, sample_path: str | Path, params: dict[str, Any]) -> dict[str, str]:
    source_sample = Path(str(params.get("sample_path") or sample_path))
    if not source_sample.is_file():
        raise ValueError(f"Argilla sample fingerprint 文件不存在: {source_sample}")
    sample_fingerprint = _jsonl_fingerprint(source_sample)

    batch_files = [Path(value) for value in _string_list(params.get("batch_files"))]
    batch_ids = _string_list(params.get("batch_ids"))
    if not batch_files:
        batch_files = [Path(sample_path)]
        batch_ids = [Path(sample_path).name]
    batch_entries: list[dict[str, str]] = []
    for index, batch_file in enumerate(batch_files):
        if not batch_file.is_file():
            raise ValueError(f"Argilla batch fingerprint 文件不存在: {batch_file}")
        batch_id = batch_ids[index] if index < len(batch_ids) else batch_file.name
        batch_entries.append({"batch_id": batch_id, "sha256": _jsonl_fingerprint(batch_file)})
    batch_entries.sort(key=lambda item: (item["batch_id"], item["sha256"]))
    batch_fingerprint = _stable_hash({"dispatch_mode": params.get("dispatch_mode") or "sample", "batches": batch_entries})

    plan_manifest_path = str(params.get("batch_manifest_path") or "").strip()
    if plan_manifest_path:
        plan_path = Path(plan_manifest_path)
        if not plan_path.is_file():
            raise ValueError(f"Argilla plan fingerprint manifest 不存在: {plan_path}")
        plan_manifest = dict(read_json(plan_path))
        plan_manifest.pop("sample", None)
    else:
        plan_manifest = {
            "dispatch_mode": params.get("dispatch_mode") or "sample",
            "batch_plan_id": params.get("batch_plan_id"),
        }
    plan_fingerprint = _stable_hash({
        "manifest": plan_manifest,
        "sample_fingerprint": sample_fingerprint,
        "batch_fingerprint": batch_fingerprint,
        "selected_batch_ids": batch_ids,
    })
    return {
        "task": _task_fingerprint(task),
        "sample": sample_fingerprint,
        "batch": batch_fingerprint,
        "plan": plan_fingerprint,
    }


def _push_fingerprint(
    *,
    versions: dict[str, str],
    workspace_name: str,
    dataset_name: str,
    min_submitted: int,
    record_id_policy: dict[str, Any],
    fingerprints: dict[str, str],
) -> str:
    return _stable_hash({
        "schema_version": _ARGILLA_CONTRACT_SCHEMA_VERSION,
        "versions": versions,
        "workspace_name": workspace_name,
        "dataset_name": dataset_name,
        "min_submitted": min_submitted,
        "record_id_policy": record_id_policy,
        "fingerprints": fingerprints,
    })


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
        converted = _argilla_suggestion_value(label, _response_value(value))
        if converted in (None, ""):
            continue
        out[name] = converted
    return out


def _argilla_suggestion_value(label: dict[str, Any], value: Any) -> Any:
    if value in (None, ""):
        return None
    casted = _cast_value(label, value)
    label_type = label.get("type", "string")
    if label_type == "integer" and "values" in label:
        return str(casted)
    if label_type == "boolean":
        return "true" if casted else "false"
    return casted


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


def _resource_uuid(resource: Any, label: str) -> str:
    value = getattr(resource, "id", None)
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise RuntimeError(f"Argilla {label} 缺少有效 UUID") from exc


def _contract_uuid(value: Any, label: str) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"Argilla manifest 缺少有效 {label}") from exc


def _validated_contract(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Argilla manifest 缺少 argilla_contract")
    if value.get("schema_version") != _ARGILLA_CONTRACT_SCHEMA_VERSION:
        raise ValueError("Argilla manifest contract schema_version 不兼容")
    workspace = value.get("workspace")
    dataset = value.get("dataset")
    fingerprints = value.get("fingerprints")
    if not isinstance(workspace, dict) or not isinstance(dataset, dict) or not isinstance(fingerprints, dict):
        raise ValueError("Argilla manifest identity/fingerprint 字段不完整")
    normalized_fingerprints: dict[str, str] = {}
    for name in (*_ARGILLA_FINGERPRINT_NAMES, "push"):
        fingerprint = str(fingerprints.get(name) or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise ValueError(f"Argilla manifest 缺少有效 {name} fingerprint")
        normalized_fingerprints[name] = fingerprint
    workspace_name = str(workspace.get("name") or "").strip()
    dataset_name = str(dataset.get("name") or "").strip()
    if not workspace_name or not dataset_name:
        raise ValueError("Argilla manifest workspace/dataset name 不完整")
    try:
        min_submitted = int(value.get("min_submitted"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Argilla manifest min_submitted 无效") from exc
    if min_submitted < 1:
        raise ValueError("Argilla manifest min_submitted 必须大于 0")
    return {
        "schema_version": _ARGILLA_CONTRACT_SCHEMA_VERSION,
        "server_version": _require_argilla_2_8_version("Argilla manifest server", value.get("server_version")),
        "sdk_version": _require_argilla_2_8_version("Argilla manifest SDK", value.get("sdk_version")),
        "workspace": {
            "uuid": _contract_uuid(workspace.get("uuid"), "workspace UUID"),
            "name": workspace_name,
        },
        "dataset": {
            "uuid": _contract_uuid(dataset.get("uuid"), "dataset UUID"),
            "name": dataset_name,
        },
        "min_submitted": min_submitted,
        "fingerprints": normalized_fingerprints,
    }


def _workspace_by_identity(client, name: str, expected_uuid: str | None = None):
    workspace = client.workspaces(id=expected_uuid) if expected_uuid else client.workspaces(name=name)
    if workspace is None:
        if expected_uuid:
            raise ValueError("Argilla workspace UUID 在当前环境不存在；拒绝按同名 workspace 回退")
        raise ValueError(f"Argilla workspace 不存在: {name}")
    actual_uuid = _resource_uuid(workspace, "workspace")
    actual_name = _workspace_name(workspace)
    if actual_name != name or (expected_uuid and actual_uuid != expected_uuid):
        raise ValueError("Argilla workspace 身份与 manifest 不一致")
    return workspace


def _dataset_by_identity(client, name: str, workspace, expected_uuid: str | None = None):
    dataset = client.datasets(id=expected_uuid) if expected_uuid else client.datasets(name=name, workspace=workspace)
    if dataset is None:
        if expected_uuid:
            raise ValueError("Argilla dataset UUID 在当前环境不存在；拒绝按同名 dataset 回退")
        return None
    actual_uuid = _resource_uuid(dataset, "dataset")
    dataset_workspace = getattr(dataset, "workspace", None)
    actual_workspace_uuid = _resource_uuid(dataset_workspace, "dataset workspace")
    if (
        str(getattr(dataset, "name", "")) != name
        or actual_workspace_uuid != _resource_uuid(workspace, "workspace")
        or (expected_uuid and actual_uuid != expected_uuid)
    ):
        raise ValueError("Argilla dataset 身份与 workspace/manifest 不一致")
    return dataset


def _record_metadata_dict(record: Any) -> dict[str, Any]:
    metadata = getattr(record, "metadata", None)
    if isinstance(metadata, dict):
        return dict(metadata)
    if hasattr(metadata, "to_dict"):
        value = metadata.to_dict()
        return dict(value) if isinstance(value, dict) else {}
    return {}


def _collection_has_items(value: Any) -> bool:
    if value is None:
        return False
    try:
        return len(value) > 0
    except TypeError:
        return any(True for _ in value)


def _remote_dataset_state(dataset) -> dict[str, Any]:
    record_ids: set[str] = set()
    fingerprints: set[str] = set()
    missing_fingerprint = False
    has_responses = False
    for record in dataset.records:
        record_ids.add(str(getattr(record, "id", "")))
        fingerprint = str(_record_metadata_dict(record).get(_ARGILLA_PUSH_FINGERPRINT_FIELD) or "").strip()
        if fingerprint:
            fingerprints.add(fingerprint)
        else:
            missing_fingerprint = True
        if _collection_has_items(getattr(record, "responses", None)):
            has_responses = True
    return {
        "record_ids": record_ids,
        "fingerprints": fingerprints,
        "missing_fingerprint": missing_fingerprint,
        "has_responses": has_responses,
    }


def _dataset_min_submitted(dataset) -> int:
    distribution = getattr(dataset, "distribution", None)
    value = getattr(distribution, "min_submitted", None)
    try:
        resolved = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Argilla dataset 缺少公开 distribution.min_submitted") from exc
    return resolved


def _validate_dataset_resume(
    existing,
    *,
    push_fingerprint: str,
    desired_record_ids: set[str],
    min_submitted: int,
) -> None:
    state = _remote_dataset_state(existing)
    if _dataset_min_submitted(existing) != min_submitted:
        raise ValueError("Argilla dataset min_submitted 与当前 push contract 不一致")
    if state["missing_fingerprint"] or state["fingerprints"] != {push_fingerprint}:
        raise ValueError("Argilla dataset fingerprint 与当前 push contract 不一致，禁止 append/resume 混入")
    unexpected_ids = state["record_ids"] - desired_record_ids
    if unexpected_ids:
        raise ValueError("Argilla dataset 包含当前 push contract 之外的 record id，拒绝幂等恢复")


def _prepare_dataset(
    dataset,
    existing,
    if_exists: str,
    *,
    push_fingerprint: str,
    desired_record_ids: set[str],
    min_submitted: int,
):
    policy = str(if_exists or "resume").strip().lower()
    if policy not in {"fail", "resume", "append", "replace"}:
        raise ValueError("Argilla 同名数据集策略只能是 fail、resume、append 或 replace")
    if existing is None:
        return dataset.create(), "created"
    if policy == "fail":
        raise ValueError(f"Argilla 数据集已存在: {_workspace_name(existing.workspace)}/{existing.name}")

    if policy in {"resume", "append"}:
        _validate_dataset_resume(
            existing,
            push_fingerprint=push_fingerprint,
            desired_record_ids=desired_record_ids,
            min_submitted=min_submitted,
        )
        return existing, "resumed"

    state = _remote_dataset_state(existing)
    if state["has_responses"]:
        raise ValueError("Argilla dataset 已有回答，禁止 replace")
    existing.delete()
    return dataset.create(), "replaced"


def _build_contract(
    *,
    versions: dict[str, str],
    workspace,
    dataset,
    min_submitted: int,
    fingerprints: dict[str, str],
    push_fingerprint: str,
) -> dict[str, Any]:
    return {
        "schema_version": _ARGILLA_CONTRACT_SCHEMA_VERSION,
        "server_version": versions["server"],
        "sdk_version": versions["sdk"],
        "workspace": {
            "uuid": _resource_uuid(workspace, "workspace"),
            "name": _workspace_name(workspace),
        },
        "dataset": {
            "uuid": _resource_uuid(dataset, "dataset"),
            "name": str(getattr(dataset, "name", "")),
        },
        "min_submitted": min_submitted,
        "fingerprints": {**fingerprints, "push": push_fingerprint},
    }


def _validate_expected_push_contract(
    value: Any,
    *,
    versions: dict[str, str],
    workspace_name: str,
    dataset_name: str,
    min_submitted: int,
    fingerprints: dict[str, str],
    push_fingerprint: str,
) -> dict[str, Any] | None:
    if value is None:
        return None
    contract = _validated_contract(value)
    expected = {
        "server_version": versions["server"],
        "sdk_version": versions["sdk"],
        "workspace_name": workspace_name,
        "dataset_name": dataset_name,
        "min_submitted": min_submitted,
        "fingerprints": {**fingerprints, "push": push_fingerprint},
    }
    actual = {
        "server_version": contract["server_version"],
        "sdk_version": contract["sdk_version"],
        "workspace_name": contract["workspace"]["name"],
        "dataset_name": contract["dataset"]["name"],
        "min_submitted": contract["min_submitted"],
        "fingerprints": contract["fingerprints"],
    }
    if actual != expected:
        raise ValueError("Argilla 既有 push manifest 与当前 task/sample/batch/plan contract 不一致")
    return contract


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
    push_fingerprint = params.get("_push_fingerprint")
    if push_fingerprint:
        metadata[_ARGILLA_PUSH_FINGERPRINT_FIELD] = str(push_fingerprint)
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
    params = dict(params or {})
    rg = _load_argilla()
    api_url = _api_url(params.get("api_url"))
    versions = _runtime_versions(rg, api_url)
    workspace_name = str(params.get("workspace") or os.environ.get("ARGILLA_WORKSPACE") or "argilla")
    text_field = params.get("text_field", "text")
    min_submitted = int(params.get("min_submitted", 1))
    if min_submitted < 1:
        raise ValueError("Argilla min_submitted 必须大于 0")
    if_exists = str(params.get("if_exists") or params.get("dataset_policy") or "resume")
    fingerprints = _push_fingerprints(task, sample_path, params)
    requested_record_id_policy = _record_id_policy(task, params)
    push_fingerprint = _push_fingerprint(
        versions=versions,
        workspace_name=workspace_name,
        dataset_name=dataset_name,
        min_submitted=min_submitted,
        record_id_policy=requested_record_id_policy,
        fingerprints=fingerprints,
    )
    expected_contract = _validate_expected_push_contract(
        params.get("expected_contract"),
        versions=versions,
        workspace_name=workspace_name,
        dataset_name=dataset_name,
        min_submitted=min_submitted,
        fingerprints=fingerprints,
        push_fingerprint=push_fingerprint,
    )
    client = _client(params.get("api_url"), params.get("api_key"))
    workspace = _workspace_by_identity(
        client,
        workspace_name,
        expected_contract["workspace"]["uuid"] if expected_contract else None,
    )
    existing = _dataset_by_identity(
        client,
        dataset_name,
        workspace,
        expected_contract["dataset"]["uuid"] if expected_contract else None,
    )
    params["_push_fingerprint"] = push_fingerprint
    records, record_id_policy, duplicate_record_ids = _prepare_records_for_push(
        rg,
        task,
        sample_path,
        text_field,
        params,
    )

    settings = rg.Settings(
        guidelines=_guidelines_for_task(task, params),
        fields=_argilla_text_fields(rg, task, text_field, params),
        questions=_questions_for_task(rg, task),
        distribution=rg.TaskDistribution(min_submitted=min_submitted),
        allow_extra_metadata=True,
    )
    dataset = rg.Dataset(name=dataset_name, workspace=workspace, settings=settings, client=client)
    dataset, dataset_action = _prepare_dataset(
        dataset,
        existing,
        if_exists,
        push_fingerprint=push_fingerprint,
        desired_record_ids={str(record.id) for record in records},
        min_submitted=min_submitted,
    )
    contract = _build_contract(
        versions=versions,
        workspace=workspace,
        dataset=dataset,
        min_submitted=min_submitted,
        fingerprints=fingerprints,
        push_fingerprint=push_fingerprint,
    )

    dataset.records.log(records)
    return {
        "backend": "argilla",
        "workspace": workspace_name,
        "workspace_uuid": contract["workspace"]["uuid"],
        "dataset": dataset_name,
        "dataset_uuid": contract["dataset"]["uuid"],
        "dataset_action": dataset_action,
        "if_exists": if_exists,
        "min_submitted": min_submitted,
        "records": len(records),
        "suggestions": _record_suggestion_count(records),
        "record_id_policy": record_id_policy,
        "duplicate_record_ids": duplicate_record_ids,
        "visible_fields": list(records[0].fields.keys()) if records else [text_field],
        "contract": contract,
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
    api_url = _api_url(params.get("api_url"))
    versions = _runtime_versions(rg, api_url)
    expected_value = params.get("expected_contract")
    if expected_value is None:
        raise ValueError("Argilla suggestion push 缺少 annotation manifest identity contract")
    expected = _validated_contract(expected_value)
    workspace_name = str(params.get("workspace") or expected["workspace"]["name"])
    if workspace_name != expected["workspace"]["name"] or dataset_name != expected["dataset"]["name"]:
        raise ValueError("Argilla suggestion push 的 workspace/dataset 与 manifest 不一致")
    text_field = params.get("text_field", "text")
    fingerprints = _push_fingerprints(task, sample_path, params)
    record_id_policy = _record_id_policy(task, params)
    push_fingerprint = _push_fingerprint(
        versions=versions,
        workspace_name=workspace_name,
        dataset_name=dataset_name,
        min_submitted=expected["min_submitted"],
        record_id_policy=record_id_policy,
        fingerprints=fingerprints,
    )
    expected = _validate_expected_push_contract(
        expected,
        versions=versions,
        workspace_name=workspace_name,
        dataset_name=dataset_name,
        min_submitted=expected["min_submitted"],
        fingerprints=fingerprints,
        push_fingerprint=push_fingerprint,
    )
    params["_push_fingerprint"] = push_fingerprint
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
    workspace = _workspace_by_identity(client, workspace_name, expected["workspace"]["uuid"])
    dataset = _dataset_by_identity(client, dataset_name, workspace, expected["dataset"]["uuid"])
    _validate_dataset_resume(
        dataset,
        push_fingerprint=push_fingerprint,
        desired_record_ids={str(record.id) for record in records},
        min_submitted=expected["min_submitted"],
    )
    dataset.records.log(records)
    return {
        "backend": "argilla",
        "workspace": workspace_name,
        "workspace_uuid": expected["workspace"]["uuid"],
        "dataset": dataset_name,
        "dataset_uuid": expected["dataset"]["uuid"],
        "records": len(records),
        "suggestions": suggestion_count,
        "record_id_policy": record_id_policy,
        "duplicate_record_ids": duplicate_record_ids,
        "contract": expected,
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
