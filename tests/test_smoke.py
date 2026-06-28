from __future__ import annotations

import base64
import json
from urllib.parse import urlparse

from llm_labeling_scaffold.smoke import SmokeConfig, SmokeResponse, render_summary, run_smoke


def test_smoke_summary_redacts_sensitive_values():
    summary = {
        "server_url": (
            "https://user:db-pass@example.test/path?"
            "token=query-token&api_key=query-api-key&password=query-password&secret=query-secret&safe=ok"
        ),
        "token": "super-secret-token",
        "rclone_config_path": "/home/app/.config/rclone/rclone.conf",
        "argilla_api_key": "argilla-secret-key",
        "database_password": "postgres-secret-password",
        "secret_path": "/run/secrets/panel_password",
        "checks": [
            {
                "name": "settings_public",
                "status": "failed",
                "message": (
                    "Authorization: Bearer super-secret-token "
                    "ARGILLA_API_KEY=argilla-secret-key "
                    "DATABASE_PASSWORD=postgres-secret-password "
                    "RCLONE_CONFIG=/home/app/.config/rclone/rclone.conf "
                    "SECRET_PATH=/run/secrets/panel_password"
                ),
            }
        ],
        "errors": [
            "token=inline-token api_key=inline-api-key password=inline-password secret=inline-secret"
        ],
    }

    rendered = render_summary(summary, "json")

    assert "db-pass" not in rendered
    assert "super-secret-token" not in rendered
    assert "query-token" not in rendered
    assert "query-api-key" not in rendered
    assert "query-password" not in rendered
    assert "query-secret" not in rendered
    assert "inline-token" not in rendered
    assert "inline-api-key" not in rendered
    assert "inline-password" not in rendered
    assert "inline-secret" not in rendered
    assert "argilla-secret-key" not in rendered
    assert "postgres-secret-password" not in rendered
    assert "rclone.conf" not in rendered
    assert "/run/secrets/panel_password" not in rendered
    assert "<redacted>" in rendered


def test_smoke_runner_organizes_discovery_task_and_import_dry_run_requests():
    calls = []

    def fake_transport(method, url, headers, body, timeout):
        payload = json.loads(body.decode("utf-8")) if body else None
        path = urlparse(url).path
        calls.append((method, path, headers, payload, timeout))
        if path == "/api/health":
            return SmokeResponse(200, {"ok": True, "status": "ok", "service": "llm-labeling-scaffold"})
        if path == "/api/version":
            return SmokeResponse(200, {"service": "llm-labeling-scaffold", "version": "0.1.0"})
        if path == "/api/capabilities":
            return SmokeResponse(
                200,
                {
                    "endpoints": [
                        {
                            "method": "POST",
                            "path": "/api/import/data_lake",
                            "action": "import_dry_run",
                            "side_effects": False,
                            "request_schema": {
                                "type": "object",
                                "properties": {"task_id": {}, "import_id": {}, "dry_run": {}},
                            },
                        }
                    ]
                },
            )
        if path == "/api/settings/public":
            return SmokeResponse(200, {"settings": {"rclone_configured": True}})
        if path == "/api/tasks/patent_boundary_v0_1/check":
            return SmokeResponse(200, {"ok": True, "checks": [], "warnings": [], "errors": []})
        if path == "/api/import/data_lake":
            return SmokeResponse(200, {"ok": True, "dry_run": True, "result": {"would_import": True}})
        raise AssertionError(f"unexpected request: {method} {path}")

    summary = run_smoke(
        SmokeConfig(
            server_url="https://scaffold.example.test",
            token="smoke-token",
            task_id="patent_boundary_v0_1",
            import_id="patent_boundary_manual_seed_500_2026_06_27",
            timeout=3.0,
        ),
        transport=fake_transport,
    )

    assert summary["ok"] is True
    import_check = next(item for item in summary["checks"] if item["name"] == "import_dry_run")
    assert [(method, path) for method, path, *_ in calls] == [
        ("GET", "/api/health"),
        ("GET", "/api/version"),
        ("GET", "/api/capabilities"),
        ("GET", "/api/settings/public"),
        ("POST", "/api/tasks/patent_boundary_v0_1/check"),
        ("POST", "/api/import/data_lake"),
    ]
    assert calls[0][2]["Authorization"] == "Bearer smoke-token"
    assert calls[-1][3] == {
        "task_id": "patent_boundary_v0_1",
        "import_id": "patent_boundary_manual_seed_500_2026_06_27",
        "dry_run": True,
    }
    assert calls[-1][4] == 3.0
    assert import_check["dry_run"] is True
    assert import_check["result_keys"] == ["would_import"]
    assert import_check["result_would_import"] is True


