from __future__ import annotations

import hashlib
import json
import threading
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from http.server import ThreadingHTTPServer

import pytest

from llm_labeling_scaffold import panel, panel_allocation
from llm_labeling_scaffold.allocation import preview_allocation
from llm_labeling_scaffold.auth import ActorContext, Identity, Principal
from llm_labeling_scaffold.db import (
    AllocationPlanLifecycle,
    AllocationProgress,
    AllocationResourceNotFound,
    AuthorizationDenied,
    AuthorizationReason,
    Permission,
)


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


_TASK_UNSET = object()


class _PreviewAuthorizationService:
    def __init__(self, *, allowed: bool = True, task=_TASK_UNSET, reason=AuthorizationReason.ROLE_DENIED):
        self.allowed = allowed
        self.task = object() if task is _TASK_UNSET and allowed else task
        self.reason = reason
        self.calls: list[tuple[str, str, Permission]] = []

    def require_task(self, identity, workspace_slug: str, task_key: str, permission: Permission):
        self.calls.append((workspace_slug, task_key, permission))
        if not self.allowed:
            raise AuthorizationDenied(SimpleNamespace(reason=self.reason))
        return SimpleNamespace(task=self.task)


@contextmanager
def _panel_server(service, tmp_path: Path, allocation_repository=None):
    old = {
        "runs_root": panel._Handler.runs_root,
        "tasks_root": panel._Handler.tasks_root,
        "static_dir": panel._Handler.static_dir,
        "authenticator": panel._Handler.authenticator,
        "authorization_service": panel._Handler.authorization_service,
        "allocation_repository": panel._Handler.allocation_repository,
        "authorization_ready": panel._Handler.authorization_ready,
        "active_task_loader": panel._Handler.active_task_loader,
    }
    panel._Handler.runs_root = tmp_path / "runs"
    panel._Handler.tasks_root = tmp_path / "tasks"
    panel._Handler.static_dir = None
    panel._Handler.authenticator = _ContextAuthenticator()
    panel._Handler.authorization_service = service
    panel._Handler.allocation_repository = allocation_repository
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


