from __future__ import annotations

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


def _label_values(label: dict[str, Any]) -> list[str]:
    values = label.get("values", [])
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


def _record_source_id(record, task: TaskConfig) -> str:
    metadata = getattr(record, "metadata", None) or {}
    if isinstance(metadata, dict) and task.id_field in metadata:
        return str(metadata[task.id_field])
    return str(record.id)


def push_sample(task: TaskConfig, sample_path: str | Path, dataset_name: str, params: dict[str, Any] | None = None) -> dict:
    params = params or {}
    rg = _load_argilla()
    client = _client(params.get("api_url"), params.get("api_key"))
    workspace = params.get("workspace") or os.environ.get("ARGILLA_WORKSPACE") or "argilla"
    text_field = params.get("text_field", "text")
    min_submitted = int(params.get("min_submitted", 1))

    settings = rg.Settings(
        guidelines=params.get("guidelines", f"Label records for task {task.task_id}."),
        fields=[rg.TextField(name=text_field, title="Text")],
        questions=_questions_for_task(rg, task),
        distribution=rg.TaskDistribution(min_submitted=min_submitted),
        allow_extra_metadata=True,
    )
    dataset = rg.Dataset(name=dataset_name, workspace=workspace, settings=settings)
    dataset.create()

    records = []
    for row in read_jsonl(sample_path):
        records.append(
            rg.Record(
                id=str(row[task.id_field]),
                fields={text_field: build_text(row, task)},
                metadata={
                    task.id_field: str(row[task.id_field]),
                    "task_id": task.task_id,
                    "sample_path": str(sample_path),
                    **{field: row[field] for field in task.metadata_fields if field in row},
                },
            )
        )
    dataset.records.log(records)
    return {
        "backend": "argilla",
        "workspace": workspace,
        "dataset": dataset_name,
        "records": len(records),
        "url": params.get("ui_url"),
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
