from __future__ import annotations

import hashlib
import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from http.server import ThreadingHTTPServer

import pytest

from llm_labeling_scaffold import panel, panel_allocation
from llm_labeling_scaffold.allocation import preview_allocation
from llm_labeling_scaffold.auth import ActorContext, Identity, Principal
from llm_labeling_scaffold.db import AuthorizationDenied, AuthorizationReason, Permission


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _payload() -> dict:
    return {
        "scope": {
            "workspace": "workspace-a",
            "task_id": "task-a",
            "revision_id": "revision-1",
            "revision_hash": _hash("revision-1"),
        },
        "request": {
            "strategy": "fixed_partition",
            "source_manifest": {
                "manifest_id": "manifest-1",
                "manifest_hash": _hash("manifest-1"),
                "kind": "sample",
            },
            "records": [
                {
                    "record_id": "record-1",
                    "content_hash": _hash("record-1"),
                    "batch_id": "batch-1",
                },
                {
                    "record_id": "record-2",
                    "content_hash": _hash("record-2"),
                    "batch_id": "batch-1",
                },
            ],
            "annotators": [
                {"annotator_id": "annotator-1", "cohort_id": "cohort-1", "capacity": 4},
                {"annotator_id": "annotator-2", "cohort_id": "cohort-1", "capacity": 4},
            ],
            "seed": 17,
            "algorithm_version": "allocation-v1",
        },
    }


def test_parse_preview_request_binds_scope_to_allocation_request():
    parsed = panel_allocation.parse_allocation_preview_request(_payload())

    assert parsed.workspace == "workspace-a"
    assert parsed.allocation_request.task_revision.task_id == "task-a"
    assert parsed.allocation_request.task_revision.revision_id == "revision-1"
    assert parsed.allocation_request.seed == 17
    assert parsed.allocation_request.records[0].record_id == "record-1"


@pytest.mark.parametrize(
    ("field", "code"),
    [
        ("actor", "server_context_forbidden"),
        ("caller", "server_context_forbidden"),
        ("channel", "server_context_forbidden"),
        ("api_key", "sensitive_input_forbidden"),
        ("password", "sensitive_input_forbidden"),
        ("path", "path_forbidden"),
    ],
)
def test_parse_preview_request_rejects_server_context_secrets_and_paths(field: str, code: str):
    payload = _payload()
    payload[field] = "blocked"

    with pytest.raises(panel_allocation.AllocationPreviewDTOError) as exc_info:
        panel_allocation.parse_allocation_preview_request(payload)

    assert exc_info.value.code == code


def test_parse_preview_request_rejects_revision_scope_conflict():
    payload = _payload()
    payload["request"]["task_revision"] = {
        "task_id": "other-task",
        "revision_id": "revision-1",
        "revision_hash": _hash("revision-1"),
    }

    with pytest.raises(panel_allocation.AllocationPreviewDTOError) as exc_info:
        panel_allocation.parse_allocation_preview_request(payload)

    assert exc_info.value.code == "scope_revision_conflict"


def test_preview_dto_is_whitelisted_and_canonical():
    parsed = panel_allocation.parse_allocation_preview_request(_payload())
    response = panel_allocation.preview_allocation_dto(parsed)
    encoded = panel_allocation.serialize_allocation_preview_json(
        parsed,
        preview_allocation(parsed.allocation_request),
    )

    assert response["schema_version"] == "panel_allocation_preview_v1"
    assert response["ready"] is True
    assert response["scope"]["workspace"] == "workspace-a"
    assert "seed" not in response
    assert "records" not in response
    assert "actor" not in encoded
    assert "caller" not in encoded
    assert "channel" not in encoded
    assert encoded == json.dumps(response, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def test_preview_dto_returns_blocking_planner_errors_without_partial_requirements():
    payload = _payload()
    payload["request"]["annotators"][0]["capacity"] = 0
    payload["request"]["annotators"][1]["capacity"] = 0

    response = panel_allocation.preview_allocation_payload(payload)

    assert response["ready"] is False
    assert response["blocking_errors"]
    assert response["workspace_requirements"] == []
    assert response["dataset_requirements"] == []


class _ContextAuthenticator:
    mode = "basic_dev"
    challenge = None

    def authenticate(self, headers):
        return ActorContext.direct(
            Principal(
                kind="user",
                authentication_method="basic_dev",
                identity=Identity("urn:test", "user-a", email="user-a@example.test"),
            )
        )


class _PreviewAuthorizationService:
    def __init__(self, *, allowed: bool = True):
        self.allowed = allowed
        self.calls: list[tuple[str, str, Permission]] = []

    def require_task(self, identity, workspace_slug: str, task_key: str, permission: Permission):
        self.calls.append((workspace_slug, task_key, permission))
        if not self.allowed:
            raise AuthorizationDenied(SimpleNamespace(reason=AuthorizationReason.ROLE_DENIED))


@contextmanager
def _panel_server(service, tmp_path: Path):
    old = {
        "runs_root": panel._Handler.runs_root,
        "tasks_root": panel._Handler.tasks_root,
        "static_dir": panel._Handler.static_dir,
        "authenticator": panel._Handler.authenticator,
        "authorization_service": panel._Handler.authorization_service,
        "authorization_ready": panel._Handler.authorization_ready,
        "active_task_loader": panel._Handler.active_task_loader,
    }
    panel._Handler.runs_root = tmp_path / "runs"
    panel._Handler.tasks_root = tmp_path / "tasks"
    panel._Handler.static_dir = None
    panel._Handler.authenticator = _ContextAuthenticator()
    panel._Handler.authorization_service = service
    panel._Handler.authorization_ready = service is not None
    panel._Handler.active_task_loader = None
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


def _request(base_url: str, body: dict) -> tuple[int, dict]:
    request = urllib.request.Request(
        base_url + "/api/allocation/preview",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_preview_route_requires_annotation_review_and_does_not_use_action(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("LLS_TASK_SOURCE", "control")
    service = _PreviewAuthorizationService()
    action_called = False

    def fail_action(*args, **kwargs):
        nonlocal action_called
        action_called = True
        raise AssertionError("allocation preview must not use /api/action")

    monkeypatch.setattr(panel.pipeline, "start_action", fail_action)
    with _panel_server(service, tmp_path) as base_url:
        status, response = _request(base_url, _payload())

    assert status == 200
    assert response["ready"] is True
    assert service.calls == [("workspace-a", "task-a", Permission.ANNOTATION_REVIEW)]
    assert action_called is False


def test_preview_route_maps_permission_denial_to_403(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("LLS_TASK_SOURCE", "control")
    with _panel_server(_PreviewAuthorizationService(allowed=False), tmp_path) as base_url:
        status, response = _request(base_url, _payload())

    assert status == 403
    assert response["code"] == "permission_denied"


def test_preview_route_maps_unavailable_authorization_to_503(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("LLS_TASK_SOURCE", "control")
    with _panel_server(None, tmp_path) as base_url:
        status, response = _request(base_url, _payload())

    assert status == 503
    assert response["code"] == "authorization_unavailable"


def test_preview_route_maps_blocking_preview_to_422(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("LLS_TASK_SOURCE", "control")
    payload = _payload()
    payload["request"]["annotators"][0]["capacity"] = 0
    payload["request"]["annotators"][1]["capacity"] = 0
    with _panel_server(_PreviewAuthorizationService(), tmp_path) as base_url:
        status, response = _request(base_url, payload)

    assert status == 422
    assert response["ready"] is False
    assert response["blocking_errors"]
