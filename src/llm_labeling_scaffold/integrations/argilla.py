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


def push_sample(task: TaskConfig, sample_path: str | Path, dataset_name: str, params: dict[str, Any] | None = None) -> dict:
    params = params or {}
    rg = _load_argilla()
    client = _client(params.get("api_url"), params.get("api_key"))
    workspace = params.get("workspace") or os.environ.get("ARGILLA_WORKSPACE") or "argilla"
    text_field = params.get("text_field", "text")
    question = task.primary_label["name"]
    labels = list(task.primary_label.get("values", []))
    min_submitted = int(params.get("min_submitted", 1))

    settings = rg.Settings(
        guidelines=params.get("guidelines", f"Label records for task {task.task_id}."),
        fields=[rg.TextField(name=text_field, title="Text")],
        questions=[rg.LabelQuestion(name=question, title=question, labels=labels)],
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
    question = task.primary_label["name"]
    dataset = client.datasets(dataset_name, workspace=workspace)
    rows = []
    for record in dataset.records:
        record_id = str(record.id)
        for response in getattr(record, "responses", []) or []:
            values = getattr(response, "values", {}) or {}
            value = values.get(question)
            if isinstance(value, dict):
                value = value.get("value")
            if value is None:
                continue
            rows.append({
                task.id_field: record_id,
                "human_label": {question: value},
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
