from __future__ import annotations

import base64
import hmac
import json
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
import threading
import urllib.error
import urllib.request

import pytest

from llm_labeling_scaffold import panel
from llm_labeling_scaffold import pipeline
from llm_labeling_scaffold.auth import build_panel_authenticator
from llm_labeling_scaffold import gold as gold_module
from llm_labeling_scaffold.audit import audit_run
from llm_labeling_scaffold.batching import batch_records
from llm_labeling_scaffold.gold import build_gold_from_decisions
from llm_labeling_scaffold.io import read_json, read_jsonl, sha256_file, write_json, write_jsonl
from llm_labeling_scaffold.merge import merge_run


def _decode_basic(header: str) -> tuple[str, str]:
    raw = base64.b64decode(header[6:]).decode("utf-8")
    user, _, pw = raw.partition(":")
    return user, pw


@contextmanager
def _panel_server(runs_root: Path, tasks_root: Path):
    old = {
        "runs_root": panel._Handler.runs_root,
        "tasks_root": panel._Handler.tasks_root,
        "static_dir": panel._Handler.static_dir,
        "authenticator": panel._Handler.authenticator,
    }
    panel._Handler.runs_root = runs_root
    panel._Handler.tasks_root = tasks_root
    panel._Handler.static_dir = None
    panel._Handler.authenticator = build_panel_authenticator(
        mode="basic_dev",
        basic_user="admin",
        basic_password="secret",
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), panel._Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        for key, value in old.items():
            setattr(panel._Handler, key, value)


