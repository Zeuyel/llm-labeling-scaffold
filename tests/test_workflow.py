from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from llm_labeling_scaffold import workflow
from llm_labeling_scaffold.io import read_json, write_jsonl


def _stages(*items: tuple[str, str, list[str]]) -> list[dict]:
    return [
        {
            "id": stage_id,
            "title": stage_id,
            "action": action,
            "depends_on": depends_on,
            "description": "",
        }
        for stage_id, action, depends_on in items
    ]


def _wait_for_manifest(runs_root: Path, task_id: str, workflow_id: str, status: str) -> dict:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        current = workflow.get_workflow_status(runs_root, task_id, workflow_id)
        if current and current.get("status") == status:
            return current
        time.sleep(0.01)
    current = workflow.get_workflow_status(runs_root, task_id, workflow_id)
    assert current is not None
    assert current.get("status") == status
    return current


def _fake_stage_runner(monkeypatch, results: dict[str, object], calls: list[str], *, sequences=None):
    counters: dict[str, int] = {}
    sequences = sequences or {}

    def start_stage(_runs_root, _task, stage, _params):
        stage_id = stage["id"]
        calls.append(stage_id)
        counters[stage_id] = counters.get(stage_id, 0) + 1
        value = sequences.get(stage_id, [results.get(stage_id, {})])
        if isinstance(value, list):
            result = value[min(counters[stage_id] - 1, len(value) - 1)]
        else:
            result = value
        if isinstance(result, dict) and result.get("status") in {"failed", "succeeded"}:
            return {
                "id": f"job-{stage_id}-{counters[stage_id]}",
                "kind": stage["action"],
                **result,
            }
        return {
            "id": f"job-{stage_id}-{counters[stage_id]}",
            "kind": stage["action"],
            "status": "succeeded",
            "result": result,
        }

    monkeypatch.setattr(workflow, "_start_stage_job", start_stage)
    return counters


def test_workflow_runs_asynchronously_in_dependency_order_and_persists_audit_metadata(
    tmp_path: Path, toy_task, monkeypatch
):
    stages = _stages(("first", "sample", []), ("second", "batch", ["first"]))
    monkeypatch.setattr(workflow, "_stage_definitions", lambda _profile: stages)
    input_path = tmp_path / "input.jsonl"
    output_path = tmp_path / "output.jsonl"
    write_jsonl([{"record_id": "r1"}, {"record_id": "r2"}], input_path)
    write_jsonl([{"record_id": "r1"}], output_path)
    calls: list[str] = []
    _fake_stage_runner(
        monkeypatch,
        {
            "first": {"artifact": str(input_path), "kind": "sample"},
            "second": {"artifact": str(output_path), "kind": "batch"},
        },
        calls,
    )

    started = workflow.start_workflow(
        tmp_path / "runs",
        toy_task,
        profile_id="test_profile",
        workflow_id="workflow_audit",
        params={"stages": {"first": {"source": str(input_path)}}},
        input_refs=[str(input_path)],
        poll_interval=0.001,
    )

    assert started["reused"] is False
    manifest = _wait_for_manifest(tmp_path / "runs", toy_task.task_id, "workflow_audit", "succeeded")
    assert calls == ["first", "second"]
    assert manifest["input_refs"][0]["sha256"]
    assert manifest["input_refs"][0]["rows"] == 2
    first = next(stage for stage in manifest["stages"] if stage["id"] == "first")
    second = next(stage for stage in manifest["stages"] if stage["id"] == "second")
    assert first["child_job_id"] == "job-first-1"
    assert first["child_job"]["status"] == "succeeded"
    assert second["output_refs"][0]["rows"] == 1
    assert (tmp_path / "runs" / toy_task.task_id / "workflow_runs" / "workflow_audit" / "manifest.json").is_file()
    assert (tmp_path / "runs" / toy_task.task_id / "workflow_runs" / "workflow_audit" / "stages" / "first.json").is_file()


