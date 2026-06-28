from __future__ import annotations

import base64
import hashlib
import json
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
import threading
import urllib.error
import urllib.request

import pytest
import yaml

from llm_labeling_scaffold import data_lake, panel, panel_settings, pipeline, task_registry
from llm_labeling_scaffold.io import read_json, write_json
from llm_labeling_scaffold.profiles import DEFAULT_PROFILE, QUALITY_CONTROL_PROFILE


def _clear_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "LLS_TASK_SOURCE",
        "LLS_TASK_REGISTRY_URI",
        "LLS_TASK_REGISTRY_SYNC_TTL_SECONDS",
        "LLS_DATA_LAKE_R2_PREFIX",
        "LLS_PANEL_SETTINGS_PATH",
        "LLS_ALLOW_DATA_LAKE_OVERRIDES",
        "LLS_ALLOW_MANUAL_IMPORTS",
        "LLS_ALLOW_LOCAL_DATA_LAKE_URIS",
        "RCLONE_CONFIG",
        "LLS_RCLONE_CONFIG",
        "LLS_RCLONE_BIN",
        "ARGILLA_API_KEY",
        "LLS_ARGILLA_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(data_lake, "_DEFAULT_REGISTRY_URI_OVERRIDE", None)
    monkeypatch.setattr(data_lake, "_ALLOWED_R2_PREFIX_OVERRIDE", None)
    panel._invalidate_task_registry_sync_cache()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _create_data_lake_contract_task(tmp_path: Path, tasks_root: Path, *, task_id: str = "contract_lake_task") -> dict:
    source = tmp_path / f"{task_id}_source.jsonl"
    source.write_text('{"record_id":"r1","title":"A"}\n{"record_id":"r2","title":"B"}\n', encoding="utf-8")
    manifest_path = tmp_path / f"{task_id}_manifest.json"
    object_path = "inputs/manual_seed/v1/raw.jsonl"
    write_json(
        {
            "dataset_id": f"{task_id}_seed",
            "layer": "labels",
            "domain": "tests",
            "object_count": 1,
            "total_bytes": source.stat().st_size,
            "objects": [
                {
                    "path": object_path,
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
    registry_path = tmp_path / f"{task_id}_data_lake.yaml"
    registry_path.write_text(
        yaml.safe_dump(
            {
                "datasets": {
                    f"{task_id}_seed": {
                        "manifest": str(manifest_path),
                        "layer": "labels",
                        "domain": "tests",
                    }
                }
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return pipeline.create_task(
        tasks_root,
        {
            "task_id": task_id,
            "id_field": "record_id",
            "text_fields": ["title"],
            "primary_label_name": "label",
            "primary_label_values": ["yes", "no"],
            "data_lake": {
                "lake_registry_uri": str(registry_path),
                "source_dataset_id": f"{task_id}_seed",
                "source_object_path": object_path,
                "default_import_id": "lake_import",
            },
        },
    )


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
            "allow_manual_imports": True,
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


def test_contract_metadata_and_public_settings_do_not_leak_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _clear_settings_env(monkeypatch)
    runs_root = tmp_path / "runs"
    tasks_root = tmp_path / "tasks"
    monkeypatch.setenv("LLS_TASK_REGISTRY_URI", "r2:tenant/governance/data_lake.yaml")
    monkeypatch.setenv("LLS_DATA_LAKE_R2_PREFIX", "r2:tenant/lake")
    monkeypatch.setenv("RCLONE_CONFIG", "/very/secret/rclone.conf")
    monkeypatch.setenv("LLS_ARGILLA_API_KEY", "argilla-secret-value")

    with _panel_server(runs_root, tasks_root) as base_url:
        status, health = _request(base_url, "/api/health")
        assert status == 200
        assert health == {"ok": True, "status": "ok", "service": "llm-labeling-scaffold"}

        status, version = _request(base_url, "/api/version")
        assert status == 200
        assert version["version"]
        assert version["api_contract_version"] == panel.API_CONTRACT_VERSION

        status, capabilities = _request(base_url, "/api/capabilities")
        assert status == 200
        advertised = {(item["method"], item["path"], item["action"]) for item in capabilities["endpoints"]}
        assert ("GET", "/api/settings/public", "settings_public") in advertised
        assert ("POST", "/api/tasks/{task_id}/check", "task_check") in advertised
        assert ("GET", "/api/task/annotation_jobs", "annotation_jobs_list") in advertised
        assert ("GET", "/api/annotation_job/detail", "annotation_job_detail") in advertised
        assert ("GET", "/api/task/decision_artifacts", "decision_artifacts_list") in advertised
        assert ("GET", "/api/decision_artifact/detail", "decision_artifact_detail") in advertised
        assert ("GET", "/api/task/gold_versions", "gold_versions_list") in advertised
        assert ("GET", "/api/gold_version/detail", "gold_version_detail") in advertised

        status, public = _request(base_url, "/api/settings/public")

    assert status == 200
    assert public["settings"] == {
        "task_source": "local",
        "allow_manual_imports": True,
        "allow_data_lake_overrides": False,
        "task_registry_configured": True,
        "data_lake_r2_prefix_configured": True,
        "rclone_configured": True,
    }
    serialized = json.dumps(public, ensure_ascii=False)
    assert "r2:tenant" not in serialized
    assert "/very/secret" not in serialized
    assert "argilla-secret-value" not in serialized
    assert "rclone_config_path" not in serialized


def test_contract_task_detail_and_check_preview_data_lake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _clear_settings_env(monkeypatch)
    monkeypatch.setenv("LLS_ALLOW_LOCAL_DATA_LAKE_URIS", "1")
    runs_root = tmp_path / "runs"
    tasks_root = tmp_path / "tasks"
    _create_data_lake_contract_task(tmp_path, tasks_root)

    with _panel_server(runs_root, tasks_root) as base_url:
        status, detail = _request(base_url, "/api/tasks/contract_lake_task")
        assert status == 200
        assert detail["task"]["task_id"] == "contract_lake_task"
        assert detail["task"]["profile"]["valid"] is True
        assert detail["task"]["data_lake"] == {
            "configured": True,
            "source_dataset_id": "contract_lake_task_seed",
            "source_object_path": "inputs/manual_seed/v1/raw.jsonl",
            "lake_registry_uri_configured": True,
            "source_manifest_uri_configured": False,
            "output_base_uri_configured": False,
        }

        status, check = _request(base_url, "/api/tasks/contract_lake_task/check", method="POST", payload={})

    assert status == 200
    assert check["ok"] is True
    assert check["errors"] == []
    by_name = {item["name"]: item for item in check["checks"]}
    assert by_name["task_load"]["status"] == "ok"
    assert by_name["profile"]["status"] == "ok"
    assert by_name["data_lake_config"]["status"] == "ok"
    assert by_name["data_lake_preview"]["status"] == "ok"
    assert by_name["data_lake_preview"]["details"]["preview"]["selected_object"]["rows"] == 2


def test_contract_task_check_reports_unreachable_data_lake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _clear_settings_env(monkeypatch)
    runs_root = tmp_path / "runs"
    tasks_root = tmp_path / "tasks"
    pipeline.create_task(
        tasks_root,
        {
            "task_id": "r2_unreachable_task",
            "id_field": "record_id",
            "text_fields": ["title"],
            "primary_label_name": "label",
            "primary_label_values": ["yes", "no"],
            "data_lake": {
                "lake_registry_uri": "r2:tenant/governance/data_lake.yaml",
                "source_dataset_id": "seed",
                "source_object_path": "inputs/raw.jsonl",
            },
        },
    )
    monkeypatch.setenv("LLS_DATA_LAKE_R2_PREFIX", "r2:tenant/")
    monkeypatch.setenv("LLS_RCLONE_BIN", "missing-rclone-for-contract-test")

    with _panel_server(runs_root, tasks_root) as base_url:
        status, check = _request(base_url, "/api/tasks/r2_unreachable_task/check", method="POST", payload={})

    assert status == 200
    assert check["ok"] is False
    assert any(item["name"] == "data_lake_preview" and item["status"] == "error" for item in check["checks"])
    assert check["errors"]
    assert "rclone" in check["errors"][-1]["message"]


def test_r2_task_source_forces_manual_imports_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _clear_settings_env(monkeypatch)
    monkeypatch.setenv("LLS_TASK_SOURCE", "r2")
    monkeypatch.setenv("LLS_ALLOW_MANUAL_IMPORTS", "true")

    with _panel_server(tmp_path / "runs", tmp_path / "tasks") as base_url:
        status, payload = _request(base_url, "/api/settings")

    assert status == 200
    assert panel_settings.allow_manual_imports() is False
    assert payload["settings"]["task_source"] == "r2"
    assert payload["settings"]["allow_manual_imports"] is False


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


def test_r2_task_sync_reuses_ttl_cache_and_expires(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _clear_settings_env(monkeypatch)
    runs_root = tmp_path / "runs"
    tasks_root = tmp_path / "tasks"
    monkeypatch.setenv("LLS_TASK_SOURCE", "r2")
    monkeypatch.setenv("LLS_TASK_REGISTRY_URI", "r2:env/governance/data_lake.yaml")
    monkeypatch.setenv("LLS_TASK_REGISTRY_SYNC_TTL_SECONDS", "5")
    clock = [100.0]
    calls: list[tuple[Path, str | None]] = []

    def fake_sync(root, registry_uri=None):
        calls.append((root, registry_uri))
        return SimpleNamespace(registry_uri=registry_uri, registry={}, tasks={}, sequence=len(calls))

    monkeypatch.setattr(panel.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(task_registry, "sync_tasks_from_registry", fake_sync)

    handler = SimpleNamespace(runs_root=runs_root, tasks_root=tasks_root)
    first = panel._Handler._sync_tasks_if_needed(handler)
    clock[0] = 104.9
    second = panel._Handler._sync_tasks_if_needed(handler)
    clock[0] = 105.1
    third = panel._Handler._sync_tasks_if_needed(handler)

    assert first is second
    assert third is not first
    assert [item[1] for item in calls] == [
        "r2:env/governance/data_lake.yaml",
        "r2:env/governance/data_lake.yaml",
    ]
    assert third.sequence == 2


def test_settings_update_invalidates_r2_task_sync_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _clear_settings_env(monkeypatch)
    runs_root = tmp_path / "runs"
    tasks_root = tmp_path / "tasks"
    monkeypatch.setenv("LLS_TASK_SOURCE", "r2")
    monkeypatch.setenv("LLS_TASK_REGISTRY_SYNC_TTL_SECONDS", "60")
    panel_settings.update_settings(runs_root, {"task_registry_uri": "r2:settings/governance/data_lake.yaml"})
    calls: list[str | None] = []

    def fake_sync(root, registry_uri=None):
        calls.append(registry_uri)
        return SimpleNamespace(registry_uri=registry_uri, registry={}, tasks={})

    monkeypatch.setattr(task_registry, "sync_tasks_from_registry", fake_sync)

    with _panel_server(runs_root, tasks_root) as base_url:
        status, payload = _request(base_url, "/api/tasks")
        assert status == 200
        assert payload == {"tasks": []}

        status, payload = _request(base_url, "/api/tasks")
        assert status == 200
        assert payload == {"tasks": []}

        status, payload = _request(
            base_url,
            "/api/settings",
            method="POST",
            payload={"task_registry_uri": "r2:settings/governance/data_lake.yaml"},
        )
        assert status == 200
        assert payload["ok"] is True

        status, payload = _request(base_url, "/api/tasks")

    assert status == 200
    assert payload == {"tasks": []}
    assert calls == [
        "r2:settings/governance/data_lake.yaml",
        "r2:settings/governance/data_lake.yaml",
    ]


def test_profile_preset_api_lists_and_switches_task_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _clear_settings_env(monkeypatch)
    runs_root = tmp_path / "runs"
    tasks_root = tmp_path / "tasks"
    pipeline.create_task(
        tasks_root,
        {
            "task_id": "profile_api_task",
            "id_field": "record_id",
            "text_fields": ["title"],
            "primary_label_name": "label",
            "primary_label_values": ["yes", "no"],
        },
    )

    with _panel_server(runs_root, tasks_root) as base_url:
        status, catalog = _request(base_url, "/api/profile/presets")
        assert status == 200
        assert catalog["default_profile_id"] == DEFAULT_PROFILE
        assert [preset["id"] for preset in catalog["presets"]][:2] == [DEFAULT_PROFILE, QUALITY_CONTROL_PROFILE]
        assert all("stages" not in preset for preset in catalog["presets"])

        status, switched = _request(
            base_url,
            f"/api/task/profile?task_id=profile_api_task&preset={QUALITY_CONTROL_PROFILE}",
        )

    assert status == 200
    assert switched["task_profile_id"] == DEFAULT_PROFILE
    assert switched["selected_profile_id"] == QUALITY_CONTROL_PROFILE
    assert "pilot_calibration" in [stage["id"] for stage in switched["stages"]]


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
