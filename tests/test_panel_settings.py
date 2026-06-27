from __future__ import annotations

import base64
import json
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
import threading
import urllib.error
import urllib.request

import pytest

from llm_labeling_scaffold import data_lake, panel, panel_settings, task_registry
from llm_labeling_scaffold.io import read_json


def _clear_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "LLS_TASK_SOURCE",
        "LLS_TASK_REGISTRY_URI",
        "LLS_DATA_LAKE_R2_PREFIX",
        "LLS_PANEL_SETTINGS_PATH",
        "LLS_ALLOW_DATA_LAKE_OVERRIDES",
        "RCLONE_CONFIG",
        "LLS_RCLONE_CONFIG",
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


def test_settings_api_saves_and_reads_runtime_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _clear_settings_env(monkeypatch)
    runs_root = tmp_path / "runs"
    tasks_root = tmp_path / "tasks"

    with _panel_server(runs_root, tasks_root) as base_url:
        status, payload = _request(base_url, "/api/settings")
        assert status == 200
        assert payload["settings"] == {
            "task_source": "local",
            "task_registry_uri": "",
            "data_lake_r2_prefix": "",
            "allow_data_lake_overrides": False,
            "rclone_config_path": None,
        }
        assert "ai-innovation" not in json.dumps(payload, ensure_ascii=False)

        status, payload = _request(
            base_url,
            "/api/settings",
            method="POST",
            payload={
                "task_registry_uri": "r2:tenant/governance/data_lake.yaml",
                "data_lake_r2_prefix": "r2:tenant/lake",
            },
        )
        assert status == 200
        assert payload["ok"] is True
        assert payload["settings"]["task_registry_uri"] == "r2:tenant/governance/data_lake.yaml"
        assert payload["settings"]["data_lake_r2_prefix"] == "r2:tenant/lake/"

        stored = read_json(runs_root / "_system" / "panel_settings.json")
        assert stored == {
            "task_registry_uri": "r2:tenant/governance/data_lake.yaml",
            "data_lake_r2_prefix": "r2:tenant/lake/",
        }

        status, payload = _request(base_url, "/api/config")
        assert status == 200
        assert payload["settings"]["task_registry_uri"] == "r2:tenant/governance/data_lake.yaml"
        assert payload["settings"]["data_lake_r2_prefix"] == "r2:tenant/lake/"
        assert payload["task_source"] == "local"
        assert payload["task_registry_uri"] is None

        status, payload = _request(
            base_url,
            "/api/settings",
            method="POST",
            payload={"task_registry_uri": "", "data_lake_r2_prefix": ""},
        )
        assert status == 200
        assert payload["settings"]["task_registry_uri"] == ""
        assert payload["settings"]["data_lake_r2_prefix"] == ""
        assert "ai-innovation" not in json.dumps(payload, ensure_ascii=False)
        assert read_json(runs_root / "_system" / "panel_settings.json") == {}


def test_settings_api_reads_env_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _clear_settings_env(monkeypatch)
    runs_root = tmp_path / "runs"
    tasks_root = tmp_path / "tasks"
    monkeypatch.setenv("LLS_TASK_REGISTRY_URI", "r2:env/governance/data_lake.yaml")
    monkeypatch.setenv("LLS_DATA_LAKE_R2_PREFIX", "r2:env/lake")

    with _panel_server(runs_root, tasks_root) as base_url:
        status, payload = _request(base_url, "/api/settings")

    assert status == 200
    assert payload["settings"]["task_registry_uri"] == "r2:env/governance/data_lake.yaml"
    assert payload["settings"]["data_lake_r2_prefix"] == "r2:env/lake/"


def test_settings_api_reads_stored_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _clear_settings_env(monkeypatch)
    runs_root = tmp_path / "runs"
    tasks_root = tmp_path / "tasks"
    panel_settings.update_settings(
        runs_root,
        {
            "task_registry_uri": "r2:stored/governance/data_lake.yaml",
            "data_lake_r2_prefix": "r2:stored/lake",
        },
    )

    with _panel_server(runs_root, tasks_root) as base_url:
        status, payload = _request(base_url, "/api/settings")

    assert status == 200
    assert payload["settings"]["task_registry_uri"] == "r2:stored/governance/data_lake.yaml"
    assert payload["settings"]["data_lake_r2_prefix"] == "r2:stored/lake/"


@pytest.mark.parametrize(
    "payload",
    [
        {"task_registry_uri": "../registry.yaml"},
        {"task_registry_uri": "file:///tmp/data_lake.yaml"},
        {"task_registry_uri": "r2:tenant/../data_lake.yaml"},
        {"data_lake_r2_prefix": "r2:tenant/../lake"},
        {"data_lake_r2_prefix": "/tmp/lake"},
        {"allow_data_lake_overrides": True},
        ["not", "an", "object"],
    ],
)
def test_settings_api_rejects_invalid_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload):
    _clear_settings_env(monkeypatch)
    with _panel_server(tmp_path / "runs", tmp_path / "tasks") as base_url:
        status, response = _request(base_url, "/api/settings", method="POST", payload=payload)

    assert status == 400
    assert response["error"]


def test_r2_task_sync_prefers_settings_registry_uri(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _clear_settings_env(monkeypatch)
    runs_root = tmp_path / "runs"
    tasks_root = tmp_path / "tasks"
    monkeypatch.setenv("LLS_TASK_SOURCE", "r2")
    monkeypatch.setenv("LLS_TASK_REGISTRY_URI", "r2:env/governance/data_lake.yaml")
    panel_settings.update_settings(runs_root, {"task_registry_uri": "r2:settings/governance/data_lake.yaml"})
    calls: dict[str, object] = {}

    def fake_sync(root, registry_uri=None):
        calls["tasks_root"] = root
        calls["registry_uri"] = registry_uri
        return SimpleNamespace(registry_uri=registry_uri, registry={}, tasks={})

    monkeypatch.setattr(task_registry, "sync_tasks_from_registry", fake_sync)

    handler = SimpleNamespace(runs_root=runs_root, tasks_root=tasks_root)
    synced = panel._Handler._sync_tasks_if_needed(handler)

    assert synced.registry_uri == "r2:settings/governance/data_lake.yaml"
    assert calls == {
        "tasks_root": tasks_root,
        "registry_uri": "r2:settings/governance/data_lake.yaml",
    }


def test_runtime_settings_override_data_lake_prefix_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _clear_settings_env(monkeypatch)
    runs_root = tmp_path / "runs"
    panel_settings.update_settings(runs_root, {"data_lake_r2_prefix": "r2:tenant/lake"})

    settings = panel._apply_runtime_settings(runs_root)

    assert settings["data_lake_r2_prefix"] == "r2:tenant/lake/"
    data_lake._validate_rclone_uri("r2:tenant/lake/object.jsonl")
    with pytest.raises(data_lake.DataLakeError):
        data_lake._validate_rclone_uri("r2:other/lake/object.jsonl")


def test_runtime_settings_keeps_internal_data_lake_defaults_unconfigured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _clear_settings_env(monkeypatch)

    settings = panel._apply_runtime_settings(tmp_path / "runs")

    assert settings["task_registry_uri"] == ""
    assert settings["data_lake_r2_prefix"] == ""
    assert data_lake.default_registry_uri() == data_lake.DEFAULT_REGISTRY_URI
    assert data_lake._allowed_r2_prefix() == data_lake.DEFAULT_R2_PREFIX