def test_stage_job_dispatches_import_and_action_to_pipeline_entry_points(tmp_path: Path, toy_task, monkeypatch):
    calls = []

    def fake_import(runs_root, task, *, import_id, overrides, max_bytes):
        calls.append(("import", runs_root, task.task_id, import_id, overrides, max_bytes))
        return {"id": "job-import", "status": "pending"}

    def fake_action(runs_root, task_path, action, params):
        calls.append(("action", runs_root, task_path, action, params))
        return {"id": "job-action", "status": "pending"}

    monkeypatch.setattr(workflow, "start_data_lake_import", fake_import)
    monkeypatch.setattr(workflow, "start_action", fake_action)
    import_stage = {"id": "lake_import", "action": "import"}
    action_stage = {"id": "sample", "action": "sample"}

    workflow._start_stage_job(tmp_path / "runs", toy_task, import_stage, {"import_id": "i1", "overrides": {"x": 1}, "max_bytes": 10})
    workflow._start_stage_job(tmp_path / "runs", toy_task, action_stage, {"rows": 2})

    assert calls == [
        ("import", tmp_path / "runs", toy_task.task_id, "i1", {"x": 1}, 10),
        ("action", tmp_path / "runs", str(toy_task.path), "sample", {"rows": 2}),
    ]


def test_argilla_push_waits_for_human_and_empty_pull_stays_blocked(tmp_path: Path, toy_task, monkeypatch):
    stages = _stages(
        ("push", "argilla_push", []),
        ("pull", "argilla_pull", ["push"]),
        ("gold", "gold", ["pull"]),
    )
    monkeypatch.setattr(workflow, "_stage_definitions", lambda _profile: stages)
    calls: list[str] = []
    _fake_stage_runner(
        monkeypatch,
        {
            "push": {"annotation_id": "a1"},
            "gold": {"artifact": str(tmp_path / "gold.jsonl")},
        },
        calls,
        sequences={"pull": [{"responses": 0, "artifact": str(tmp_path / "empty.jsonl")}, {"responses": 2, "artifact": str(tmp_path / "decisions.jsonl")}]},
    )

    workflow.start_workflow(
        tmp_path / "runs",
        toy_task,
        profile_id="test_profile",
        workflow_id="workflow_human",
        poll_interval=0.001,
    )
    waiting = _wait_for_manifest(tmp_path / "runs", toy_task.task_id, "workflow_human", "waiting_for_human")
    assert calls == ["push"]
    assert next(stage for stage in waiting["stages"] if stage["id"] == "pull")["status"] == "waiting_for_human"

    workflow.resume_workflow(tmp_path / "runs", toy_task, "workflow_human", poll_interval=0.001)
    waiting_empty = _wait_for_manifest(tmp_path / "runs", toy_task.task_id, "workflow_human", "waiting_for_human")
    pull = next(stage for stage in waiting_empty["stages"] if stage["id"] == "pull")
    assert calls == ["push", "pull"]
    assert pull["result"]["responses"] == 0
    assert pull["blocked_reason"] == "Argilla 尚无人工提交结果"

    workflow.resume_workflow(tmp_path / "runs", toy_task, "workflow_human", poll_interval=0.001)
    succeeded = _wait_for_manifest(tmp_path / "runs", toy_task.task_id, "workflow_human", "succeeded")
    assert calls == ["push", "pull", "pull", "gold"]
    assert next(stage for stage in succeeded["stages"] if stage["id"] == "pull")["attempts"] == 2


def test_failed_agreement_audit_blocks_gold_train_and_infer(tmp_path: Path, toy_task, monkeypatch):
    stages = _stages(
        ("audit", "agreement_audit", []),
        ("gold", "gold", ["audit"]),
        ("train", "train", ["gold"]),
        ("infer", "infer", ["train"]),
    )
    monkeypatch.setattr(workflow, "_stage_definitions", lambda _profile: stages)
    calls: list[str] = []
    _fake_stage_runner(monkeypatch, {"audit": {"passed": False, "summary": {"passed": False}}}, calls)

    workflow.start_workflow(tmp_path / "runs", toy_task, profile_id="test_profile", workflow_id="workflow_bad_audit", poll_interval=0.001)
    manifest = _wait_for_manifest(tmp_path / "runs", toy_task.task_id, "workflow_bad_audit", "failed")
    assert calls == ["audit"]
    assert "禁止构建训练集" in manifest["error"]
    assert next(stage for stage in manifest["stages"] if stage["id"] == "audit")["quality_passed"] is False
    assert all(
        next(stage for stage in manifest["stages"] if stage["id"] == stage_id)["status"] == "blocked"
        for stage_id in ("gold", "train", "infer")
    )


