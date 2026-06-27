from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import yaml

from llm_labeling_scaffold import pipeline
from llm_labeling_scaffold.data_lake import DataLakeError
from llm_labeling_scaffold.task_registry import TASK_CACHE_METADATA, sync_tasks_from_registry


def _create_task_file(root: Path, registry_path: Path, *, task_id: str = "data_task",
                      source_dataset_id: str = "lake_seed") -> Path:
    created = pipeline.create_task(
        root,
        {
            "task_id": task_id,
            "id_field": "record_id",
            "text_fields": ["title"],
            "primary_label_name": "label",
            "primary_label_values": ["yes", "no"],
            "data_lake": {
                "lake_registry_uri": str(registry_path),
                "source_dataset_id": source_dataset_id,
                "source_object_path": "inputs/manual_seed/v1/raw.jsonl",
            },
        },
    )
    return Path(created["path"])


def test_sync_tasks_from_registry_materializes_active_tasks(tmp_path: Path):
    registry_path = tmp_path / "data_lake.yaml"
    active_task = _create_task_file(tmp_path / "source_tasks", registry_path)
    inactive_task = _create_task_file(tmp_path / "source_tasks", registry_path, task_id="old_task")
    registry_path.write_text(
        yaml.safe_dump(
            {
                "datasets": {},
                "tasks": {
                    "data_task": {
                        "status": "active",
                        "task_uri": str(active_task),
                        "source_dataset_id": "lake_seed",
                    },
                    "old_task": {
                        "status": "inactive",
                        "task_uri": str(inactive_task),
                        "source_dataset_id": "lake_seed",
                    },
                },
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    stale_file = tmp_path / "cache_tasks" / "data_task" / "raw" / "input.jsonl"
    stale_file.parent.mkdir(parents=True)
    stale_file.write_text('{"record_id":"stale"}\n', encoding="utf-8")

    with patch.dict("os.environ", {"LLS_ALLOW_LOCAL_DATA_LAKE_URIS": "1"}):
        synced = sync_tasks_from_registry(tmp_path / "cache_tasks", registry_uri=str(registry_path))

    cached_task = tmp_path / "cache_tasks" / "data_task" / "task.yaml"
    metadata = json.loads((cached_task.parent / TASK_CACHE_METADATA).read_text(encoding="utf-8"))

    assert sorted(synced.tasks) == ["data_task"]
    assert cached_task.exists()
    assert not (tmp_path / "cache_tasks" / "old_task" / "task.yaml").exists()
    assert not stale_file.exists()
    assert metadata["source"] == "r2_data_lake_task_registry"
    assert metadata["task_uri"] == str(active_task)
    assert pipeline.list_tasks(tmp_path / "cache_tasks")[0]["task_id"] == "data_task"


def test_sync_tasks_from_registry_rejects_dataset_mismatch(tmp_path: Path):
    registry_path = tmp_path / "data_lake.yaml"
    task_path = _create_task_file(tmp_path / "source_tasks", registry_path, source_dataset_id="wrong_seed")
    registry_path.write_text(
        yaml.safe_dump(
            {
                "datasets": {},
                "tasks": {
                    "data_task": {
                        "status": "active",
                        "task_uri": str(task_path),
                        "source_dataset_id": "lake_seed",
                    },
                },
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    try:
        with patch.dict("os.environ", {"LLS_ALLOW_LOCAL_DATA_LAKE_URIS": "1"}):
            sync_tasks_from_registry(tmp_path / "cache_tasks", registry_uri=str(registry_path))
    except DataLakeError as exc:
        assert "source_dataset_id 与 registry 不一致" in str(exc)
    else:
        raise AssertionError("task registry sync should reject source dataset mismatches")


def test_sync_tasks_from_registry_requires_tasks_section(tmp_path: Path):
    registry_path = tmp_path / "data_lake.yaml"
    registry_path.write_text(yaml.safe_dump({"datasets": {}}), encoding="utf-8")

    try:
        with patch.dict("os.environ", {"LLS_ALLOW_LOCAL_DATA_LAKE_URIS": "1"}):
            sync_tasks_from_registry(tmp_path / "cache_tasks", registry_uri=str(registry_path))
    except DataLakeError as exc:
        assert "缺少 tasks" in str(exc)
    else:
        raise AssertionError("production registry must include tasks")
