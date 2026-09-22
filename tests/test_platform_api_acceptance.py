from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from llm_labeling_scaffold.auth import ActorContext, PanelAuthenticator
from llm_labeling_scaffold.db.materialization import (
    ControlTaskSnapshotLoader,
    TaskMaterializationWorker,
)
from tests.test_panel_control import (
    PANEL_AUDIENCE,
    _access_assertion,
    _access_verifier,
    _auth_principal,
    _draft_spec,
    _panel_server,
    _request,
    _signing_key,
    FakeActiveTaskLoader,
    control_database,
)


@pytest.fixture
def acceptance_root(tmp_path: Path) -> Path:
    root = tmp_path / "http-tests"
    root.mkdir(parents=True)
    return root


def _session_factory(control_database):
    return sessionmaker(
        bind=control_database["engine"],
        class_=Session,
        expire_on_commit=False,
    )


def test_http_task_control_create_publish_materialize_and_lifecycle(
    control_database,
    acceptance_root: Path,
    monkeypatch,
):
    monkeypatch.setenv("LLS_TASK_SOURCE", "control")
    service = control_database["service"]
    factory = _session_factory(control_database)
    loader = ControlTaskSnapshotLoader(
        factory,
        acceptance_root / "runs",
        acceptance_root / "tasks",
    )
    context = ActorContext.direct(_auth_principal("experimenter"))
    task_id = "http-acceptance-task"
    spec = {
        **_draft_spec(task_id),
        "annotation_guidelines": "first release",
    }

    with _panel_server(
        acceptance_root,
        context=context,
        service=service,
        loader=loader,
    ) as base_url:
        create_status, created, create_headers = _request(
            base_url,
            "/api/tasks",
            method="POST",
            body={**spec, "idempotency_key": "http-create-001"},
        )
        initial_etag = create_headers["ETag"]
        edit_status, edited, edit_headers = _request(
            base_url,
            f"/api/tasks/{task_id}",
            method="PUT",
            body={**spec, "annotation_guidelines": "first release edited"},
            headers={"If-Match": initial_etag},
        )
        stale_status, stale, stale_headers = _request(
            base_url,
            f"/api/tasks/{task_id}",
            method="PUT",
            body={**spec, "annotation_guidelines": "stale edit"},
            headers={"If-Match": initial_etag},
        )
        current_etag = edit_headers["ETag"]
        publish_status, published, _ = _request(
            base_url,
            f"/api/tasks/{task_id}/publish?workspace=workspace-a",
            method="POST",
            body={
                "confirm": True,
                "reason": "acceptance release",
                "idempotency_key": "http-publish-001",
            },
            headers={"If-Match": current_etag},
        )

        worker = TaskMaterializationWorker(
            factory,
            runs_root=acceptance_root / "runs",
            tasks_root=acceptance_root / "tasks",
            worker_id="http-acceptance-worker",
            retry_delay_seconds=0,
            poll_seconds=0.01,
        )
        materialized = worker.drain(
            uuid.UUID(published["published"]["materialization_id"]),
            timeout_seconds=1,
        )
        detail_status, detail, _ = _request(
            base_url,
            f"/api/tasks/{task_id}?workspace=workspace-a",
        )
        snapshot_path = Path(materialized.snapshot_path)
        snapshot_definition = (snapshot_path / "definition.json").read_bytes()
        active = loader.load_active_revision("workspace-a", task_id)

        draft_status, draft, draft_headers = _request(
            base_url,
            f"/api/task/control?task_id={task_id}&workspace=workspace-a",
        )
        post_publish_edit_status, post_publish_edit, _ = _request(
            base_url,
            f"/api/tasks/{task_id}",
            method="PUT",
            body={**spec, "annotation_guidelines": "draft after release"},
            headers={"If-Match": draft_headers["ETag"]},
        )
        assert (snapshot_path / "definition.json").read_bytes() == snapshot_definition
        active_after_draft_edit = loader.load_active_revision("workspace-a", task_id)

        archive_status, archived, _ = _request(
            base_url,
            f"/api/tasks/{task_id}/archive?workspace=workspace-a",
            method="POST",
            body={"reason": "archive release", "idempotency_key": "http-archive-001"},
        )
        archive_replay_status, archive_replay, _ = _request(
            base_url,
            f"/api/tasks/{task_id}/archive?workspace=workspace-a",
            method="POST",
            body={"reason": "archive release", "idempotency_key": "http-archive-001"},
        )
        archived_detail_status, archived_detail, _ = _request(
            base_url,
            f"/api/tasks/{task_id}?workspace=workspace-a",
        )
        restore_status, restored, _ = _request(
            base_url,
            f"/api/tasks/{task_id}/restore?workspace=workspace-a",
            method="POST",
            body={"reason": "restore for acceptance", "idempotency_key": "http-restore-001"},
        )
        restored_detail_status, restored_detail, _ = _request(
            base_url,
            f"/api/tasks/{task_id}?workspace=workspace-a",
        )
        archive_again_status, archive_again, _ = _request(
            base_url,
            f"/api/tasks/{task_id}/archive?workspace=workspace-a",
            method="POST",
            body={"reason": "archive after restore", "idempotency_key": "http-archive-002"},
        )
        archive_again_replay_status, archive_again_replay, _ = _request(
            base_url,
            f"/api/tasks/{task_id}/archive?workspace=workspace-a",
            method="POST",
            body={"reason": "archive after restore", "idempotency_key": "http-archive-002"},
        )

    assert (create_status, created["action"]) == (201, "created")
    assert edit_status == 200
    assert edited["record"]["draft_version"] == 2
    assert (stale_status, stale["code"]) == (412, "task_draft_conflict")
    assert stale_headers["ETag"] == current_etag
    assert (publish_status, published["published"]["materialization_state"]) == (202, "pending")
    assert materialized.state.value == "succeeded"
    assert materialized.activation_state == "active"
    assert detail_status == 200
    assert detail["task"]["revision"] == 1
    assert active is not None
    assert active.revision_number == 1
    assert active.definition["annotation_guidelines"] == "first release edited"
    assert draft_status == 200
    assert post_publish_edit_status == 200
    assert post_publish_edit["record"]["draft_version"] == 3
    assert active_after_draft_edit is not None
    assert active_after_draft_edit.definition["annotation_guidelines"] == "first release edited"
    assert (archive_status, archived["task"]["lifecycle_state"]) == (200, "archived")
    assert (archive_replay_status, archive_replay["replayed"]) == (200, True)
    assert (archived_detail_status, archived_detail["code"]) == (409, "task_lifecycle_not_active")
    assert (restore_status, restored["task"]["lifecycle_state"]) == (200, "active")
    assert restored_detail_status == 200
    assert restored_detail["task"]["revision"] == 1
    assert (archive_again_status, archive_again["replayed"]) == (200, False)
    assert (archive_again_replay_status, archive_again_replay["replayed"]) == (200, True)
    assert json.loads(snapshot_definition)["annotation_guidelines"] == "first release edited"