def test_workflow_fingerprint_is_idempotent_and_rejects_changed_input_or_params(tmp_path: Path, toy_task, monkeypatch):
    stages = _stages(("one", "sample", []))
    monkeypatch.setattr(workflow, "_stage_definitions", lambda _profile: stages)
    calls: list[str] = []
    _fake_stage_runner(monkeypatch, {"one": {"ok": True}}, calls)
    input_a = tmp_path / "a.jsonl"
    input_b = tmp_path / "b.jsonl"
    write_jsonl([{"record_id": "a"}], input_a)
    write_jsonl([{"record_id": "b"}], input_b)
    kwargs = {
        "runs_root": tmp_path / "runs",
        "task": toy_task,
        "profile_id": "test_profile",
        "workflow_id": "workflow_idempotent",
        "params": {"stages": {"one": {"value": 1}}},
        "input_refs": [str(input_a)],
        "poll_interval": 0.001,
    }
    workflow.start_workflow(**kwargs)
    _wait_for_manifest(tmp_path / "runs", toy_task.task_id, "workflow_idempotent", "succeeded")
    reused = workflow.start_workflow(**kwargs)
    assert reused["reused"] is True
    assert calls == ["one"]
    with pytest.raises(workflow.WorkflowError, match="指纹不同"):
        workflow.start_workflow(**{**kwargs, "params": {"stages": {"one": {"value": 2}}}})
    with pytest.raises(workflow.WorkflowError, match="指纹不同"):
        workflow.start_workflow(**{**kwargs, "input_refs": [str(input_b)]})
    write_jsonl([{"record_id": "changed"}], input_a)
    with pytest.raises(workflow.WorkflowError, match="指纹不同"):
        workflow.start_workflow(**kwargs)


def test_resume_restarts_only_failed_stage(tmp_path: Path, toy_task, monkeypatch):
    stages = _stages(("first", "sample", []), ("second", "batch", ["first"]))
    monkeypatch.setattr(workflow, "_stage_definitions", lambda _profile: stages)
    calls: list[str] = []
    _fake_stage_runner(
        monkeypatch,
        {"first": {"ok": True}},
        calls,
        sequences={"second": [{"status": "failed", "error": "transient"}, {"status": "succeeded", "result": {"ok": True}}]},
    )

    workflow.start_workflow(tmp_path / "runs", toy_task, profile_id="test_profile", workflow_id="workflow_resume", poll_interval=0.001)
    failed = _wait_for_manifest(tmp_path / "runs", toy_task.task_id, "workflow_resume", "failed")
    assert calls == ["first", "second"]
    assert next(stage for stage in failed["stages"] if stage["id"] == "first")["status"] == "succeeded"

    workflow.resume_workflow(tmp_path / "runs", toy_task, "workflow_resume", poll_interval=0.001)
    succeeded = _wait_for_manifest(tmp_path / "runs", toy_task.task_id, "workflow_resume", "succeeded")
    assert calls == ["first", "second", "second"]
    assert next(stage for stage in succeeded["stages"] if stage["id"] == "second")["attempts"] == 2


def test_status_and_list_apis_read_persisted_workflow(tmp_path: Path, toy_task, monkeypatch):
    stages = _stages(("one", "sample", []))
    monkeypatch.setattr(workflow, "_stage_definitions", lambda _profile: stages)
    _fake_stage_runner(monkeypatch, {"one": {"ok": True}}, [])
    workflow.start_workflow(tmp_path / "runs", toy_task, profile_id="test_profile", workflow_id="workflow_list", poll_interval=0.001)
    _wait_for_manifest(tmp_path / "runs", toy_task.task_id, "workflow_list", "succeeded")

    assert workflow.workflow_status(tmp_path / "runs", toy_task.task_id, "workflow_list")["status"] == "succeeded"
    listed = workflow.list_workflows(tmp_path / "runs")
    assert [item["workflow_id"] for item in listed] == ["workflow_list"]
    assert read_json(
        tmp_path / "runs" / toy_task.task_id / "workflow_runs" / "workflow_list" / "stages" / "one.json"
    )["task_id"] == toy_task.task_id


