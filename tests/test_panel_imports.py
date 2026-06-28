from __future__ import annotations

import base64
import hashlib
import json
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
import threading
import time
import urllib.error
import urllib.request

import pytest
import yaml

from llm_labeling_scaffold import data_lake, panel, pipeline
from llm_labeling_scaffold.io import read_json, write_json


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _clear_import_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "LLS_TASK_SOURCE",
        "LLS_TASK_REGISTRY_URI",
        "LLS_DATA_LAKE_R2_PREFIX",
        "LLS_PANEL_SETTINGS_PATH",
        "LLS_ALLOW_DATA_LAKE_OVERRIDES",
        "LLS_ALLOW_LOCAL_DATA_LAKE_URIS",
        "LLS_ALLOW_MANUAL_IMPORTS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(data_lake, "_DEFAULT_REGISTRY_URI_OVERRIDE", None)
    monkeypatch.setattr(data_lake, "_ALLOWED_R2_PREFIX_OVERRIDE", None)


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


def _request(base_url: str, path: str, *, method: str = "GET", payload=None) -> tuple[int, dict]:
    headers = {
        "Authorization": "Basic " + base64.b64encode(b"admin:secret").decode("ascii"),
    }
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(base_url + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _create_manual_task(tasks_root: Path) -> dict:
    return pipeline.create_task(
        tasks_root,
        {
            "task_id": "manual_task",
            "id_field": "record_id",
            "text_fields": ["title"],
            "primary_label_name": "label",
            "primary_label_values": ["yes", "no"],
        },
    )


def _create_data_lake_task(tmp_path: Path, tasks_root: Path) -> tuple[str, Path]:
    source = tmp_path / "lake_source.jsonl"
    source.write_text(
        '{"record_id":"r1","title":"A"}\n{"record_id":"r2","title":"B"}\n',
        encoding="utf-8",
    )
    manifest_path = tmp_path / "lake_manifest.json"
    write_json(
        {
            "dataset_id": "lake_seed",
            "layer": "labels",
            "domain": "patent",
            "objects": [
                {
                    "path": "inputs/manual_seed/v1/raw.jsonl",
                    "storage_uri": str(source),
                    "asset_type": "label_import_jsonl",
                    "rows": 2,
                    "id_field": "record_id",
                    "unique_ids": 2,
                    "bytes": source.stat().st_size,
                    "sha256": _file_sha256(source),
                    "created_by": "tests",
                    "upstream_uri": ["r2:test/upstream/source.jsonl"],
                    "sampling_strategy": "unit_test_seed",
                }
            ],
        },
        manifest_path,
    )
    registry_path = tmp_path / "data_lake.yaml"
    registry_path.write_text(
        yaml.safe_dump({"datasets": {"lake_seed": {"manifest": str(manifest_path)}}}, allow_unicode=True),
        encoding="utf-8",
    )
    created = pipeline.create_task(
        tasks_root,
        {
            "task_id": "lake_task",
            "id_field": "record_id",
            "text_fields": ["title"],
            "primary_label_name": "label",
            "primary_label_values": ["yes", "no"],
            "data_lake": {
                "lake_registry_uri": str(registry_path),
                "source_dataset_id": "lake_seed",
                "source_object_path": "inputs/manual_seed/v1/raw.jsonl",
                "default_import_id": "lake_import",
            },
        },
    )
    return str(created["task_id"]), source


def _wait_for_job(base_url: str, task_id: str, job_id: str) -> dict:
    current = None
    for _ in range(100):
        status, payload = _request(base_url, f"/api/jobs?task_id={task_id}")
        assert status == 200
        current = next((item for item in payload["jobs"] if item["id"] == job_id), None)
        if current and current["status"] in {"succeeded", "failed"}:
            return current
        time.sleep(0.05)
    raise AssertionError(f"job did not finish: {job_id} {current}")


def test_r2_task_source_rejects_manual_import(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _clear_import_env(monkeypatch)
    monkeypatch.setenv("LLS_TASK_SOURCE", "r2")
    monkeypatch.setenv("LLS_ALLOW_MANUAL_IMPORTS", "true")

    with _panel_server(tmp_path / "runs", tmp_path / "tasks") as base_url:
        status, settings = _request(base_url, "/api/settings")
        assert status == 200
        assert settings["settings"]["allow_manual_imports"] is False

        status, payload = _request(
            base_url,
            "/api/import?task_id=manual_task&name=manual_seed",
            method="POST",
            payload=[{"record_id": "r1", "title": "A"}],
        )

    assert status == 400
    assert "生产模式不允许手动上传或粘贴导入" in payload["error"]


def test_local_task_source_allows_manual_import(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _clear_import_env(monkeypatch)
    runs_root = tmp_path / "runs"
    tasks_root = tmp_path / "tasks"
    _create_manual_task(tasks_root)

    with _panel_server(runs_root, tasks_root) as base_url:
        status, payload = _request(
            base_url,
            "/api/import?task_id=manual_task&name=manual_seed",
            method="POST",
            payload=[{"record_id": "r1", "title": "A"}],
        )

    assert status == 200
    assert payload["import"]["import_id"] == "manual_seed"
    assert (runs_root / "manual_task" / "imports" / "manual_seed" / "manifest.json").exists()


def test_jobs_api_reports_failed_background_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _clear_import_env(monkeypatch)
    runs_root = tmp_path / "runs"
    tasks_root = tmp_path / "tasks"
    created = _create_manual_task(tasks_root)

    with _panel_server(runs_root, tasks_root) as base_url:
        status, payload = _request(
            base_url,
            "/api/action",
            method="POST",
            payload={
                "task": created["path"],
                "action": "batch",
                "params": {"batch_size": 1},
            },
        )
        assert status == 200
        job_id = payload["job"]["id"]

        failed = _wait_for_job(base_url, "manual_task", job_id)

    assert failed["status"] == "failed"
    assert "KeyError" in failed["error"]
    assert "sample" in failed["error"]


def test_data_lake_import_rejects_source_override_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _clear_import_env(monkeypatch)

    with _panel_server(tmp_path / "runs", tmp_path / "tasks") as base_url:
        status, payload = _request(
            base_url,
            "/api/import/data_lake",
            method="POST",
            payload={"task_id": "lake_task", "source_object_path": "inputs/other/raw.jsonl"},
        )

    assert status == 400
    assert "生产模式不允许覆盖数据湖来源" in payload["error"]


def test_data_lake_import_dry_run_returns_summary_without_writing_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _clear_import_env(monkeypatch)
    monkeypatch.setenv("LLS_ALLOW_LOCAL_DATA_LAKE_URIS", "1")
    runs_root = tmp_path / "runs"
    tasks_root = tmp_path / "tasks"
    task_id, source = _create_data_lake_task(tmp_path, tasks_root)

    with _panel_server(runs_root, tasks_root) as base_url:
        status, payload = _request(
            base_url,
            "/api/import/data_lake",
            method="POST",
            payload={"task_id": task_id, "dry_run": True, "import_id": "lake_import"},
        )

    assert status == 200
    assert payload["ok"] is True
    dry_run = payload["dry_run"]
    assert dry_run["import_id"] == "lake_import"
    assert dry_run["task"]["task_id"] == task_id
    assert dry_run["source"]["source_object_sha256"] == _file_sha256(source)
    assert dry_run["manifest"]["selected_object"]["rows"] == 2
    assert dry_run["validation"]["ok"] is True
    assert dry_run["plan"]["action"] == "create"
    assert not (runs_root / task_id / "imports" / "lake_import").exists()


def test_data_lake_import_submit_requires_confirm_and_idempotency_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _clear_import_env(monkeypatch)
    monkeypatch.setenv("LLS_ALLOW_LOCAL_DATA_LAKE_URIS", "1")
    runs_root = tmp_path / "runs"
    tasks_root = tmp_path / "tasks"
    task_id, _source = _create_data_lake_task(tmp_path, tasks_root)

    with _panel_server(runs_root, tasks_root) as base_url:
        status, payload = _request(
            base_url,
            "/api/import/data_lake",
            method="POST",
            payload={"task_id": task_id, "idempotency_key": "lake-submit-1"},
        )
        assert status == 400
        assert "confirm=true" in payload["error"]

        status, payload = _request(
            base_url,
            "/api/import/data_lake",
            method="POST",
            payload={"task_id": task_id, "confirm": True},
        )

    assert status == 400
    assert "idempotency_key" in payload["error"]


def test_data_lake_import_idempotency_key_reuses_existing_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _clear_import_env(monkeypatch)
    monkeypatch.setenv("LLS_ALLOW_LOCAL_DATA_LAKE_URIS", "1")
    runs_root = tmp_path / "runs"
    tasks_root = tmp_path / "tasks"
    task_id, _source = _create_data_lake_task(tmp_path, tasks_root)

    payload = {"task_id": task_id, "confirm": True, "idempotency_key": "lake-submit-1"}
    with _panel_server(runs_root, tasks_root) as base_url:
        first_status, first = _request(base_url, "/api/import/data_lake", method="POST", payload=payload)
        second_status, second = _request(base_url, "/api/import/data_lake", method="POST", payload=payload)
        assert first_status == 200
        assert second_status == 200
        assert second["job"]["id"] == first["job"]["id"]
        assert second["job"]["idempotent_submit"] is True
        job = _wait_for_job(base_url, task_id, first["job"]["id"])

    assert job["status"] == "succeeded"
    assert job["result"]["import_id"] == "lake_import"


def test_data_lake_import_idempotency_key_rejects_different_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _clear_import_env(monkeypatch)
    monkeypatch.setenv("LLS_ALLOW_LOCAL_DATA_LAKE_URIS", "1")
    runs_root = tmp_path / "runs"
    tasks_root = tmp_path / "tasks"
    task_id, _source = _create_data_lake_task(tmp_path, tasks_root)

    with _panel_server(runs_root, tasks_root) as base_url:
        status, _payload = _request(
            base_url,
            "/api/import/data_lake",
            method="POST",
            payload={
                "task_id": task_id,
                "confirm": True,
                "idempotency_key": "lake-submit-1",
                "import_id": "first_import",
            },
        )
        assert status == 200

        status, payload = _request(
            base_url,
            "/api/import/data_lake",
            method="POST",
            payload={
                "task_id": task_id,
                "confirm": True,
                "idempotency_key": "lake-submit-1",
                "import_id": "second_import",
            },
        )

    assert status == 400
    assert "幂等 key" in payload["error"]


def test_data_lake_import_api_returns_job_and_writes_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _clear_import_env(monkeypatch)
    monkeypatch.setenv("LLS_ALLOW_LOCAL_DATA_LAKE_URIS", "1")
    runs_root = tmp_path / "runs"
    tasks_root = tmp_path / "tasks"
    task_id, source = _create_data_lake_task(tmp_path, tasks_root)

    with _panel_server(runs_root, tasks_root) as base_url:
        status, payload = _request(
            base_url,
            "/api/import/data_lake",
            method="POST",
            payload={"task_id": task_id, "confirm": True, "idempotency_key": "lake-submit-1"},
        )
        assert status == 200
        assert payload["job"]["kind"] == "data_lake_import"
        assert payload["job"]["status"] in {"pending", "running", "succeeded"}

        job = _wait_for_job(base_url, task_id, payload["job"]["id"])
        status, payload = _request(
            base_url,
            "/api/import/data_lake",
            method="POST",
            payload={"task_id": task_id, "confirm": True, "idempotency_key": "lake-submit-2"},
        )
        assert status == 200
        reused_job = _wait_for_job(base_url, task_id, payload["job"]["id"])

    assert job["status"] == "succeeded"
    assert job["result"]["import_id"] == "lake_import"
    assert reused_job["status"] == "succeeded"
    assert reused_job["result"]["action"] == "reused"
    manifest = read_json(runs_root / task_id / "imports" / "lake_import" / "manifest.json")
    assert manifest["source"] == "data_lake"
    assert manifest["source_object_sha256"] == _file_sha256(source)