def _request(base_url: str, path: str, *, method: str = "GET", body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        base_url + path,
        data=data,
        headers={
            "Authorization": "Basic " + base64.b64encode(b"admin:secret").decode("ascii"),
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_discover_runs_finds_demo(panel_workspace):
    runs = panel._discover_runs(panel_workspace["runs_root"])
    ids = {(r["task_id"], r["run_id"]) for r in runs}
    assert ("toy_multiclass_v1", "demo") in ids
    demo = next(r for r in runs if r["run_id"] == "demo")
    assert demo["merge"] and demo["merge"]["merged_rows"] == 2


def test_sample_rows_returns_merged(panel_workspace):
    runs = panel._discover_runs(panel_workspace["runs_root"])
    demo = next(r for r in runs if r["run_id"] == "demo")
    rows = panel._sample_rows(Path(demo["path"]))
    assert rows and "record_id" in rows[0]


def test_basic_auth_roundtrip():
    header = "Basic " + base64.b64encode(b"admin:secret123").decode()
    user, pw = _decode_basic(header)
    assert hmac.compare_digest(user, "admin")
    assert hmac.compare_digest(pw, "secret123")
    assert not hmac.compare_digest(pw, "wrong")


def test_action_api_redacts_sensitive_url_from_errors(tmp_path: Path, monkeypatch):
    runs_root = tmp_path / "runs"
    tasks_root = tmp_path / "tasks"
    created = pipeline.create_task(
        tasks_root,
        {
            "task_id": "argilla_url_error_task",
            "id_field": "record_id",
            "text_fields": ["text"],
            "primary_label_name": "label",
            "primary_label_values": ["yes", "no"],
        },
    )
    api_url = "https://api-user:api-password@argilla.example?access_token=api-secret"

    def fail_action(*args, **kwargs):
        raise RuntimeError(f"upstream rejected {api_url}")

    monkeypatch.setattr(pipeline, "start_action", fail_action)

    with _panel_server(runs_root, tasks_root) as base_url:
        status, payload = _request(
            base_url,
            "/api/action",
            method="POST",
            body={"task": created["path"], "action": "argilla_pull", "params": {"api_url": api_url}},
        )

    assert status == 400
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "api-user" not in serialized
    assert "api-password" not in serialized
    assert "api-secret" not in serialized
    assert "argilla.example" in serialized
    assert "[REDACTED]" in serialized


def test_task_source_mode_defaults_to_local(monkeypatch):
    monkeypatch.delenv("LLS_TASK_SOURCE", raising=False)
    assert panel._task_source_mode() == "local"
    monkeypatch.setenv("LLS_TASK_SOURCE", "r2")
    assert panel._task_source_mode() == "r2"
    monkeypatch.setenv("LLS_TASK_SOURCE", "data_lake")
    assert panel._task_source_mode() == "r2"


def test_run_detail_and_pools(panel_workspace):
    runs = panel.discover_runs(panel_workspace["runs_root"])
    demo = next(r for r in runs if r["run_id"] == "demo")
    detail = panel.run_detail(Path(demo["path"]))
    assert "pools" in detail and "merged" in detail["pools"]
    assert detail["pools"]["merged"] == 2
    for kind in ("merged", "missing", "duplicate", "conflict"):
        assert isinstance(panel.pool_rows(Path(demo["path"]), kind), list)


def test_list_gold(panel_workspace):
    gold = panel.list_gold(panel_workspace["runs_root"], "toy_multiclass_v1")
    assert gold and gold[0]["task_id"] == "toy_multiclass_v1"


def test_append_decision_roundtrip(panel_workspace):
    run_dir = panel_workspace["run_dir"]
    n = panel.append_decision(
        run_dir,
        {"record_id": "r001", "human_label": {"class_label": "non_target"}, "note": "t"},
    )
    assert n == 1
    assert (run_dir / "adjudication" / "decisions.jsonl").exists()


def test_safe_segment_blocks_traversal():
    assert panel._safe_segment("demo")
    assert not panel._safe_segment("../etc")
    assert not panel._safe_segment("a/b")
    assert not panel._safe_segment("a\\b")


def test_parse_import_rows_accepts_jsonl_and_json_array():
    rows = panel.parse_import_rows('{"record_id":"r001"}\n{"record_id":"r002"}\n')
    assert [row["record_id"] for row in rows] == ["r001", "r002"]

    rows = panel.parse_import_rows('[{"record_id":"r003"}]')
    assert rows == [{"record_id": "r003"}]


def test_parse_import_rows_rejects_bad_lines():
    try:
        panel.parse_import_rows('{"record_id":"r001"}\nnot-json\n')
    except ValueError as exc:
        assert "第 2 行" in str(exc)
    else:
        raise AssertionError("bad JSONL line should fail")


def test_list_decision_artifacts_reads_manifest(panel_workspace):
    artifacts = pipeline.list_decision_artifacts(panel_workspace["runs_root"], "toy_multiclass_v1")
    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert artifact["task_id"] == "toy_multiclass_v1"
    assert artifact["decision_id"] == "argilla_round_1"
    assert artifact["source"] == "argilla"
    assert artifact["argilla_dataset"] == "toy_argilla_round_1"
    assert artifact["sample_id"] == "sample_a"
    assert artifact["sample_path"] == str(panel_workspace["sample_path"])
    assert artifact["path"] == str(panel_workspace["decisions_path"])
    assert artifact["rows"] == 2
    assert artifact["decisions_pulled"] is True
    assert artifact["state"] == "decisions_pulled"
    assert artifact["linked_decision_ids"] == ["argilla_round_1"]


def test_asset_status_detail_endpoints_empty_state(tmp_path: Path):
    with _panel_server(tmp_path / "runs", tmp_path / "tasks") as base_url:
        status, annotations = _request(base_url, "/api/task/annotation_jobs?task_id=empty_task")
        assert status == 200
        assert annotations == {"annotation_jobs": []}

        status, decisions = _request(base_url, "/api/task/decision_artifacts?task_id=empty_task")
        assert status == 200
        assert decisions == {"decision_artifacts": []}

        status, gold = _request(base_url, "/api/task/gold_versions?task_id=empty_task")
        assert status == 200
        assert gold == {"gold_versions": []}

        status, missing = _request(
            base_url,
            "/api/annotation_job/detail?task_id=empty_task&annotation_id=missing_round",
        )
        assert status == 404
        assert missing["found"] is False

        status, bad = _request(
            base_url,
            "/api/decision_artifact/detail?task_id=empty_task&decision_id=../bad",
        )
        assert status == 400
        assert "error" in bad


def test_asset_status_detail_endpoints_return_smoke_contract(panel_workspace):
    task_id = "toy_multiclass_v1"
    runs_root = panel_workspace["runs_root"]
    annotation_dir = runs_root / task_id / "annotation_jobs" / "argilla_round_1"
    dispatch_path = annotation_dir / "dispatch.jsonl"
    write_jsonl([{"record_id": "r001", "title": "General notice"}], dispatch_path)
    write_json(
        {
            "task_id": task_id,
            "annotation_id": "argilla_round_1",
            "source": "argilla",
            "argilla_dataset": "toy_argilla_round_1",
            "dispatch_path": str(dispatch_path),
            "status": "已分发",
            "result": {"records": 1, "record_id_policy": {"strategy": "original"}},
        },
        annotation_dir / "manifest.json",
    )
    gold_dir = runs_root / task_id / "gold"
    gold_path = gold_dir / "gold_from_decisions_v001.jsonl"
    write_json(
        {
            "task_id": task_id,
            "version": "from_decisions_v001",
            "path": str(gold_path),
            "rows": 2,
            "source": "decision_artifact",
            "decisions": str(panel_workspace["decisions_path"]),
        },
        gold_dir / "gold_from_decisions_v001.manifest.json",
    )

    with _panel_server(runs_root, Path("examples")) as base_url:
        status, annotation = _request(
            base_url,
            "/api/annotation_job/detail?task_id=toy_multiclass_v1&annotation_id=argilla_round_1",
        )
        assert status == 200
        annotation_job = annotation["annotation_job"]
        assert annotation_job["local_dispatch_file_exists"] is True
        assert annotation_job["argilla_published"] is True
        assert annotation_job["decisions_pulled"] is True
        assert annotation_job["gold_generated"] is True
        assert annotation_job["linked_decision_ids"] == ["argilla_round_1"]
        assert annotation_job["linked_gold_versions"] == ["from_decisions_v001"]

        status, decision = _request(
            base_url,
            "/api/decision_artifact/detail?task_id=toy_multiclass_v1&decision_id=argilla_round_1",
        )
        assert status == 200
        assert decision["decision_artifact"]["linked_gold_versions"] == ["from_decisions_v001"]

        status, gold = _request(
            base_url,
            "/api/gold_version/detail?task_id=toy_multiclass_v1&version=from_decisions_v001",
        )
        assert status == 200
        assert gold["gold_version"]["linked_decision_ids"] == ["argilla_round_1"]


def test_task_graph_api_returns_nodes_and_edges(panel_workspace):
    with _panel_server(panel_workspace["runs_root"], Path("examples")) as base_url:
        status, body = _request(base_url, "/api/task/graph?task_id=toy_multiclass_v1")

    assert status == 200
    node_ids = {node["id"] for node in body["nodes"]}
    assert "task" in node_ids
    assert "stage:lake_import" in node_ids
    assert "sample:sample_a" in node_ids
    assert "decision:argilla_round_1" in node_ids
    assert any(edge["source"] == "sample:sample_a" and edge["target"] == "decision:argilla_round_1" for edge in body["edges"])


def test_workflow_start_api_passes_validated_inputs(panel_workspace, monkeypatch):
    calls = {}

    class FakeWorkflow:
        @staticmethod
        def start_workflow(runs_root, task, *, profile_id, workflow_id, params):
            calls.update({
                "runs_root": runs_root,
                "task_id": task.task_id,
                "profile_id": profile_id,
                "workflow_id": workflow_id,
                "params": params,
            })
            return {"workflow_id": workflow_id, "status": "running"}

    monkeypatch.setattr(panel, "_workflow_api", lambda: FakeWorkflow)
    with _panel_server(panel_workspace["runs_root"], Path("examples")) as base_url:
        status, body = _request(
            base_url,
            "/api/workflow/start",
            method="POST",
            body={
                "task_id": "toy_multiclass_v1",
                "profile": "manual_labeling_cv_v1",
                "workflow_id": "workflow_001",
                "params": {"batch_size": 10},
            },
        )

    assert status == 200
    assert body == {"ok": True, "workflow": {"workflow_id": "workflow_001", "status": "running"}}
    assert calls == {
        "runs_root": panel_workspace["runs_root"],
        "task_id": "toy_multiclass_v1",
        "profile_id": "manual_labeling_cv_v1",
        "workflow_id": "workflow_001",
        "params": {"batch_size": 10},
    }


def test_workflow_list_and_status_api(panel_workspace, monkeypatch):
    calls = []

    class FakeWorkflow:
        @staticmethod
        def list_workflows(runs_root, task_id):
            calls.append(("list", runs_root, task_id))
            return [{"workflow_id": "workflow_001", "status": "running"}]

        @staticmethod
        def get_workflow_status(runs_root, task_id, workflow_id):
            calls.append(("status", runs_root, task_id, workflow_id))
            return {"workflow_id": workflow_id, "status": "running"}

    monkeypatch.setattr(panel, "_workflow_api", lambda: FakeWorkflow)
    with _panel_server(panel_workspace["runs_root"], Path("examples")) as base_url:
        list_status, list_body = _request(base_url, "/api/workflow?task_id=toy_multiclass_v1")
        status_status, status_body = _request(
            base_url,
            "/api/workflow/status?task_id=toy_multiclass_v1&workflow_id=workflow_001",
        )

    assert list_status == 200
    assert list_body == {"workflows": [{"workflow_id": "workflow_001", "status": "running"}]}
    assert status_status == 200
    assert status_body == {"workflow": {"workflow_id": "workflow_001", "status": "running"}}
    assert calls == [
        ("list", panel_workspace["runs_root"], "toy_multiclass_v1"),
        ("status", panel_workspace["runs_root"], "toy_multiclass_v1", "workflow_001"),
    ]


def test_workflow_status_api_returns_404_for_unknown_workflow(panel_workspace):
    with _panel_server(panel_workspace["runs_root"], Path("examples")) as base_url:
        status, body = _request(
            base_url,
            "/api/workflow/status?task_id=toy_multiclass_v1&workflow_id=missing_workflow",
        )

    assert status == 404
    assert body == {"error": "workflow not found: missing_workflow"}


def test_workflow_api_preserves_r2_task_registry_constraint(panel_workspace, monkeypatch):
    class SyncedTasks:
        tasks = {}

    called = False

    def unavailable():
        nonlocal called
        called = True
        raise AssertionError("workflow engine must not run for an unregistered R2 task")

    monkeypatch.setenv("LLS_TASK_SOURCE", "r2")
    monkeypatch.setattr(panel._Handler, "_sync_tasks_if_needed", lambda _self: SyncedTasks())
    monkeypatch.setattr(panel, "_workflow_api", unavailable)
    with _panel_server(panel_workspace["runs_root"], Path("examples")) as base_url:
        status, body = _request(
            base_url,
            "/api/workflow/status?task_id=toy_multiclass_v1&workflow_id=workflow_001",
        )

    assert status == 400
    assert "未在 R2 数据湖登记表中登记为启用状态" in body["error"]
    assert called is False


def test_workflow_api_does_not_sync_r2_registry_in_local_mode(panel_workspace, monkeypatch):
    apply_calls = []

    class FakeWorkflow:
        @staticmethod
        def get_workflow_status(_runs_root, _task_id, workflow_id):
            return {"workflow_id": workflow_id, "status": "running"}

    monkeypatch.setenv("LLS_TASK_SOURCE", "local")
    monkeypatch.setattr(
        panel,
        "_apply_runtime_settings",
        lambda *args, **kwargs: apply_calls.append((args, kwargs)),
    )
    monkeypatch.setattr(panel, "_workflow_api", lambda: FakeWorkflow)
    with _panel_server(panel_workspace["runs_root"], Path("examples")) as base_url:
        status, body = _request(
            base_url,
            "/api/workflow/status?task_id=toy_multiclass_v1&workflow_id=workflow_001",
        )

    assert status == 200
    assert body == {"workflow": {"workflow_id": "workflow_001", "status": "running"}}
    assert apply_calls == []


def test_workflow_api_rejects_invalid_inputs(panel_workspace, monkeypatch):
    called = False

    def unavailable():
        nonlocal called
        called = True
        raise AssertionError("workflow engine should not be called for invalid input")

    monkeypatch.setattr(panel, "_workflow_api", unavailable)
    with _panel_server(panel_workspace["runs_root"], Path("examples")) as base_url:
        missing_task_status, _ = _request(base_url, "/api/workflow/start", method="POST", body={})
        bad_task_status, _ = _request(
            base_url,
            "/api/workflow/start",
            method="POST",
            body={"task_id": "../outside"},
        )
        dot_task_status, _ = _request(
            base_url,
            "/api/workflow/start",
            method="POST",
            body={"task_id": "."},
        )
        missing_workflow_status, _ = _request(
            base_url,
            "/api/workflow/resume",
            method="POST",
            body={"task_id": "toy_multiclass_v1"},
        )
        bad_params_status, _ = _request(
            base_url,
            "/api/workflow/start",
            method="POST",
            body={"task_id": "toy_multiclass_v1", "params": []},
        )
        bad_workflow_status, _ = _request(
            base_url,
            "/api/workflow/status?task_id=toy_multiclass_v1&workflow_id=../outside",
        )
        dot_workflow_status, _ = _request(
            base_url,
            "/api/workflow/status?task_id=toy_multiclass_v1&workflow_id=.",
        )

    assert [
        missing_task_status,
        bad_task_status,
        dot_task_status,
        missing_workflow_status,
        bad_params_status,
        bad_workflow_status,
        dot_workflow_status,
    ] == [400, 400, 400, 400, 400, 400, 400]
    assert called is False


def test_task_archive_plan_api_returns_active_assets(panel_workspace):
    with _panel_server(panel_workspace["runs_root"], Path("examples")) as base_url:
        status, body = _request(base_url, "/api/task/archive_plan?task_id=toy_multiclass_v1")

    assert status == 200
    assert body["task_id"] == "toy_multiclass_v1"
    assert body["can_archive"] is False
    assert any(item["code"] == "task_config_readonly" for item in body["blocked"])
    asset_types = {item["asset_type"] for item in body["active_assets"]}
    assert "sample" in asset_types
    assert "decision" in asset_types
    assert body["cleanup"]["r2_protected"] is True


def test_annotation_job_archive_api_moves_local_record_only(panel_workspace):
    task = panel_workspace["task"]
    annotation_dir = panel_workspace["runs_root"] / task.task_id / "annotation_jobs" / "archive_me"
    write_json(
        {
            "task_id": task.task_id,
            "annotation_id": "archive_me",
            "source": "argilla",
            "argilla_dataset": "archive_me_dataset",
        },
        annotation_dir / "manifest.json",
    )

    with _panel_server(panel_workspace["runs_root"], Path("examples")) as base_url:
        status, body = _request(
            base_url,
            "/api/annotation_job?task_id=toy_multiclass_v1&annotation_id=archive_me&reason=done",
            method="DELETE",
        )

    assert status == 200
    assert body["annotation_job"]["archived"] is True
    assert not annotation_dir.exists()
    archived_path = Path(body["annotation_job"]["archive_path"])
    assert (archived_path / "manifest.json").exists()
    assert read_json(archived_path / "manifest.json")["state"] == "archived"
    events = read_jsonl(panel_workspace["runs_root"] / task.task_id / "_audit" / "events.jsonl")
    assert any(event["event"] == "annotation_job.archive" and event["details"]["remote_affected"] is False for event in events)


def test_build_gold_from_sample_and_decisions(panel_workspace):
    task = panel_workspace["task"]
    gold_path = build_gold_from_decisions(
        task,
        panel_workspace["sample_path"],
        panel_workspace["decisions_path"],
        "from_decisions_v001",
    )

    rows = read_jsonl(gold_path)
    assert [row["record_id"] for row in rows] == ["r001", "r002"]
    assert rows[1]["title"] == "Service upgrade"
    assert rows[1]["class_label"] == "service_upgrade"
    assert rows[1]["gold_source"] == "argilla"

    manifest = read_json(gold_path.parent / "gold_from_decisions_v001.manifest.json")
    assert manifest["source"] == "decision_artifact"
    assert manifest["rows"] == 2
    assert manifest["sample_path"] == str(panel_workspace["sample_path"])


def test_build_gold_from_decisions_rejects_unknown_id_without_outputs(panel_workspace):
    task = panel_workspace["task"]
    decisions_path = panel_workspace["decisions_path"]
    write_jsonl(
        [{"record_id": "unknown", "human_label": {"class_label": "non_target"}}],
        decisions_path,
    )

    with pytest.raises(ValueError, match="未知 ID"):
        build_gold_from_decisions(
            task,
            panel_workspace["sample_path"],
            decisions_path,
            "strict_unknown_v001",
        )

    gold_dir = task.runs_dir / "gold"
    assert not any(
        path.exists()
        for path in (
            gold_dir / "gold_strict_unknown_v001.jsonl",
            gold_dir / "gold_strict_unknown_v001.manifest.json",
            gold_dir / "gold_strict_unknown_v001.data_card.md",
        )
    )


def test_build_gold_from_decisions_rejects_duplicate_id_without_outputs(panel_workspace):
    task = panel_workspace["task"]
    decisions_path = panel_workspace["decisions_path"]
    write_jsonl(
        [
            {"record_id": "r001", "human_label": {"class_label": "non_target"}},
            {"record_id": "r001", "human_label": {"class_label": "non_target"}},
        ],
        decisions_path,
    )

    with pytest.raises(ValueError, match="重复 ID"):
        build_gold_from_decisions(
            task,
            panel_workspace["sample_path"],
            decisions_path,
            "strict_duplicate_v001",
        )

    gold_dir = task.runs_dir / "gold"
    assert not (gold_dir / "gold_strict_duplicate_v001.jsonl").exists()
    assert not (gold_dir / "gold_strict_duplicate_v001.manifest.json").exists()
    assert not (gold_dir / "gold_strict_duplicate_v001.data_card.md").exists()


def test_build_gold_from_decisions_rejects_missing_primary_label_without_outputs(panel_workspace):
    task = panel_workspace["task"]
    decisions_path = panel_workspace["decisions_path"]
    write_jsonl(
        [{"record_id": "r001", "human_label": {"is_target": 0}}],
        decisions_path,
    )

    with pytest.raises(ValueError, match="缺少主标签"):
        build_gold_from_decisions(
            task,
            panel_workspace["sample_path"],
            decisions_path,
            "strict_missing_primary_v001",
        )

    gold_dir = task.runs_dir / "gold"
    assert not (gold_dir / "gold_strict_missing_primary_v001.jsonl").exists()
    assert not (gold_dir / "gold_strict_missing_primary_v001.manifest.json").exists()
    assert not (gold_dir / "gold_strict_missing_primary_v001.data_card.md").exists()


def _prepare_gold_run(task, run_dir, rows, *, batch_size=1, overlap_rate=0.0):
    sample_path = run_dir / "sample.jsonl"
    write_jsonl(rows, sample_path)
    batches = batch_records(
        sample_path,
        run_dir / "input",
        batch_size,
        overlap_rate=overlap_rate,
        min_annotators_per_overlap_item=2,
        id_field=task.id_field,
    )
    for batch in batches:
        write_json(
            {
                "results": [
                    {task.id_field: row[task.id_field], "class_label": "non_target", "is_target": 0}
                    for row in read_jsonl(batch)
                ]
            },
            run_dir / "llm" / f"{batch.stem}.json",
        )
    assert audit_run(task, run_dir)["is_usable"]
    assert merge_run(task, run_dir)["is_usable"]


def test_build_gold_allows_identical_cross_batch_overlap(panel_workspace):
    task = panel_workspace["task"]
    run_dir = panel_workspace["run_dir"]
    _prepare_gold_run(
        task,
        run_dir,
        [
            {"record_id": "r001", "title": "General notice"},
            {"record_id": "r002", "title": "Service upgrade"},
        ],
        overlap_rate=0.5,
    )

    gold_path = gold_module.build_gold(task, run_dir, "strict_overlap_v001")

    assert len(read_jsonl(gold_path)) == 2


def test_build_gold_rejects_conflicting_cross_batch_duplicate(panel_workspace):
    task = panel_workspace["task"]
    run_dir = panel_workspace["run_dir"]
    _prepare_gold_run(
        task,
        run_dir,
        [
            {"record_id": "r001", "title": "General notice"},
            {"record_id": "r002", "title": "Service upgrade"},
        ],
        overlap_rate=0.5,
    )
    manifest_path = run_dir / "input" / "manifest.json"
    manifest = read_json(manifest_path)
    overlapping_batch = next(entry for entry in manifest["batches"] if entry["overlap_rows"])
    batch_path = run_dir / "input" / "batches" / overlapping_batch["batch"]
    batch_rows = read_jsonl(batch_path)
    overlapping_id = overlapping_batch["overlap_item_ids"][0]
    next(row for row in batch_rows if row[task.id_field] == overlapping_id)["title"] = "Conflicting source"
    write_jsonl(batch_rows, batch_path)
    overlapping_batch["sha256"] = sha256_file(batch_path)
    write_json(manifest, manifest_path)
    assert audit_run(task, run_dir)["is_usable"]
    assert merge_run(task, run_dir)["is_usable"]

    with pytest.raises(ValueError, match="输入批次跨批重复 ID 内容不一致"):
        gold_module.build_gold(task, run_dir, "strict_overlap_conflict_v001")


def test_build_gold_rejects_duplicate_merged_id(panel_workspace):
    task = panel_workspace["task"]
    run_dir = panel_workspace["run_dir"]
    _prepare_gold_run(
        task,
        run_dir,
        [
            {"record_id": "r001", "title": "General notice"},
            {"record_id": "r002", "title": "Service upgrade"},
        ],
    )
    merged_path = run_dir / "merged" / "merged_clean.jsonl"
    write_jsonl(
        [
            {"record_id": "r001", "class_label": "non_target"},
            {"record_id": "r001", "class_label": "service_upgrade"},
        ],
        merged_path,
    )
    merge_summary_path = run_dir / "merged" / "merge_summary.json"
    merge_summary = read_json(merge_summary_path)
    merge_summary["merged_sha256"] = sha256_file(merged_path)
    write_json(merge_summary, merge_summary_path)

    with pytest.raises(ValueError, match="merged_clean 存在重复 ID"):
        gold_module.build_gold(task, run_dir, "strict_merged_duplicate_v001")


@pytest.mark.parametrize(
    "existing_name",
    [
        "gold_strict_existing_v001.jsonl",
        "gold_strict_existing_v001.manifest.json",
        "gold_strict_existing_v001.data_card.md",
    ],
)
def test_build_gold_from_decisions_rejects_any_existing_output(panel_workspace, existing_name):
    task = panel_workspace["task"]
    gold_dir = task.runs_dir / "gold"
    existing_path = gold_dir / existing_name
    existing_path.write_text("existing\n", encoding="utf-8")

    with pytest.raises(FileExistsError, match="拒绝覆盖"):
        build_gold_from_decisions(
            task,
            panel_workspace["sample_path"],
            panel_workspace["decisions_path"],
            "strict_existing_v001",
        )

    assert existing_path.read_text(encoding="utf-8") == "existing\n"
    output_paths = {
        gold_dir / "gold_strict_existing_v001.jsonl",
        gold_dir / "gold_strict_existing_v001.manifest.json",
        gold_dir / "gold_strict_existing_v001.data_card.md",
    }
    assert {path for path in output_paths if path.exists()} == {existing_path}


def test_build_gold_from_decisions_does_not_publish_partial_outputs(panel_workspace, monkeypatch):
    task = panel_workspace["task"]

    def fail_manifest_write(*args, **kwargs):
        raise OSError("manifest storage unavailable")

    monkeypatch.setattr(gold_module, "write_json", fail_manifest_write)

    with pytest.raises(OSError, match="manifest storage unavailable"):
        build_gold_from_decisions(
            task,
            panel_workspace["sample_path"],
            panel_workspace["decisions_path"],
            "strict_atomic_v001",
        )

    gold_dir = task.runs_dir / "gold"
    assert not any(gold_dir.glob("gold_strict_atomic_v001.*"))
    assert not any(gold_dir.glob(".gold_strict_atomic_v001_*"))


def test_build_gold_cleans_published_files_when_publish_fails(panel_workspace, monkeypatch):
    task = panel_workspace["task"]
    real_link = gold_module.os.link
    calls = 0

    def fail_on_second_link(source, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("gold publication unavailable")
        return real_link(source, target)

    monkeypatch.setattr(gold_module.os, "link", fail_on_second_link)

    with pytest.raises(OSError, match="gold publication unavailable"):
        build_gold_from_decisions(
            task,
            panel_workspace["sample_path"],
            panel_workspace["decisions_path"],
            "strict_publish_failure_v001",
        )

    gold_dir = task.runs_dir / "gold"
    assert not any(gold_dir.glob("gold_strict_publish_failure_v001.*"))
    assert not any(gold_dir.glob(".gold_strict_publish_failure_v001_*"))