def test_quality_control_profile_routes_pilot_batch_through_argilla_before_audit():
    stages = workflow._stage_definitions("manual_labeling_quality_control_v1")
    ids = [stage["id"] for stage in stages]
    assert ids.index("pilot_calibration") < ids.index("pilot_argilla_dispatch")
    assert ids.index("pilot_argilla_dispatch") < ids.index("pilot_argilla_pull")
    assert ids.index("pilot_argilla_pull") < ids.index("consistency_check")
    assert next(stage for stage in stages if stage["id"] == "pilot_argilla_dispatch")["action"] == "argilla_push"
    assert next(stage for stage in stages if stage["id"] == "pilot_argilla_pull")["action"] == "argilla_pull"
    assert next(stage for stage in stages if stage["id"] == "consistency_check")["depends_on"] == ["pilot_argilla_pull"]


def test_quality_control_pilot_push_waits_before_consistency_check(tmp_path: Path, toy_task, monkeypatch):
    calls: list[tuple[str, dict]] = []
    sample_path = tmp_path / "sample.jsonl"
    batch_manifest = tmp_path / "batch-manifest.json"

    def start_stage(_runs_root, _task, stage, params):
        calls.append((stage["id"], dict(params)))
        results = {
            "lake_import": {"import_id": "import-1"},
            "sample": {"artifact": str(sample_path), "kind": "sample"},
            "pilot_calibration": {
                "manifest_path": str(batch_manifest),
                "sample_id": "sample-1",
                "plan_id": "pilot-plan-1",
            },
            "pilot_argilla_dispatch": {
                "annotation_id": "pilot-annotation-1",
                "result": {"dataset": "pilot-dataset"},
            },
        }
        return {
            "id": f"job-{stage['id']}",
            "kind": stage["action"],
            "status": "succeeded",
            "result": results.get(stage["id"], {}),
        }

    monkeypatch.setattr(workflow, "_start_stage_job", start_stage)
    workflow.start_workflow(
        tmp_path / "runs",
        toy_task,
        profile_id="manual_labeling_quality_control_v1",
        workflow_id="workflow_quality_pilot",
        poll_interval=0.001,
    )

    waiting = _wait_for_manifest(tmp_path / "runs", toy_task.task_id, "workflow_quality_pilot", "waiting_for_human")
    call_ids = [stage_id for stage_id, _params in calls]
    assert call_ids == ["lake_import", "sample", "pilot_calibration", "pilot_argilla_dispatch"]
    dispatch_params = dict(calls[-1][1])
    assert dispatch_params["dispatch_mode"] == "batch_plan"
    assert dispatch_params["batch_manifest_path"] == str(batch_manifest)
    assert dispatch_params["batch_plan_id"] == "pilot-plan-1"
    assert dispatch_params["sample"] == str(sample_path)
    assert next(stage for stage in waiting["stages"] if stage["id"] == "consistency_check")["status"] == "pending"
    assert next(stage for stage in waiting["stages"] if stage["id"] == "pilot_argilla_pull")["status"] == "waiting_for_human"


