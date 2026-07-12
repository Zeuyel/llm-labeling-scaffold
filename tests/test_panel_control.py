from __future__ import annotations

import base64
import json
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
import threading
import urllib.error
import urllib.request

from llm_labeling_scaffold import panel
from llm_labeling_scaffold.config import load_task


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


def _draft_spec(task_id: str = "panel_control_task") -> dict:
    return {
        "task_id": task_id,
        "id_field": "record_id",
        "text_fields": ["title"],
        "primary_label_name": "decision",
        "primary_label_values": ["accept", "reject"],
        "data_lake": {
            "source_dataset_id": "panel_dataset",
            "source_object_path": "inputs/panel.jsonl",
            "default_import_id": "panel_import",
            "output_base_uri": "outputs/panel_control_task",
        },
    }


def test_unpublished_control_draft_cannot_load_normal_task_detail(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LLS_TASK_SOURCE", "control")
    runs_root = tmp_path / "runs"
    tasks_root = tmp_path / "tasks"
    spec = _draft_spec()

    with _panel_server(runs_root, tasks_root) as base_url:
        status, created = _request(base_url, "/api/tasks", method="POST", body=spec)
        assert status == 200
        assert created["task"]["status"] == "draft"
        assert not (tasks_root / spec["task_id"] / "task.yaml").exists()

        status, tasks = _request(base_url, "/api/tasks")
        assert status == 200
        assert tasks["tasks"] == [created["task"]]

        status, detail = _request(base_url, f"/api/tasks/{spec['task_id']}")
        assert status == 404
        assert "尚未发布" in detail["error"]


def test_control_api_updates_publishes_revisions_and_rejects_delete(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LLS_TASK_SOURCE", "control")
    runs_root = tmp_path / "runs"
    tasks_root = tmp_path / "tasks"
    spec = _draft_spec()
    updated_spec = {
        **spec,
        "primary_label_values": ["accept", "reject", "review"],
    }

    with _panel_server(runs_root, tasks_root) as base_url:
        status, _ = _request(base_url, "/api/tasks", method="POST", body=spec)
        assert status == 200

        status, updated = _request(
            base_url,
            f"/api/tasks/{spec['task_id']}",
            method="PUT",
            body=updated_spec,
        )
        assert status == 200
        assert updated["action"] == "updated"
        assert updated["record"]["draft_spec"] == updated_spec

        status, first = _request(base_url, f"/api/tasks/{spec['task_id']}/publish", method="POST", body={})
        assert status == 200
        first_snapshot = Path(first["published"]["snapshot_path"])
        assert first["task"]["revision"] == 1
        assert first_snapshot.exists()
        assert load_task(first_snapshot).raw["revision"] == 1

        status, second = _request(base_url, f"/api/tasks/{spec['task_id']}/publish", method="POST", body={})
        assert status == 200
        assert second["task"]["revision"] == 2
        assert Path(second["published"]["snapshot_path"]).name == "task.yaml"
        assert Path(second["published"]["snapshot_path"]).parent.name == "revision_000002"

        status, deleted = _request(
            base_url,
            f"/api/tasks?task_id={spec['task_id']}",
            method="DELETE",
        )
        assert status == 400
        assert "控制面任务不能直接删除" in deleted["error"]
        assert (tasks_root / spec["task_id"] / "task.yaml").exists()
