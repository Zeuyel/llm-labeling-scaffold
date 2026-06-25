from __future__ import annotations

from pathlib import Path

import pytest

from llm_labeling_scaffold.config import TaskConfig, load_task
from llm_labeling_scaffold.io import write_json, write_jsonl


@pytest.fixture
def toy_task(tmp_path: Path) -> TaskConfig:
    base = load_task(Path("examples/toy_text_classification/task.yaml"))
    return TaskConfig(path=base.path, raw={**base.raw, "runs_dir": str(tmp_path / "runs")})


@pytest.fixture
def panel_workspace(toy_task: TaskConfig) -> dict:
    runs_root = Path(toy_task.raw["runs_dir"])
    run_dir = toy_task.runs_dir / "demo"

    write_json(
        {
            "audited_batches": 1,
            "clean_batches": 1,
            "usable_batches": 1,
            "blocked_batches": 0,
        },
        run_dir / "audit" / "run_summary.json",
    )
    write_json(
        {
            "batch_name": "batch_000",
            "input_rows": 2,
            "output_rows": 2,
            "missing_count": 0,
            "duplicate_row_count": 0,
            "duplicate_conflict_id_count": 0,
            "schema_error_count": 0,
            "constraint_error_count": 0,
            "is_clean": True,
            "is_usable": True,
        },
        run_dir / "audit" / "batch_000.summary.json",
    )
    write_json(
        {
            "audited_batches": 1,
            "clean_batches": 1,
            "usable_batches": 1,
            "merged_rows": 2,
            "missing_pool_rows": 0,
            "duplicate_pool_rows": 0,
            "conflict_pool_rows": 0,
            "primary_label_field": "class_label",
            "primary_label_counts": {"non_target": 1, "service_upgrade": 1},
        },
        run_dir / "merged" / "merge_summary.json",
    )
    write_jsonl(
        [
            {"record_id": "r001", "class_label": "non_target", "is_target": 0},
            {"record_id": "r002", "class_label": "service_upgrade", "is_target": 1},
        ],
        run_dir / "merged" / "merged_clean.jsonl",
    )
    for pool in ("missing_pool", "duplicate_pool", "conflict_pool"):
        write_jsonl([], run_dir / "merged" / f"{pool}.jsonl")

    gold_dir = toy_task.runs_dir / "gold"
    gold_path = gold_dir / "gold_v001.jsonl"
    write_jsonl(
        [
            {"record_id": "r001", "class_label": "non_target", "is_target": 0},
            {"record_id": "r002", "class_label": "service_upgrade", "is_target": 1},
        ],
        gold_path,
    )
    write_json(
        {
            "task_id": toy_task.task_id,
            "version": "v001",
            "path": str(gold_path),
            "rows": 2,
            "unique_ids": 2,
            "primary_label": "class_label",
            "label_counts": {"non_target": 1, "service_upgrade": 1},
            "source": "run",
        },
        gold_dir / "gold_v001.manifest.json",
    )

    sample_path = toy_task.runs_dir / "samples" / "sample_a" / "sample.jsonl"
    write_jsonl(
        [
            {
                "record_id": "r001",
                "title": "General notice",
                "body": "Annual operating update",
                "year": 2025,
                "firm_id": "f001",
            },
            {
                "record_id": "r002",
                "title": "Service upgrade",
                "body": "New remote service platform",
                "year": 2025,
                "firm_id": "f002",
            },
        ],
        sample_path,
    )
    decision_dir = toy_task.runs_dir / "decisions" / "argilla_round_1"
    decisions_path = decision_dir / "decisions.jsonl"
    write_jsonl(
        [
            {
                "record_id": "r001",
                "human_label": {"class_label": "non_target", "is_target": 0},
                "source": "argilla",
            },
            {
                "record_id": "r002",
                "human_label": {"class_label": "service_upgrade", "is_target": 1},
                "source": "argilla",
            },
        ],
        decisions_path,
    )
    write_json(
        {
            "task_id": toy_task.task_id,
            "decision_id": "argilla_round_1",
            "source": "argilla",
            "argilla_dataset": "toy_argilla_round_1",
            "sample_id": "sample_a",
            "sample_path": str(sample_path),
            "path": str(decisions_path),
            "rows": 2,
        },
        decision_dir / "manifest.json",
    )

    return {
        "runs_root": runs_root,
        "task": toy_task,
        "run_dir": run_dir,
        "sample_path": sample_path,
        "decisions_path": decisions_path,
    }