def _request_json(
    base_url: str,
    path: str,
    *,
    body: dict | None = None,
    method: str = "GET",
    headers: dict[str, str] | None = None,
) -> tuple[int, dict]:
    request_headers = {"Content-Type": "application/json", **(headers or {})}
    request = urllib.request.Request(
        base_url + path,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers=request_headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class _AllocationRepositoryStub:
    def __init__(self):
        self.create_calls = []
        self.confirm_calls = []
        self.progress_calls = []
        self.detail_calls = []
        self.assignment_calls = []

    def create(self, request, **kwargs):
        self.create_calls.append((request, kwargs))
        return SimpleNamespace(
            plan_id=uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
            workspace_id=uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
            task_id=uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc"),
            task_revision_id=uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd"),
            cohort_revision_id=kwargs["cohort_revision_id"],
            fingerprint=_hash("plan"),
            input_fingerprint=_hash("input"),
            lifecycle_state=AllocationPlanLifecycle.DRAFT,
            response_status=201,
            replayed=False,
        )

    def confirm(self, plan_id, **kwargs):
        self.confirm_calls.append((plan_id, kwargs))
        return SimpleNamespace(
            plan_id=uuid.UUID(plan_id),
            lifecycle_state=AllocationPlanLifecycle.CONFIRMED,
            confirmed_at=None,
            response_status=200,
            replayed=False,
        )

    def progress(self, plan_id, **kwargs):
        self.progress_calls.append((plan_id, kwargs))
        return AllocationProgress(
            plan_id=uuid.UUID(plan_id),
            accepted_submissions=2,
            required_submissions=5,
            completed=False,
        )

    def list_plans(self, **kwargs):
        self.detail_calls.append(("list", kwargs))
        return {"workspace": kwargs["workspace_slug"], "plans": [], "next_cursor": None}

    def get_plan(self, plan_id, **kwargs):
        self.detail_calls.append((plan_id, kwargs))
        if kwargs["workspace_slug"] != "workspace-a":
            raise AllocationResourceNotFound("allocation plan was not found")
        return {"plan_id": str(plan_id), "audit_events": []}

    def list_assignments(self, plan_id, **kwargs):
        self.assignment_calls.append(("list", plan_id, kwargs))
        return {
            "workspace": kwargs["workspace_slug"],
            "plan_id": str(plan_id),
            "assignments": [],
            "next_cursor": None,
        }

    def get_assignment(self, assignment_id, **kwargs):
        self.assignment_calls.append((assignment_id, kwargs))
        return {"assignment_id": str(assignment_id), "audit_events": []}


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


@pytest.mark.parametrize(
    "service",
    [
        _PreviewAuthorizationService(task=None),
        _PreviewAuthorizationService(
            allowed=False,
            reason=AuthorizationReason.RESOURCE_NOT_VISIBLE,
        ),
    ],
    ids=["missing_task_on_decision", "invisible_task"],
)
def test_preview_route_fails_closed_for_missing_or_invisible_task(
    monkeypatch,
    tmp_path: Path,
    service: _PreviewAuthorizationService,
):
    monkeypatch.setenv("LLS_TASK_SOURCE", "control")
    preview_called = False

    def fail_preview(*args, **kwargs):
        nonlocal preview_called
        preview_called = True
        raise AssertionError("invisible or missing tasks must not reach preview_allocation_dto")

    monkeypatch.setattr(panel, "preview_allocation_dto", fail_preview)
    with _panel_server(service, tmp_path) as base_url:
        status, response = _request(base_url, _payload())

    assert status == 404
    assert response["code"] == "resource_not_found"
    assert preview_called is False


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


def _plan_create_payload() -> dict:
    payload = _payload()
    payload["scope"]["cohort_revision_id"] = "11111111-1111-1111-1111-111111111111"
    return payload


def test_parse_plan_create_request_binds_cohort_scope_and_rejects_forged_output():
    parsed = panel_allocation.parse_allocation_plan_create_request(_plan_create_payload())

    assert parsed.workspace == "workspace-a"
    assert parsed.cohort_revision_id == uuid.UUID("11111111-1111-1111-1111-111111111111")
    assert parsed.allocation_request.task_revision.task_id == "task-a"

    for field in ("plan", "assignments", "progress"):
        payload = _plan_create_payload()
        payload[field] = {}
        with pytest.raises(panel_allocation.AllocationPreviewDTOError) as exc_info:
            panel_allocation.parse_allocation_plan_create_request(payload)
        assert exc_info.value.code == "unknown_field"


def test_plan_create_uses_dedicated_route_and_repository_planner(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("LLS_TASK_SOURCE", "control")
    repository = _AllocationRepositoryStub()
    payload = _plan_create_payload()
    payload["idempotency_key"] = "plan-create-1"

    with _panel_server(_PreviewAuthorizationService(), tmp_path, repository) as base_url:
        status, response = _request_json(
            base_url,
            "/api/allocation/plans",
            body=payload,
            method="POST",
            headers={"Idempotency-Key": "plan-create-1"},
        )

    assert status == 201
    assert response["kind"] == "allocation_plan_v1"
    assert response["lifecycle_state"] == "draft"
    assert len(repository.create_calls) == 1
    request, kwargs = repository.create_calls[0]
    assert request.task_revision.task_id == "task-a"
    assert kwargs["workspace_slug"] == "workspace-a"
    assert kwargs["idempotency_key"] == "plan-create-1"


def test_plan_create_rejects_caller_plan_assignment_and_progress(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("LLS_TASK_SOURCE", "control")
    repository = _AllocationRepositoryStub()

    payload = _plan_create_payload()
    payload["assignments"] = [{"assignment_id": "forged"}]
    with _panel_server(_PreviewAuthorizationService(), tmp_path, repository) as base_url:
        status, response = _request_json(
            base_url,
            "/api/allocation/plans",
            body=payload,
            method="POST",
            headers={"Idempotency-Key": "plan-create-2"},
        )

    assert status == 422
    assert response["code"] == "unknown_field"
    assert repository.create_calls == []


def test_plan_create_requires_idempotency_key_header(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("LLS_TASK_SOURCE", "control")
    repository = _AllocationRepositoryStub()
    payload = _plan_create_payload()
    payload["idempotency_key"] = "body-only-key"

    with _panel_server(_PreviewAuthorizationService(), tmp_path, repository) as base_url:
        status, response = _request_json(
            base_url,
            "/api/allocation/plans",
            body=payload,
            method="POST",
        )

    assert status == 422
    assert response["code"] == "missing_idempotency_key"
    assert repository.create_calls == []


def test_plan_control_routes_return_server_derived_progress_and_detail(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("LLS_TASK_SOURCE", "control")
    repository = _AllocationRepositoryStub()
    plan_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    assignment_id = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"

    with _panel_server(_PreviewAuthorizationService(), tmp_path, repository) as base_url:
        confirm_status, confirm = _request_json(
            base_url,
            f"/api/allocation/plans/{plan_id}/confirm?workspace=workspace-a",
            body={"plan_fingerprint": _hash("plan")},
            method="POST",
            headers={"Idempotency-Key": "confirm-1"},
        )
        progress_status, progress = _request_json(
            base_url,
            f"/api/allocation/plans/{plan_id}/progress?workspace=workspace-a",
        )
        plan_status, plan = _request_json(
            base_url,
            f"/api/allocation/plans/{plan_id}?workspace=workspace-a",
        )
        assignments_status, assignments = _request_json(
            base_url,
            f"/api/allocation/plans/{plan_id}/assignments?workspace=workspace-a",
        )
        assignment_status, assignment = _request_json(
            base_url,
            f"/api/allocation/assignments/{assignment_id}?workspace=workspace-a",
        )

    assert confirm_status == 200
    assert confirm["lifecycle_state"] == "confirmed"
    assert progress_status == 200
    assert progress["progress"] == {
        "accepted_submissions": 2,
        "required_submissions": 5,
        "completed": False,
        "ratio": 0.4,
    }
    assert plan_status == 200 and plan["plan"]["plan_id"] == plan_id
    assert assignments_status == 200 and assignments["plan_id"] == plan_id
    assert assignment_status == 200 and assignment["assignment"]["assignment_id"] == assignment_id
    assert repository.confirm_calls[0][1]["workspace_slug"] == "workspace-a"
    assert repository.progress_calls[0][1]["workspace_slug"] == "workspace-a"


def test_plan_confirm_fails_closed_on_workspace_selector_conflict(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("LLS_TASK_SOURCE", "control")
    repository = _AllocationRepositoryStub()
    with _panel_server(_PreviewAuthorizationService(), tmp_path, repository) as base_url:
        status, response = _request_json(
            base_url,
            "/api/allocation/plans/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/confirm?workspace=workspace-b",
            body={"workspace": "workspace-a", "plan_fingerprint": _hash("plan")},
            method="POST",
            headers={"Idempotency-Key": "confirm-2"},
        )

    assert status == 422
    assert response["code"] == "workspace_selector_conflict"
    assert repository.confirm_calls == []


def test_plan_detail_maps_cross_workspace_resource_to_404(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("LLS_TASK_SOURCE", "control")
    repository = _AllocationRepositoryStub()
    with _panel_server(_PreviewAuthorizationService(), tmp_path, repository) as base_url:
        status, response = _request_json(
            base_url,
            "/api/allocation/plans/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa?workspace=workspace-b",
        )

    assert status == 404
    assert response == {"code": "resource_not_found", "error": "allocation 资源不存在"}