def test_quality_control_pilot_pull_requires_responses_before_consistency_check(tmp_path: Path, toy_task, monkeypatch):
    calls: list[tuple[str, dict]] = []
    pull_count = 0
    sample_path = tmp_path / "sample.jsonl"
    decisions_path = tmp_path / "decisions.jsonl"

    def start_stage(_runs_root, _task, stage, params):
        nonlocal pull_count
        calls.append((stage["id"], dict(params)))
        result: dict = {}
        if stage["id"] == "sample":
            result = {"artifact": str(sample_path)}
        elif stage["id"] == "pilot_calibration":
            result = {"manifest_path": str(tmp_path / "pilot-manifest.json"), "sample_id": "sample-1", "plan_id": "pilot-plan-1"}
        elif stage["id"] == "pilot_argilla_dispatch":
            result = {"annotation_id": "pilot-annotation-1", "result": {"dataset": "pilot-dataset"}}
        elif stage["id"] == "pilot_argilla_pull":
            pull_count += 1
            result = {"responses": 0 if pull_count == 1 else 2, "artifact": str(decisions_path)}
        elif stage["id"] == "consistency_check":
            result = {"passed": True}
        elif stage["id"] == "main_annotation":
            result = {"annotation_id": "main-annotation-1", "result": {"dataset": "main-dataset"}}
        return {
            "id": f"job-{stage['id']}-{len(calls)}",
            "kind": stage["action"],
            "status": "succeeded",
            "result": result,
        }

    monkeypatch.setattr(workflow, "_start_stage_job", start_stage)
    workflow.start_workflow(
        tmp_path / "runs",
        toy_task,
        profile_id="manual_labeling_quality_control_v1",
        workflow_id="workflow_quality_wait",
        poll_interval=0.001,
    )
    _wait_for_manifest(tmp_path / "runs", toy_task.task_id, "workflow_quality_wait", "waiting_for_human")

    workflow.resume_workflow(tmp_path / "runs", toy_task, "workflow_quality_wait", poll_interval=0.001)
    empty = _wait_for_manifest(tmp_path / "runs", toy_task.task_id, "workflow_quality_wait", "waiting_for_human")
    assert [stage_id for stage_id, _params in calls].count("consistency_check") == 0
    assert next(stage for stage in empty["stages"] if stage["id"] == "pilot_argilla_pull")["result"]["responses"] == 0

    workflow.resume_workflow(tmp_path / "runs", toy_task, "workflow_quality_wait", poll_interval=0.001)
    final = _wait_for_manifest(tmp_path / "runs", toy_task.task_id, "workflow_quality_wait", "waiting_for_human")
    call_ids = [stage_id for stage_id, _params in calls]
    assert call_ids.count("pilot_argilla_pull") == 2
    assert call_ids.index("pilot_argilla_pull") < call_ids.index("consistency_check")
    assert call_ids.index("consistency_check") < call_ids.index("main_annotation")
    audit_params = next(params for stage_id, params in calls if stage_id == "consistency_check")
    assert audit_params["sample"] == str(sample_path)
    assert audit_params["decisions"] == str(decisions_path)
    assert next(stage for stage in final["stages"] if stage["id"] == "consistency_check")["status"] == "succeeded"


def test_resume_recovers_non_secret_params_after_process_state_is_lost(tmp_path: Path, toy_task, monkeypatch):
    stages = _stages(("one", "sample", []))
    monkeypatch.setattr(workflow, "_stage_definitions", lambda _profile: stages)
    calls: list[str] = []
    _fake_stage_runner(
        monkeypatch,
        {},
        calls,
        sequences={"one": [{"status": "failed", "error": "transient"}, {"ok": True}]},
    )
    workflow.start_workflow(
        tmp_path / "runs",
        toy_task,
        profile_id="test_profile",
        workflow_id="workflow_restart_non_secret",
        params={"stages": {"one": {"value": 1}}},
        poll_interval=0.001,
    )
    _wait_for_manifest(tmp_path / "runs", toy_task.task_id, "workflow_restart_non_secret", "failed")
    workflow._WORKFLOW_INPUTS.clear()

    workflow.resume_workflow(tmp_path / "runs", toy_task, "workflow_restart_non_secret", poll_interval=0.001)
    succeeded = _wait_for_manifest(tmp_path / "runs", toy_task.task_id, "workflow_restart_non_secret", "succeeded")
    assert succeeded["params_recoverable"] is True
    assert calls == ["one", "one"]


