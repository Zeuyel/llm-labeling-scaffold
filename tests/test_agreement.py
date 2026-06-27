from __future__ import annotations

from pathlib import Path
import time

from llm_labeling_scaffold import pipeline
from llm_labeling_scaffold.config import load_task
from llm_labeling_scaffold.io import read_json, write_jsonl


def _make_task(tmp_path: Path, task_id: str = "agreement_task") -> dict:
    return pipeline.create_task(
        tmp_path / "tasks",
        {
            "task_id": task_id,
            "id_field": "record_id",
            "text_fields": ["title"],
            "primary_label_name": "label",
            "primary_label_values": ["yes", "no"],
        },
    )


def _wait_job(runs_root: Path, task_id: str, job_id: str) -> dict:
    current = None
    for _ in range(80):
        current = next((item for item in pipeline.jobs_for_task(runs_root, task_id) if item["id"] == job_id), None)
        if current and current["status"] in {"succeeded", "failed"}:
            return current
        time.sleep(0.02)
    raise AssertionError(f"job did not finish: {job_id} current={current}")


def test_agreement_audit_action_writes_summary_and_reuses_same_inputs(tmp_path: Path):
    created = _make_task(tmp_path)
    runs_root = tmp_path / "runs"
    task = load_task(created["path"])
    sample_path = runs_root / task.task_id / "samples" / "sample_a" / "sample.jsonl"
    decisions_path = runs_root / task.task_id / "decisions" / "round_1" / "decisions.jsonl"
    write_jsonl(
        [
            {"record_id": "r1", "title": "Alpha"},
            {"record_id": "r2", "title": "Beta"},
        ],
        sample_path,
    )
    write_jsonl(
        [
            {"record_id": "r1", "human_label": {"label": "yes"}, "user_id": "u1"},
            {"record_id": "r2", "human_label": {"label": "no"}, "user_id": "u1"},
        ],
        decisions_path,
    )

    job = pipeline.start_action(
        runs_root,
        created["path"],
        "agreement_audit",
        {
            "sample": str(sample_path),
            "decisions": str(decisions_path),
            "audit_id": "round_1",
            "min_submitted": 1,
        },
    )
    finished = _wait_job(runs_root, task.task_id, job["id"])

    assert finished["status"] == "succeeded"
    assert finished["result"]["action"] == "created"
    summary_path = runs_root / task.task_id / "agreement_audits" / "round_1" / "summary.json"
    summary = read_json(summary_path)
    assert summary["passed"] is True
    assert summary["sample_coverage"]["coverage_rate"] == 1.0
    assert summary["unknown_ids"] == []
    assert summary["below_min_submitted_ids"] == []
    assert summary["label_distribution"] == {"no": 1, "yes": 1}

    status = pipeline.task_profile_status(runs_root, task)
    stage = next(item for item in status["stages"] if item["id"] == "agreement_audit")
    assert stage["status"] == "done"
    assert stage["evidence"] == [str(summary_path)]

    reused_job = pipeline.start_action(
        runs_root,
        created["path"],
        "agreement_audit",
        {
            "sample": str(sample_path),
            "decisions": str(decisions_path),
            "audit_id": "round_1",
            "min_submitted": 1,
        },
    )
    reused = _wait_job(runs_root, task.task_id, reused_job["id"])
    assert reused["status"] == "succeeded"
    assert reused["result"]["action"] == "reused"
    assert reused["result"]["idempotent"] is True

    conflict_job = pipeline.start_action(
        runs_root,
        created["path"],
        "agreement_audit",
        {
            "sample": str(sample_path),
            "decisions": str(decisions_path),
            "audit_id": "round_1",
            "min_submitted": 2,
        },
    )
    conflict = _wait_job(runs_root, task.task_id, conflict_job["id"])
    assert conflict["status"] == "failed"
    assert "输入或参数不同" in conflict["error"]


def test_agreement_audit_writes_failed_quality_summary_without_argilla(tmp_path: Path):
    created = _make_task(tmp_path, "agreement_fail_task")
    runs_root = tmp_path / "runs"
    task = load_task(created["path"])
    sample_path = runs_root / task.task_id / "samples" / "sample_a" / "sample.jsonl"
    decisions_path = runs_root / task.task_id / "decisions" / "round_bad" / "decisions.jsonl"
    write_jsonl(
        [
            {"record_id": "r1", "title": "Alpha"},
            {"record_id": "r2", "title": "Beta"},
            {"record_id": "r3", "title": "Gamma"},
        ],
        sample_path,
    )
    write_jsonl(
        [
            {"record_id": "r1", "human_label": {"label": "yes"}, "user_id": "u1"},
            {"record_id": "r1", "human_label": {"label": "no"}, "user_id": "u1"},
            {"record_id": "r2", "human_label": {"note": "missing primary"}, "user_id": "u2"},
            {"record_id": "r404", "human_label": {"label": "yes"}, "user_id": "u3"},
            {"human_label": {"label": "no"}, "user_id": "u4"},
        ],
        decisions_path,
    )

    job = pipeline.start_action(
        runs_root,
        created["path"],
        "agreement_audit",
        {
            "sample": str(sample_path),
            "decisions": str(decisions_path),
            "audit_id": "round_bad",
            "min_submitted": 2,
        },
    )
    finished = _wait_job(runs_root, task.task_id, job["id"])

    assert finished["status"] == "succeeded"
    summary = read_json(runs_root / task.task_id / "agreement_audits" / "round_bad" / "summary.json")
    assert summary["passed"] is False
    assert summary["sample_coverage"]["missing_ids"] == ["r3"]
    assert summary["unknown_ids"] == ["r404"]
    assert summary["decision_missing_id_rows"] == [5]
    assert summary["duplicate_submissions"] == [{"id": "r1", "submitter": "u1", "rows": [1, 2]}]
    assert summary["primary_label_missing_ids"] == ["r2"]
    assert set(summary["below_min_submitted_ids"]) == {"r1", "r2", "r3"}
    assert summary["label_distribution"] == {"yes": 1}
    assert summary["issue_counts"]["duplicate_submissions"] == 1

    status = pipeline.task_profile_status(runs_root, task)
    stage = next(item for item in status["stages"] if item["id"] == "agreement_audit")
    assert stage["status"] == "blocked"
    assert "summary.json" in stage["evidence"][0]
