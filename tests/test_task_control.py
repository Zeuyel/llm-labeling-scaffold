from __future__ import annotations

from pathlib import Path

import pytest

from llm_labeling_scaffold import task_control
from llm_labeling_scaffold.config import load_task
from llm_labeling_scaffold.io import read_json


def _draft_spec(task_id: str = "control_task") -> dict:
    return {
        "task_id": task_id,
        "id_field": "record_id",
        "text_fields": ["title"],
        "primary_label_name": "decision",
        "primary_label_values": ["accept", "reject"],
        "data_lake": {
            "source_dataset_id": "draft_dataset",
            "source_object_path": "inputs/draft.jsonl",
            "default_import_id": "draft_import",
            "output_base_uri": "outputs/control_task",
        },
    }


def test_create_and_update_draft_do_not_write_task_yaml(tmp_path: Path):
    runs_root = tmp_path / "runs"
    tasks_root = tmp_path / "tasks"
    spec = _draft_spec()

    created = task_control.create_draft(runs_root, tasks_root, spec, actor="tester")

    task_dir = tasks_root / spec["task_id"]
    assert created["task"]["status"] == "draft"
    assert not task_dir.exists()

    updated_spec = {
        **spec,
        "primary_label_values": ["accept", "reject", "review"],
        "annotation_guidelines": "Review the title before assigning a decision.",
    }
    updated = task_control.update_draft(runs_root, spec["task_id"], updated_spec, actor="editor")

    record = task_control.get_task_record(runs_root, spec["task_id"])
    assert updated["action"] == "updated"
    assert updated["task"]["status"] == "draft"
    assert updated["task"]["revision"] == 0
    assert record["draft_spec"] == updated_spec
    assert record["updated_by"] == "editor"
    assert not (task_dir / "task.yaml").exists()


def test_publish_writes_revision_snapshots_and_increments_revision(tmp_path: Path):
    runs_root = tmp_path / "runs"
    tasks_root = tmp_path / "tasks"
    spec = _draft_spec()
    task_control.create_draft(runs_root, tasks_root, spec)

    first = task_control.publish_task(runs_root, tasks_root, spec["task_id"], actor="publisher")

    task_path = tasks_root / spec["task_id"] / "task.yaml"
    first_snapshot = (
        runs_root
        / "_system"
        / "task_control"
        / "task_snapshots"
        / spec["task_id"]
        / "revision_000001"
    )
    assert first["task"]["revision"] == 1
    assert task_path.exists()
    assert first["published"]["snapshot_path"] == str(first_snapshot / "task.yaml")
    assert (first_snapshot / "task.yaml").exists()
    assert read_json(first_snapshot / "draft_spec.json") == spec
    assert load_task(task_path).raw["revision"] == 1
    assert load_task(first_snapshot / "task.yaml").raw["revision"] == 1

    second = task_control.publish_task(runs_root, tasks_root, spec["task_id"], actor="publisher")

    second_snapshot = first_snapshot.parent / "revision_000002"
    record = task_control.get_task_record(runs_root, spec["task_id"])
    assert second["task"]["revision"] == 2
    assert second["published"]["snapshot_path"] == str(second_snapshot / "task.yaml")
    assert (second_snapshot / "task.yaml").exists()
    assert load_task(task_path).raw["revision"] == 2
    assert load_task(first_snapshot / "task.yaml").raw["revision"] == 1
    assert load_task(second_snapshot / "task.yaml").raw["revision"] == 2
    assert [item["revision"] for item in record["revisions"]] == [1, 2]


def test_publish_requires_complete_data_lake_configuration(tmp_path: Path):
    runs_root = tmp_path / "runs"
    tasks_root = tmp_path / "tasks"
    spec = _draft_spec()
    spec["data_lake"].pop("output_base_uri")
    task_control.create_draft(runs_root, tasks_root, spec)

    with pytest.raises(ValueError, match="output_base_uri"):
        task_control.publish_task(runs_root, tasks_root, spec["task_id"])

    record = task_control.get_task_record(runs_root, spec["task_id"])
    assert record["revision"] == 0
    assert not (tasks_root / spec["task_id"] / "task.yaml").exists()


def test_publish_keeps_live_task_unchanged_when_promotion_fails(tmp_path: Path, monkeypatch):
    runs_root = tmp_path / "runs"
    tasks_root = tmp_path / "tasks"
    spec = _draft_spec()
    task_control.create_draft(runs_root, tasks_root, spec)
    task_path = tasks_root / spec["task_id"] / "task.yaml"
    original_write = task_control.write_text_atomic

    def fail_live_task_write(text: str, path: str | Path) -> None:
        if Path(path) == task_path:
            raise OSError("live task write failed")
        original_write(text, path)

    monkeypatch.setattr(task_control, "write_text_atomic", fail_live_task_write)
    with pytest.raises(OSError, match="live task write failed"):
        task_control.publish_task(runs_root, tasks_root, spec["task_id"])

    snapshot = (
        runs_root
        / "_system"
        / "task_control"
        / "task_snapshots"
        / spec["task_id"]
        / "revision_000001"
        / "task.yaml"
    )
    assert snapshot.exists()
    assert not task_path.exists()
    assert task_control.get_task_record(runs_root, spec["task_id"])["revision"] == 0

    monkeypatch.setattr(task_control, "write_text_atomic", original_write)
    retried = task_control.publish_task(runs_root, tasks_root, spec["task_id"])
    assert retried["task"]["revision"] == 1
    assert load_task(task_path).raw["revision"] == 1