def test_resume_requires_sensitive_params_after_process_state_is_lost(tmp_path: Path, toy_task, monkeypatch):
    stages = _stages(("one", "sample", []))
    monkeypatch.setattr(workflow, "_stage_definitions", lambda _profile: stages)
    calls: list[str] = []
    _fake_stage_runner(
        monkeypatch,
        {},
        calls,
        sequences={"one": [{"status": "failed", "error": "transient"}, {"ok": True}]},
    )
    workflow.start_workflow(
        tmp_path / "runs",
        toy_task,
        profile_id="test_profile",
        workflow_id="workflow_restart_secret",
        params={"stages": {"one": {"api_token": "secret-a"}}},
        poll_interval=0.001,
    )
    _wait_for_manifest(tmp_path / "runs", toy_task.task_id, "workflow_restart_secret", "failed")
    workflow._WORKFLOW_INPUTS.clear()

    with pytest.raises(workflow.WorkflowError, match="敏感参数"):
        workflow.resume_workflow(tmp_path / "runs", toy_task, "workflow_restart_secret", poll_interval=0.001)
    with pytest.raises(workflow.WorkflowError, match="指纹不同"):
        workflow.resume_workflow(
            tmp_path / "runs",
            toy_task,
            "workflow_restart_secret",
            params={"stages": {"one": {"api_token": "secret-b"}}},
            poll_interval=0.001,
        )

    workflow.resume_workflow(
        tmp_path / "runs",
        toy_task,
        "workflow_restart_secret",
        params={"stages": {"one": {"api_token": "secret-a"}}},
        poll_interval=0.001,
    )
    succeeded = _wait_for_manifest(tmp_path / "runs", toy_task.task_id, "workflow_restart_secret", "succeeded")
    assert succeeded["params_recoverable"] is False
    assert calls == ["one", "one"]


def test_resume_recovers_persisted_child_without_restarting_argilla_push(tmp_path: Path, toy_task, monkeypatch):
    stages = _stages(("push", "argilla_push", []), ("pull", "argilla_pull", ["push"]))
    monkeypatch.setattr(workflow, "_stage_definitions", lambda _profile: stages)
    stage_params = workflow._stage_params(stages, None)
    manifest = workflow._initial_manifest(
        toy_task,
        "test_profile",
        "workflow_recover_push",
        stages,
        stage_params,
        None,
    )
    push = next(stage for stage in manifest["stages"] if stage["id"] == "push")
    push.update(
        {
            "status": "running",
            "attempts": 1,
            "child_job_id": "persisted-push-job",
            "child_job": {
                "id": "persisted-push-job",
                "kind": "argilla_push",
                "status": "succeeded",
                "result": {"annotation_id": "annotation-1", "dataset": "pilot-dataset"},
            },
        }
    )
    base = workflow.workflow_dir(tmp_path / "runs", toy_task.task_id, "workflow_recover_push")
    workflow._persist(manifest, base)
    calls: list[str] = []

    def unexpected_start(*_args, **_kwargs):
        calls.append("unexpected")
        raise AssertionError("恢复已有 push 子任务时不得重新推送")

    monkeypatch.setattr(workflow, "_start_stage_job", unexpected_start)
    workflow._WORKFLOW_INPUTS.clear()
    workflow.resume_workflow(
        tmp_path / "runs",
        toy_task,
        "workflow_recover_push",
        poll_interval=0.001,
    )

    waiting = _wait_for_manifest(
        tmp_path / "runs",
        toy_task.task_id,
        "workflow_recover_push",
        "waiting_for_human",
    )
    assert calls == []
    assert next(stage for stage in waiting["stages"] if stage["id"] == "push")["status"] == "succeeded"
    assert next(stage for stage in waiting["stages"] if stage["id"] == "pull")["status"] == "waiting_for_human"


