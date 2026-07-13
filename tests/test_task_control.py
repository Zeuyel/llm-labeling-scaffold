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


def _fingerprint(runs_root: Path, task_id: str) -> str:
    return task_control.get_task_record(runs_root, task_id)["draft_fingerprint"]


def _publish(
    runs_root: Path,
    tasks_root: Path,
    task_id: str,
    key: str,
    *,
    actor: str = "panel",
) -> dict:
    return task_control.publish_task(
        runs_root,
        tasks_root,
        task_id,
        expected_draft_fingerprint=_fingerprint(runs_root, task_id),
        idempotency_key=key,
        actor=actor,
    )


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
    updated = task_control.update_draft(
        runs_root,
        spec["task_id"],
        updated_spec,
        expected_draft_fingerprint=created["record"]["draft_fingerprint"],
        actor="editor",
    )

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

    first = _publish(runs_root, tasks_root, spec["task_id"], "publish-001", actor="publisher")

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

    second = _publish(runs_root, tasks_root, spec["task_id"], "publish-002", actor="publisher")

    second_snapshot = first_snapshot.parent / "revision_000002"
    record = task_control.get_task_record(runs_root, spec["task_id"])
    assert second["task"]["revision"] == 2
    assert second["published"]["snapshot_path"] == str(second_snapshot / "task.yaml")
    assert (second_snapshot / "task.yaml").exists()
    assert load_task(task_path).raw["revision"] == 2
    assert load_task(first_snapshot / "task.yaml").raw["revision"] == 1
    assert load_task(second_snapshot / "task.yaml").raw["revision"] == 2
    assert [item["revision"] for item in record["revisions"]] == [1, 2]


def test_publish_keeps_live_prompts_versioned(tmp_path: Path):
    runs_root = tmp_path / "runs"
    tasks_root = tmp_path / "tasks"
    spec = {**_draft_spec(), "prompt": "First prompt"}
    task_control.create_draft(runs_root, tasks_root, spec)
    _publish(runs_root, tasks_root, spec["task_id"], "publish-001")

    updated_spec = {**spec, "prompt": "Second prompt"}
    task_control.update_draft(
        runs_root,
        spec["task_id"],
        updated_spec,
        expected_draft_fingerprint=_fingerprint(runs_root, spec["task_id"]),
    )
    _publish(runs_root, tasks_root, spec["task_id"], "publish-002")

    task_dir = tasks_root / spec["task_id"]
    live_raw = load_task(task_dir / "task.yaml").raw
    first_prompt = task_dir / "prompt.revision_000001.md"
    second_prompt = task_dir / "prompt.revision_000002.md"
    assert live_raw["prompt"] == second_prompt.name
    assert first_prompt.read_text(encoding="utf-8") == "First prompt\n"
    assert second_prompt.read_text(encoding="utf-8") == "Second prompt\n"


def test_publish_idempotency_reuses_the_same_revision(tmp_path: Path):
    runs_root = tmp_path / "runs"
    tasks_root = tmp_path / "tasks"
    spec = _draft_spec()
    task_control.create_draft(runs_root, tasks_root, spec)

    fingerprint = _fingerprint(runs_root, spec["task_id"])
    first = task_control.publish_task(
        runs_root,
        tasks_root,
        spec["task_id"],
        expected_draft_fingerprint=fingerprint,
        idempotency_key="publish-001",
    )
    second = task_control.publish_task(
        runs_root,
        tasks_root,
        spec["task_id"],
        expected_draft_fingerprint=fingerprint,
        idempotency_key="publish-001",
    )

    assert first["idempotent"] is False
    assert second["idempotent"] is True
    assert second["published"]["revision"] == 1
    assert task_control.get_task_record(runs_root, spec["task_id"])["revision"] == 1

    updated_spec = {**spec, "annotation_guidelines": "Updated draft"}
    task_control.update_draft(
        runs_root,
        spec["task_id"],
        updated_spec,
        expected_draft_fingerprint=fingerprint,
    )
    with pytest.raises(ValueError, match="不同任务草稿"):
        task_control.publish_task(
            runs_root,
            tasks_root,
            spec["task_id"],
            expected_draft_fingerprint=_fingerprint(runs_root, spec["task_id"]),
            idempotency_key="publish-001",
        )


def test_publish_requires_complete_data_lake_configuration(tmp_path: Path):
    runs_root = tmp_path / "runs"
    tasks_root = tmp_path / "tasks"
    spec = _draft_spec()
    spec["data_lake"].pop("output_base_uri")
    task_control.create_draft(runs_root, tasks_root, spec)

    with pytest.raises(ValueError, match="output_base_uri"):
        _publish(runs_root, tasks_root, spec["task_id"], "publish-invalid")

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
        _publish(runs_root, tasks_root, spec["task_id"], "publish-001")

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
    retried = _publish(runs_root, tasks_root, spec["task_id"], "publish-001")
    assert retried["task"]["revision"] == 1
    assert load_task(task_path).raw["revision"] == 1