def test_http_task_control_rejects_viewer_cross_workspace_and_unknown_identity(
    control_database,
    acceptance_root: Path,
    monkeypatch,
):
    monkeypatch.setenv("LLS_TASK_SOURCE", "control")
    service = control_database["service"]
    spec = {**_draft_spec("http-denied-create")}

    with _panel_server(
        acceptance_root,
        context=ActorContext.direct(_auth_principal("viewer")),
        service=service,
        loader=FakeActiveTaskLoader(),
    ) as base_url:
        viewer_status, viewer, _ = _request(
            base_url,
            "/api/tasks",
            method="POST",
            body={**spec, "idempotency_key": "http-denied-create-001"},
        )

    with _panel_server(
        acceptance_root,
        context=ActorContext.direct(_auth_principal("experimenter")),
        service=service,
        loader=FakeActiveTaskLoader(),
    ) as base_url:
        cross_workspace_status, cross_workspace, _ = _request(
            base_url,
            "/api/tasks/hidden-task?workspace=workspace-b",
        )

    with _panel_server(
        acceptance_root,
        context=ActorContext.direct(_auth_principal("unknown")),
        service=service,
        loader=FakeActiveTaskLoader(),
    ) as base_url:
        unknown_status, unknown, _ = _request(
            base_url,
            "/api/tasks?workspace=workspace-a",
        )

    assert (viewer_status, viewer["code"]) == (403, "permission_denied")
    assert (cross_workspace_status, cross_workspace["code"]) == (404, "resource_not_found")
    assert (unknown_status, unknown["code"]) == (404, "resource_not_found")


def test_cloudflare_workflow_route_is_fail_closed_fixture_blocker(
    control_database,
    acceptance_root: Path,
    monkeypatch,
):
    monkeypatch.setenv("LLS_TASK_SOURCE", "control")
    private_key, jwk = _signing_key()
    assertion = _access_assertion(private_key, PANEL_AUDIENCE)
    authenticator = PanelAuthenticator(
        mode="cloudflare_access",
        cloudflare_verifier=_access_verifier(jwk, PANEL_AUDIENCE),
    )

    with _panel_server(
        acceptance_root,
        authenticator=authenticator,
        service=control_database["service"],
        loader=FakeActiveTaskLoader(),
    ) as base_url:
        status, payload, _ = _request(
            base_url,
            "/api/action",
            method="POST",
            body={"task": "http-acceptance-task", "action": "workflow"},
            headers={"Cf-Access-Jwt-Assertion": assertion},
        )

    assert (status, payload["code"]) == (503, "route_authorization_unavailable")