def test_resume_fails_closed_when_running_child_is_missing(tmp_path: Path, toy_task, monkeypatch):
    stages = _stages(("push", "argilla_push", []))
    monkeypatch.setattr(workflow, "_stage_definitions", lambda _profile: stages)
    stage_params = workflow._stage_params(stages, None)
    manifest = workflow._initial_manifest(
        toy_task,
        "test_profile",
        "workflow_missing_child",
        stages,
        stage_params,
        None,
    )
    push = manifest["stages"][0]
    push.update(
        {
            "status": "running",
            "attempts": 1,
            "child_job_id": "missing-push-job",
            "child_job": {
                "id": "missing-push-job",
                "kind": "argilla_push",
                "status": "running",
            },
        }
    )
    workflow._persist(
        manifest,
        workflow.workflow_dir(tmp_path / "runs", toy_task.task_id, "workflow_missing_child"),
    )
    calls: list[str] = []

    def unexpected_start(*_args, **_kwargs):
        calls.append("unexpected")
        raise AssertionError("缺失的子任务不得自动重推")

    monkeypatch.setattr(workflow, "_start_stage_job", unexpected_start)
    workflow._WORKFLOW_INPUTS.clear()
    workflow.resume_workflow(
        tmp_path / "runs",
        toy_task,
        "workflow_missing_child",
        poll_interval=0.001,
    )
    failed = _wait_for_manifest(
        tmp_path / "runs",
        toy_task.task_id,
        "workflow_missing_child",
        "failed",
    )
    assert calls == []
    assert failed["recovery_required"] is True
    assert failed["stages"][0]["recovery_required"] is True
    with pytest.raises(workflow.WorkflowError, match="人工确认"):
        workflow.resume_workflow(
            tmp_path / "runs",
            toy_task,
            "workflow_missing_child",
            poll_interval=0.001,
        )


def test_concurrent_start_is_idempotent_and_creates_one_child_job(tmp_path: Path, toy_task, monkeypatch):
    stages = _stages(("one", "sample", []))
    monkeypatch.setattr(workflow, "_stage_definitions", lambda _profile: stages)
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def start_stage(_runs_root, _task, stage, _params):
        calls.append(stage["id"])
        entered.set()
        assert release.wait(3)
        return {"id": "one-job", "kind": "sample", "status": "succeeded", "result": {"ok": True}}

    monkeypatch.setattr(workflow, "_start_stage_job", start_stage)
    kwargs = {
        "runs_root": tmp_path / "runs",
        "task": toy_task,
        "profile_id": "test_profile",
        "workflow_id": "workflow_concurrent_start",
        "poll_interval": 0.001,
    }
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(workflow.start_workflow, **kwargs) for _ in range(8)]
        assert entered.wait(3)
        release.set()
        results = [future.result(timeout=3) for future in futures]
    succeeded = _wait_for_manifest(
        tmp_path / "runs",
        toy_task.task_id,
        "workflow_concurrent_start",
        "succeeded",
    )
    assert len(calls) == 1
    assert all(result["workflow_id"] == "workflow_concurrent_start" for result in results)
    assert next(stage for stage in succeeded["stages"] if stage["id"] == "one")["attempts"] == 1


def test_argilla_pull_retry_does_not_repeat_push(tmp_path: Path, toy_task, monkeypatch):
    stages = _stages(("push", "argilla_push", []), ("pull", "argilla_pull", ["push"]))
    monkeypatch.setattr(workflow, "_stage_definitions", lambda _profile: stages)
    calls: list[str] = []
    pull_params: list[dict] = []
    pull_attempts = 0

    def start_stage(_runs_root, _task, stage, _params):
        nonlocal pull_attempts
        calls.append(stage["id"])
        if stage["id"] == "pull":
            pull_params.append(dict(_params))
            pull_attempts += 1
            output_path = Path(_params["output"])
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(f"attempt-{pull_attempts}\n", encoding="utf-8")
            result = {"responses": 0 if pull_attempts == 1 else 1, "artifact": str(output_path)}
        else:
            result = {"annotation_id": "annotation-1", "dataset": "dataset-1"}
        return {
            "id": f"{stage['id']}-{len(calls)}",
            "kind": stage["action"],
            "status": "succeeded",
            "result": result,
        }

    monkeypatch.setattr(workflow, "_start_stage_job", start_stage)
    workflow.start_workflow(
        tmp_path / "runs",
        toy_task,
        profile_id="test_profile",
        workflow_id="workflow_pull_retry",
        poll_interval=0.001,
    )
    _wait_for_manifest(tmp_path / "runs", toy_task.task_id, "workflow_pull_retry", "waiting_for_human")
    workflow.resume_workflow(tmp_path / "runs", toy_task, "workflow_pull_retry", poll_interval=0.001)
    _wait_for_manifest(tmp_path / "runs", toy_task.task_id, "workflow_pull_retry", "waiting_for_human")
    workflow.resume_workflow(tmp_path / "runs", toy_task, "workflow_pull_retry", poll_interval=0.001)
    _wait_for_manifest(tmp_path / "runs", toy_task.task_id, "workflow_pull_retry", "succeeded")
    assert calls == ["push", "pull", "pull"]
    final = workflow.get_workflow_status(tmp_path / "runs", toy_task.task_id, "workflow_pull_retry")
    push = next(stage for stage in final["stages"] if stage["id"] == "push")
    pull = next(stage for stage in final["stages"] if stage["id"] == "pull")
    assert push["attempts"] == 1
    assert pull["attempts"] == 2
    assert pull_params[0]["decision_id"] != pull_params[1]["decision_id"]
    assert pull_params[0]["output"] != pull_params[1]["output"]
    assert Path(pull_params[0]["output"]).read_text(encoding="utf-8") == "attempt-1\n"
    assert Path(pull_params[1]["output"]).read_text(encoding="utf-8") == "attempt-2\n"
    assert [attempt["attempt"] for attempt in pull["attempt_history"]] == [1, 2]
    assert (tmp_path / "runs" / toy_task.task_id / "workflow_runs" / "workflow_pull_retry" / "stages" / "pull" / "attempts" / "attempt_0001.json").is_file()
    assert (tmp_path / "runs" / toy_task.task_id / "workflow_runs" / "workflow_pull_retry" / "stages" / "pull" / "attempts" / "attempt_0002.json").is_file()