def test_smoke_runner_marks_import_dry_run_not_supported_without_safe_contract():
    calls = []
    basic_header = "Basic " + base64.b64encode(b"admin:secret").decode("ascii")

    def fake_transport(method, url, headers, body, timeout):
        path = urlparse(url).path
        calls.append((method, path, headers))
        if path == "/api/health":
            return SmokeResponse(200, {"ok": True, "status": "ok", "service": "llm-labeling-scaffold"})
        if path == "/api/version":
            return SmokeResponse(200, {"service": "llm-labeling-scaffold", "version": "0.1.0"})
        if path == "/api/capabilities":
            return SmokeResponse(200, {"endpoints": []})
        if path == "/api/settings/public":
            return SmokeResponse(200, {"settings": {}})
        if path == "/api/tasks/patent_boundary_v0_1/check":
            return SmokeResponse(200, {"ok": True, "checks": [], "warnings": [], "errors": []})
        raise AssertionError(f"unsafe import endpoint should not be called: {path}")

    summary = run_smoke(
        SmokeConfig(
            server_url="http://127.0.0.1:8765",
            basic_user="admin",
            basic_password="secret",
        ),
        transport=fake_transport,
    )

    import_check = next(item for item in summary["checks"] if item["name"] == "import_dry_run")
    assert summary["ok"] is False
    assert import_check["status"] == "not_supported"
    assert calls[0][2]["Authorization"] == basic_header
    assert all(path != "/api/import/data_lake" for _, path, _ in calls)


def test_smoke_runner_compacts_legacy_import_dry_run_object():
    def fake_transport(method, url, headers, body, timeout):
        path = urlparse(url).path
        if path == "/api/health":
            return SmokeResponse(200, {"ok": True, "status": "ok", "service": "llm-labeling-scaffold"})
        if path == "/api/version":
            return SmokeResponse(200, {"service": "llm-labeling-scaffold", "version": "0.1.0"})
        if path == "/api/capabilities":
            return SmokeResponse(
                200,
                {
                    "endpoints": [
                        {
                            "method": "POST",
                            "path": "/api/import/data_lake",
                            "action": "import_dry_run",
                            "side_effects": False,
                            "request_schema": {"properties": {"dry_run": {}}},
                        }
                    ]
                },
            )
        if path == "/api/settings/public":
            return SmokeResponse(200, {"settings": {}})
        if path == "/api/tasks/patent_boundary_v0_1/check":
            return SmokeResponse(200, {"ok": True, "checks": [], "warnings": [], "errors": []})
        if path == "/api/import/data_lake":
            return SmokeResponse(
                200,
                {
                    "ok": True,
                    "dry_run": {
                        "import_id": "patent_boundary_manual_seed_500_2026_06_27",
                        "validation_ok": True,
                        "source_manifest_uri": "r2://private-bucket/secret/manifest.json",
                        "manifest": {"source_object_path": "private/raw.jsonl"},
                    },
                },
            )
        raise AssertionError(f"unexpected request: {method} {path}")

    summary = run_smoke(SmokeConfig(server_url="http://127.0.0.1:8765"), transport=fake_transport)
    import_check = next(item for item in summary["checks"] if item["name"] == "import_dry_run")
    rendered = render_summary(summary, "json")

    assert summary["ok"] is True
    assert import_check["dry_run"] is True
    assert import_check["dry_run_import_id"] == "patent_boundary_manual_seed_500_2026_06_27"
    assert import_check["dry_run_validation_ok"] is True
    assert "r2://private-bucket" not in rendered
    assert "private/raw.jsonl" not in rendered
