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

from llm_labeling_scaffold import panel
from llm_labeling_scaffold import pipeline
from llm_labeling_scaffold.gold import build_gold_from_decisions
from llm_labeling_scaffold.io import read_json, read_jsonl


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
        "auth_user": panel._Handler.auth_user,
        "auth_pass": panel._Handler.auth_pass,
    }
    panel._Handler.runs_root = runs_root
    panel._Handler.tasks_root = tasks_root
    panel._Handler.static_dir = None
    panel._Handler.auth_user = "admin"
    panel._Handler.auth_pass = "secret"
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
    assert artifacts == [
        {
            "task_id": "toy_multiclass_v1",
            "decision_id": "argilla_round_1",
            "source": "argilla",
            "argilla_dataset": "toy_argilla_round_1",
            "sample_id": "sample_a",
            "sample_path": str(panel_workspace["sample_path"]),
            "path": str(panel_workspace["decisions_path"]),
            "rows": 2,
        }
    ]


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