def test_resume_waits_for_waiting_thread_to_exit_before_restart(tmp_path: Path, toy_task, monkeypatch):
    stages = _stages(("push", "argilla_push", []), ("pull", "argilla_pull", ["push"]))
    monkeypatch.setattr(workflow, "_stage_definitions", lambda _profile: stages)
    waiting_persisted = threading.Event()
    resume_read_waiting = threading.Event()
    allow_waiting_thread_exit = threading.Event()
    original_persist = workflow._persist
    original_read_manifest = workflow._read_manifest
    calls: list[str] = []
    pull_attempts = 0

    def persist(manifest, base):
        original_persist(manifest, base)
        if manifest.get("status") == "waiting_for_human" and not waiting_persisted.is_set():
            waiting_persisted.set()
            assert allow_waiting_thread_exit.wait(3)

    def read_manifest(runs_root, task_id, workflow_id):
        manifest = original_read_manifest(runs_root, task_id, workflow_id)
        if waiting_persisted.is_set() and manifest and manifest.get("status") == "waiting_for_human":
            resume_read_waiting.set()
        return manifest

    def start_stage(_runs_root, _task, stage, _params):
        nonlocal pull_attempts
        calls.append(stage["id"])
        if stage["id"] == "pull":
            pull_attempts += 1
            result = {"responses": 0 if pull_attempts == 1 else 1}
        else:
            result = {"annotation_id": "annotation-1"}
        return {"id": f"{stage['id']}-{len(calls)}", "kind": stage["action"], "status": "succeeded", "result": result}

    monkeypatch.setattr(workflow, "_persist", persist)
    monkeypatch.setattr(workflow, "_read_manifest", read_manifest)
    monkeypatch.setattr(workflow, "_start_stage_job", start_stage)
    workflow.start_workflow(
        tmp_path / "runs",
        toy_task,
        profile_id="test_profile",
        workflow_id="workflow_waiting_handoff",
        poll_interval=0.001,
    )
    assert waiting_persisted.wait(3)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            workflow.resume_workflow,
            tmp_path / "runs",
            toy_task,
            "workflow_waiting_handoff",
            poll_interval=0.001,
        )
        assert resume_read_waiting.wait(3)
        allow_waiting_thread_exit.set()
        future.result(timeout=3)

    _wait_for_manifest(tmp_path / "runs", toy_task.task_id, "workflow_waiting_handoff", "waiting_for_human")
    workflow.resume_workflow(
        tmp_path / "runs",
        toy_task,
        "workflow_waiting_handoff",
        poll_interval=0.001,
    )
    succeeded = _wait_for_manifest(tmp_path / "runs", toy_task.task_id, "workflow_waiting_handoff", "succeeded")
    assert calls == ["push", "pull", "pull"]
    assert next(stage for stage in succeeded["stages"] if stage["id"] == "pull")["attempts"] == 2
