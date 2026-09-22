from __future__ import annotations

import json
from pathlib import Path

from llm_labeling_scaffold.auth import ActorContext
from llm_labeling_scaffold.db import AuditChannel, ExternalIdentity
from llm_labeling_scaffold import panel

from tests.test_panel_control import (
    FakeActiveTaskLoader,
    _active_revision,
    _auth_principal,
    _draft_spec,
    _panel_server,
    _request,
    control_database,
)


class RecordingActiveTaskLoader(FakeActiveTaskLoader):
    def __init__(self, revisions=None) -> None:
        super().__init__(revisions)
        self.calls: list[tuple[str, str]] = []

    def load_active_revision(self, workspace_slug: str, task_key: str):
        self.calls.append((workspace_slug, task_key))
        return super().load_active_revision(workspace_slug, task_key)


def _save_draft(service, task_id: str, definition: dict) -> None:
    identity = ExternalIdentity("urn:lls:basic-dev", "experimenter")
    service.save_task_draft(
        actor_identity=identity,
        caller_identity=identity,
        workspace_slug="workspace-a",
        task_key=task_id,
        definition=definition,
        rendered_task=f"task_id: {task_id}\n",
        if_match=None,
        if_none_match="*",
        channel=AuditChannel.PANEL,
    )


def test_task_summary_returns_authorized_published_viewer_summary(
    control_database,
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("LLS_TASK_SOURCE", "control")
    service = control_database["service"]
    loader = RecordingActiveTaskLoader({("workspace-a", "panel-control-task"): _active_revision()})

    with _panel_server(
        tmp_path,
        context=ActorContext.direct(_auth_principal("viewer")),
        service=service,
        loader=loader,
    ) as base_url:
        status, payload, headers = _request(
            base_url,
            "/api/task/summary?workspace=workspace-a&task_id=panel-control-task",
        )
        capabilities_status, capabilities, _ = _request(base_url, "/api/capabilities")

    task = payload["task"]
    assert status == 200
    assert task["task_id"] == "panel-control-task"
    assert task["workspace"] == "workspace-a"
    assert task["lifecycle_state"] == "active"
    assert task["status"] == "published"
    assert task["active"] is True
    assert task["revision"] == 1
    assert task["roles"] == ["viewer"]
    assert task["capabilities"] == ["task:read"]
    assert task["editable"] is False
    assert task["deletable"] is False
    assert "draft_spec" not in json.dumps(payload)
    assert headers["Cache-Control"] == "no-store, private"
    assert headers["Pragma"] == "no-cache"
    assert loader.calls == [("workspace-a", "panel-control-task")]
    assert capabilities_status == 200
    assert any(
        endpoint["method"] == "GET" and endpoint["path"] == "/api/task/summary"
        for endpoint in capabilities["endpoints"]
    )
    assert panel._mcp_route_allowed("GET", "/api/task/summary") is True
    assert panel._database_authorized_route("GET", "/api/task/summary") is True


def test_task_summary_reports_draft_states_without_draft_spec(
    control_database,
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("LLS_TASK_SOURCE", "control")
    service = control_database["service"]
    _save_draft(service, "unpublished-task", _draft_spec("unpublished-task"))
    _save_draft(
        service,
        "panel-control-task",
        {**_draft_spec(), "annotation_guidelines": "changed draft"},
    )
    loader = RecordingActiveTaskLoader({("workspace-a", "panel-control-task"): _active_revision()})

    with _panel_server(
        tmp_path,
        context=ActorContext.direct(_auth_principal("viewer")),
        service=service,
        loader=loader,
    ) as base_url:
        draft_status, draft_payload, _ = _request(
            base_url,
            "/api/task/summary?workspace=workspace-a&task_id=unpublished-task",
        )
        published_with_draft_status, published_with_draft_payload, _ = _request(
            base_url,
            "/api/task/summary?workspace=workspace-a&task_id=panel-control-task",
        )

    draft = draft_payload["task"]
    published_with_draft = published_with_draft_payload["task"]
    assert draft_status == 200
    assert draft["lifecycle_state"] == "active"
    assert draft["status"] == "draft"
    assert draft["revision"] == 0
    assert draft["capabilities"] == ["task:read"]
    assert published_with_draft_status == 200
    assert published_with_draft["status"] == "published_with_draft"
    assert published_with_draft["revision"] == 1
    assert published_with_draft["draft_version"] == 1
    assert "draft_spec" not in json.dumps(draft_payload)
    assert "draft_spec" not in json.dumps(published_with_draft_payload)


def test_task_summary_returns_disabled_and_archived_without_revision_zero_or_loader_access(
    control_database,
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("LLS_TASK_SOURCE", "control")
    service = control_database["service"]
    identity = ExternalIdentity("urn:lls:basic-dev", "experimenter")
    common = {
        "actor_identity": identity,
        "caller_identity": identity,
        "workspace_slug": "workspace-a",
        "channel": AuditChannel.PANEL,
    }
    service.disable_task(
        **common,
        task_key="panel-control-task",
        reason="pause",
        idempotency_key="summary-disable",
    )
    service.archive_task(
        **common,
        task_key="unpublished-task",
        reason="retention",
        idempotency_key="summary-archive",
    )
    loader = RecordingActiveTaskLoader(
        {
            ("workspace-a", "panel-control-task"): _active_revision(),
            ("workspace-a", "unpublished-task"): _active_revision(),
        }
    )

    with _panel_server(
        tmp_path,
        context=ActorContext.direct(_auth_principal("viewer")),
        service=service,
        loader=loader,
    ) as base_url:
        disabled_status, disabled_payload, _ = _request(
            base_url,
            "/api/task/summary?workspace=workspace-a&task_id=panel-control-task",
        )
        archived_status, archived_payload, _ = _request(
            base_url,
            "/api/task/summary?workspace=workspace-a&task_id=unpublished-task",
        )

    disabled = disabled_payload["task"]
    archived = archived_payload["task"]
    assert disabled_status == 200
    assert disabled["lifecycle_state"] == "disabled"
    assert disabled["status"] == "disabled"
    assert disabled["active"] is False
    assert "revision" not in disabled
    assert archived_status == 200
    assert archived["lifecycle_state"] == "archived"
    assert archived["status"] == "archived"
    assert archived["active"] is False
    assert "revision" not in archived
    assert loader.calls == []


def test_task_summary_hides_unknown_and_cross_workspace_tasks(
    control_database,
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("LLS_TASK_SOURCE", "control")
    service = control_database["service"]
    loader = RecordingActiveTaskLoader()

    with _panel_server(
        tmp_path,
        context=ActorContext.direct(_auth_principal("viewer")),
        service=service,
        loader=loader,
    ) as base_url:
        unknown_status, unknown_payload, _ = _request(
            base_url,
            "/api/task/summary?workspace=workspace-a&task_id=missing-task",
        )
        cross_workspace_status, cross_workspace_payload, _ = _request(
            base_url,
            "/api/task/summary?workspace=workspace-b&task_id=hidden-task",
        )
        wrong_workspace_status, wrong_workspace_payload, _ = _request(
            base_url,
            "/api/task/summary?workspace=workspace-a&task_id=hidden-task",
        )

    assert (unknown_status, unknown_payload["code"]) == (404, "resource_not_found")
    assert (cross_workspace_status, cross_workspace_payload["code"]) == (404, "resource_not_found")
    assert (wrong_workspace_status, wrong_workspace_payload["code"]) == (404, "resource_not_found")

    with _panel_server(
        tmp_path,
        context=ActorContext.direct(_auth_principal("unknown")),
        service=service,
        loader=loader,
    ) as base_url:
        unknown_principal_status, unknown_principal_payload, _ = _request(
            base_url,
            "/api/task/summary?workspace=workspace-a&task_id=panel-control-task",
        )

    assert (unknown_principal_status, unknown_principal_payload["code"]) == (404, "resource_not_found")
