from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType

from llm_labeling_scaffold import cli


def test_workflow_parser_has_expected_commands():
    parser = cli.build_parser()

    args = parser.parse_args(["workflow", "start", "--task-id", "demo", "--profile", "manual_labeling_cv_v1"])
    assert args.cmd == "workflow"
    assert args.workflow_cmd == "start"
    assert args.task_id == "demo"
    assert args.profile == "manual_labeling_cv_v1"

    args = parser.parse_args(
        [
            "workflow",
            "resume",
            "--task-id",
            "demo",
            "--workflow-id",
            "wf-1",
            "--profile",
            "manual_labeling_cv_v1",
        ],
    )
    assert args.workflow_cmd == "resume"
    assert args.workflow_id == "wf-1"
    assert args.profile == "manual_labeling_cv_v1"

    args = parser.parse_args(["workflow", "status", "--task-id", "demo", "--workflow-id", "wf-1"])
    assert args.workflow_cmd == "status"


def test_workflow_start_prints_core_result(monkeypatch, capsys, tmp_path: Path):
    calls: dict[str, object] = {}
    workflow = ModuleType("llm_labeling_scaffold.workflow")

    def start_workflow(runs_root, task, *, profile_id, workflow_id, params):
        calls.update(
            {
                "runs_root": runs_root,
                "task_id": task.task_id,
                "profile_id": profile_id,
                "workflow_id": workflow_id,
                "params": params,
            },
        )
        return {"workflow_id": workflow_id, "status": "pending"}

    workflow.start_workflow = start_workflow
    monkeypatch.setitem(sys.modules, "llm_labeling_scaffold.workflow", workflow)

    cli.main(
        [
            "workflow",
            "start",
            "--task",
            "examples/toy_text_classification/task.yaml",
            "--runs-root",
            str(tmp_path / "runs"),
            "--profile",
            "manual_labeling_cv_v1",
            "--workflow-id",
            "wf-1",
            "--param",
            "batch_size=100",
        ],
    )

    result = json.loads(capsys.readouterr().out)
    assert result == {"workflow_id": "wf-1", "status": "pending"}
    assert calls["task_id"] == "toy_multiclass_v1"
    assert calls["runs_root"] == tmp_path / "runs"
    assert calls["params"] == {"batch_size": 100}


def test_workflow_list_wraps_core_list(monkeypatch, capsys, tmp_path: Path):
    workflow = ModuleType("llm_labeling_scaffold.workflow")
    workflow.list_workflows = lambda runs_root, *, task_id: [{"workflow_id": "wf-1", "task_id": task_id}]
    monkeypatch.setitem(sys.modules, "llm_labeling_scaffold.workflow", workflow)

    cli.main(
        [
            "workflow",
            "list",
            "--task-id",
            "demo",
            "--runs-root",
            str(tmp_path / "runs"),
        ],
    )

    assert json.loads(capsys.readouterr().out) == {
        "workflows": [{"workflow_id": "wf-1", "task_id": "demo"}],
    }


def test_workflow_resume_and_status_use_core_contract(monkeypatch, capsys, tmp_path: Path):
    calls: list[tuple[str, object]] = []
    workflow = ModuleType("llm_labeling_scaffold.workflow")

    def resume_workflow(runs_root, task, workflow_id, *, params, profile_id):
        calls.append(("resume", (runs_root, task.task_id, workflow_id, params, profile_id)))
        return {"workflow_id": workflow_id, "status": "pending"}

    def get_workflow_status(runs_root, task_id, workflow_id):
        calls.append(("status", (runs_root, task_id, workflow_id)))
        return {"workflow_id": workflow_id, "status": "waiting_for_human"}

    workflow.resume_workflow = resume_workflow
    workflow.get_workflow_status = get_workflow_status
    monkeypatch.setitem(sys.modules, "llm_labeling_scaffold.workflow", workflow)

    cli.main(
        [
            "workflow",
            "resume",
            "--task",
            "examples/toy_text_classification/task.yaml",
            "--runs-root",
            str(tmp_path / "runs"),
            "--workflow-id",
            "wf-1",
            "--profile",
            "manual_labeling_cv_v1",
            "--param",
            'batch_size=100',
        ],
    )
    assert json.loads(capsys.readouterr().out) == {"workflow_id": "wf-1", "status": "pending"}

    cli.main(
        [
            "workflow",
            "status",
            "--task-id",
            "toy_multiclass_v1",
            "--runs-root",
            str(tmp_path / "runs"),
            "--workflow-id",
            "wf-1",
        ],
    )
    assert json.loads(capsys.readouterr().out) == {"workflow_id": "wf-1", "status": "waiting_for_human"}
    assert calls[0][0] == "resume"
    assert calls[0][1][1:] == ("toy_multiclass_v1", "wf-1", {"batch_size": 100}, "manual_labeling_cv_v1")
    assert calls[1][0] == "status"
    assert calls[1][1][1:] == ("toy_multiclass_v1", "wf-1")
