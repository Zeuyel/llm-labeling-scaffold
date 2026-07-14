from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from llm_labeling_scaffold import panel
from llm_labeling_scaffold.auth import ActorContext, Identity, Principal as AuthPrincipal
from llm_labeling_scaffold.db import (
    AuthorizationUnavailable,
    DatabaseService,
    ExternalIdentity,
    PrincipalType,
    Role,
)
from llm_labeling_scaffold.db.base import Base
from llm_labeling_scaffold.db.database import create_database_engine
from llm_labeling_scaffold.db.enums import AuditChannel
from llm_labeling_scaffold.db.migration import upgrade_database
from llm_labeling_scaffold.db.models import AuditEvent, Principal, RoleBinding, Task, Workspace


class ContextAuthenticator:
    mode = "basic_dev"
    challenge = None

    def __init__(self, context: ActorContext) -> None:
        self.context = context

    def authenticate(self, headers) -> ActorContext:
        return self.context


class FakeActiveTaskLoader:
    def __init__(self, revisions: dict[tuple[str, str], panel.ActiveTaskRevision] | None = None) -> None:
        self.revisions = revisions or {}

    def load_active_revision(self, workspace_slug: str, task_key: str):
        return self.revisions.get((workspace_slug, task_key))


def _auth_principal(subject: str) -> AuthPrincipal:
    return AuthPrincipal(
        kind="user",
        authentication_method="basic_dev",
        identity=Identity(
            issuer="urn:lls:basic-dev",
            subject=subject,
            email=f"{subject}@example.test",
            display_name=subject,
        ),
    )


def _mcp_principal() -> AuthPrincipal:
    return AuthPrincipal(
        kind="service",
        authentication_method="mcp_service_bearer",
        identity=Identity(
            issuer="urn:lls:mcp-service",
            subject="mcp",
            display_name="MCP service",
        ),
    )


@pytest.fixture
def control_database():
    engine = create_database_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session, session.begin():
        principals = {
            subject: Principal(
                issuer="urn:lls:basic-dev",
                subject=subject,
                principal_type=PrincipalType.USER,
            )
            for subject in ("viewer", "annotator", "experimenter", "admin")
        }
        mcp = Principal(
            issuer="urn:lls:mcp-service",
            subject="mcp",
            principal_type=PrincipalType.SERVICE,
        )
        workspace_a = Workspace(slug="workspace-a", name="Workspace A")
        workspace_b = Workspace(slug="workspace-b", name="Workspace B")
        session.add_all([*principals.values(), mcp, workspace_a, workspace_b])
        session.flush()

        task = Task(
            workspace_id=workspace_a.id,
            task_key="panel-control-task",
            name="Panel Control Task",
            created_by_principal_id=principals["admin"].id,
        )
        unpublished = Task(
            workspace_id=workspace_a.id,
            task_key="unpublished-task",
            name="Unpublished Task",
            created_by_principal_id=principals["admin"].id,
        )
        hidden = Task(
            workspace_id=workspace_b.id,
            task_key="hidden-task",
            name="Hidden Task",
            created_by_principal_id=principals["admin"].id,
        )
        session.add_all([task, unpublished, hidden])
        session.flush()
        session.add_all(
            [
                RoleBinding(
                    workspace_id=workspace_a.id,
                    principal_id=principals["viewer"].id,
                    role=Role.VIEWER,
                    created_by_principal_id=principals["admin"].id,
                ),
                RoleBinding(
                    workspace_id=workspace_a.id,
                    task_id=task.id,
                    principal_id=principals["annotator"].id,
                    role=Role.ANNOTATOR,
                    created_by_principal_id=principals["admin"].id,
                ),
                RoleBinding(
                    workspace_id=workspace_a.id,
                    principal_id=principals["experimenter"].id,
                    role=Role.EXPERIMENTER,
                    created_by_principal_id=principals["admin"].id,
                ),
                RoleBinding(
                    workspace_id=workspace_a.id,
                    principal_id=principals["admin"].id,
                    role=Role.ADMIN,
                    created_by_principal_id=principals["admin"].id,
                ),
                RoleBinding(
                    workspace_id=workspace_b.id,
                    principal_id=principals["admin"].id,
                    role=Role.ADMIN,
                    created_by_principal_id=principals["admin"].id,
                ),
            ],
        )

    service = DatabaseService(sessionmaker(bind=engine, class_=Session, expire_on_commit=False))
    try:
        yield {"engine": engine, "service": service}
    finally:
        engine.dispose()


def _draft_spec(task_id: str = "panel-control-task") -> dict:
    return {
        "task_id": task_id,
        "id_field": "record_id",
        "text_fields": ["title"],
        "primary_label_name": "decision",
        "primary_label_values": ["accept", "reject"],
        "data_lake": {
            "source_dataset_id": "panel-dataset",
            "source_object_path": "inputs/panel.jsonl",
            "default_import_id": "panel-import",
            "output_base_uri": "outputs/panel-control-task",
        },
    }


def _active_revision(*, task_config=None) -> panel.ActiveTaskRevision:
    return panel.ActiveTaskRevision(
        revision_id="11111111-1111-1111-1111-111111111111",
        revision_number=1,
        definition=_draft_spec(),
        rendered_task="task_id: panel-control-task\n",
        task_config=task_config,
    )


@contextmanager
def _panel_server(
    tmp_path: Path,
    *,
    context: ActorContext,
    service: DatabaseService | None,
    loader: panel.ActiveTaskLoader | None,
    ready: bool = True,
):
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
    panel._Handler.authenticator = ContextAuthenticator(context)
    panel._Handler.authorization_service = service
    panel._Handler.authorization_ready = ready
    panel._Handler.active_task_loader = loader
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


def _request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    body: dict | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict, dict[str, str]]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        base_url + path,
        data=data,
        headers={"Content-Type": "application/json", **(headers or {})},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8")), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8")), dict(exc.headers)


def test_authorization_runtime_requires_current_schema(tmp_path: Path):
    database_url = f"sqlite+pysqlite:///{tmp_path / 'panel.db'}"
    upgrade_database(database_url, "20260713_0001")

    with pytest.raises(AuthorizationUnavailable):
        panel._build_authorization_service(database_url)

    upgrade_database(database_url, "head")
    service = panel._build_authorization_service(database_url)
    try:
        assert service.get_session(ExternalIdentity("urn:test", "unknown")).workspaces == ()
    finally:
        service.close()


def test_session_and_workspace_selection_fail_closed(control_database, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LLS_TASK_SOURCE", "control")
    service = control_database["service"]
    loader = FakeActiveTaskLoader({("workspace-a", "panel-control-task"): _active_revision()})

    with _panel_server(
        tmp_path,
        context=ActorContext.direct(_auth_principal("experimenter")),
        service=service,
        loader=loader,
    ) as base_url:
        status, session, _ = _request(base_url, "/api/session")
        list_status, listed, _ = _request(base_url, "/api/tasks")

    assert status == 200
    assert session["authorization"]["state"] == "ready"
    assert [item["slug"] for item in session["authorization"]["workspaces"]] == ["workspace-a"]
    assert "token" not in json.dumps(session).lower()
    assert "claims" not in json.dumps(session).lower()
    assert "jwks" not in json.dumps(session).lower()
    assert list_status == 200
    assert listed["workspace"] == "workspace-a"

    with _panel_server(
        tmp_path,
        context=ActorContext.direct(_auth_principal("admin")),
        service=service,
        loader=loader,
    ) as base_url:
        missing_status, missing, _ = _request(base_url, "/api/tasks")
        explicit_status, explicit, _ = _request(base_url, "/api/tasks?workspace=workspace-a")

    assert (missing_status, missing["code"]) == (422, "workspace_required")
    assert explicit_status == 200
    assert explicit["workspace"] == "workspace-a"

    with _panel_server(
        tmp_path,
        context=ActorContext.direct(_auth_principal("unknown")),
        service=service,
        loader=loader,
    ) as base_url:
        unknown_session_status, unknown_session, _ = _request(base_url, "/api/session")
        unknown_list_status, unknown_list, _ = _request(base_url, "/api/tasks?workspace=workspace-a")

    assert unknown_session_status == 200
    assert unknown_session["authorization"]["workspaces"] == []
    assert (unknown_list_status, unknown_list["code"]) == (404, "resource_not_found")

    with _panel_server(
        tmp_path,
        context=ActorContext.direct(_auth_principal("experimenter")),
        service=None,
        loader=loader,
        ready=False,
    ) as base_url:
        unavailable_status, unavailable, _ = _request(base_url, "/api/session")

    assert (unavailable_status, unavailable["code"]) == (503, "authorization_unavailable")


@pytest.mark.parametrize("subject", ["viewer", "annotator"])
def test_read_only_roles_see_only_active_revision_and_cannot_read_draft(
    control_database,
    tmp_path: Path,
    monkeypatch,
    subject: str,
):
    monkeypatch.setenv("LLS_TASK_SOURCE", "control")
    service = control_database["service"]
    loader = FakeActiveTaskLoader({("workspace-a", "panel-control-task"): _active_revision()})
    context = ActorContext.direct(_auth_principal(subject))

    with _panel_server(tmp_path, context=context, service=service, loader=loader) as base_url:
        list_status, listed, _ = _request(base_url, "/api/tasks")
        detail_status, detail, detail_headers = _request(base_url, "/api/tasks/panel-control-task")
        draft_status, draft, _ = _request(base_url, "/api/task/control?task_id=panel-control-task")

    assert list_status == 200
    assert [item["task_id"] for item in listed["tasks"]] == ["panel-control-task"]
    assert detail_status == 200
    assert detail["task"]["revision"] == 1
    assert "draft" not in json.dumps(listed).lower()
    assert "dirty" not in json.dumps(listed).lower()
    assert "draft" not in json.dumps(detail).lower()
    assert "ETag" not in detail_headers
    assert (draft_status, draft["code"]) == (403, "permission_denied")


def test_draft_etag_publish_errors_and_audit_context(control_database, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LLS_TASK_SOURCE", "control")
    service = control_database["service"]
    context = ActorContext.direct(_auth_principal("experimenter"))

    with _panel_server(tmp_path, context=context, service=service, loader=FakeActiveTaskLoader()) as base_url:
        create_status, created, create_headers = _request(
            base_url,
            "/api/tasks",
            method="POST",
            body=_draft_spec(),
        )
        etag = create_headers["ETag"]
        draft_status, draft, draft_headers = _request(
            base_url,
            "/api/task/control?task_id=panel-control-task",
        )
        missing_status, missing, _ = _request(
            base_url,
            "/api/tasks/panel-control-task",
            method="PUT",
            body={**_draft_spec(), "annotation_guidelines": "missing"},
        )
        weak_status, weak, _ = _request(
            base_url,
            "/api/tasks/panel-control-task",
            method="PUT",
            body={**_draft_spec(), "annotation_guidelines": "weak"},
            headers={"If-Match": f"W/{etag}"},
        )
        invalid_status, invalid, _ = _request(
            base_url,
            "/api/tasks/panel-control-task",
            method="PUT",
            body={**_draft_spec(), "text_fields": []},
            headers={"If-Match": etag},
        )
        update_status, updated, update_headers = _request(
            base_url,
            "/api/tasks/panel-control-task",
            method="PUT",
            body={**_draft_spec(), "annotation_guidelines": "updated"},
            headers={"If-Match": etag},
        )
        current_etag = update_headers["ETag"]
        stale_status, stale, stale_headers = _request(
            base_url,
            "/api/tasks/panel-control-task",
            method="PUT",
            body={**_draft_spec(), "annotation_guidelines": "stale"},
            headers={"If-Match": etag},
        )
        no_reason_status, no_reason, _ = _request(
            base_url,
            "/api/tasks/panel-control-task/publish",
            method="POST",
            body={"confirm": True, "idempotency_key": "publish-001"},
            headers={"If-Match": current_etag},
        )
        no_match_status, no_match, _ = _request(
            base_url,
            "/api/tasks/panel-control-task/publish",
            method="POST",
            body={"confirm": True, "idempotency_key": "publish-001", "reason": "release"},
        )
        publish_status, published, _ = _request(
            base_url,
            "/api/tasks/panel-control-task/publish",
            method="POST",
            body={"confirm": True, "idempotency_key": "publish-001", "reason": "release"},
            headers={"If-Match": current_etag},
        )

    assert create_status == 200
    assert etag.startswith('"task-draft-v1-') and etag.endswith('"')
    assert draft_status == 200
    assert draft_headers["ETag"] == etag
    assert draft["record"]["draft_etag"] == etag
    assert (missing_status, missing["code"]) == (428, "task_precondition_required")
    assert (weak_status, weak["code"]) == (422, "invalid_definition")
    assert (invalid_status, invalid["code"]) == (422, "invalid_definition")
    assert update_status == 200
    assert updated["record"]["draft_version"] == 2
    assert current_etag != etag
    assert (stale_status, stale["code"]) == (409, "task_draft_conflict")
    assert stale_headers["ETag"] == current_etag
    assert (no_reason_status, no_reason["code"]) == (422, "invalid_definition")
    assert (no_match_status, no_match["code"]) == (428, "task_precondition_required")
    assert publish_status == 202
    assert published["published"]["materialization_state"] == "pending"

    with Session(control_database["engine"]) as session:
        events = session.scalars(select(AuditEvent).order_by(AuditEvent.occurred_at)).all()
        actors = {session.get(Principal, event.actor_principal_id).subject for event in events}
        callers = {session.get(Principal, event.caller_principal_id).subject for event in events}
    assert actors == {"experimenter"}
    assert callers == {"experimenter"}
    assert {event.channel for event in events} == {AuditChannel.PANEL}
    assert not (tmp_path / "runs" / "_system" / "task_control" / "registry.json").exists()


def test_visibility_permission_loader_and_context_errors(control_database, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LLS_TASK_SOURCE", "control")
    service = control_database["service"]
    loader = FakeActiveTaskLoader({("workspace-a", "panel-control-task"): _active_revision()})

    with _panel_server(
        tmp_path,
        context=ActorContext.direct(_auth_principal("experimenter")),
        service=service,
        loader=loader,
    ) as base_url:
        hidden_status, hidden, _ = _request(
            base_url,
            "/api/tasks/hidden-task?workspace=workspace-b",
        )
        conflict_status, conflict, _ = _request(
            base_url,
            "/api/tasks?workspace=workspace-a&workspace_slug=workspace-b",
        )
        spoof_status, spoof, _ = _request(
            base_url,
            "/api/tasks",
            method="POST",
            body={**_draft_spec(), "actor": "attacker", "channel": "mcp"},
        )

    assert (hidden_status, hidden["code"]) == (404, "resource_not_found")
    assert (conflict_status, conflict["code"]) == (422, "workspace_selector_conflict")
    assert (spoof_status, spoof["code"]) == (422, "invalid_definition")

    with _panel_server(
        tmp_path,
        context=ActorContext.direct(_auth_principal("viewer")),
        service=service,
        loader=loader,
    ) as base_url:
        denied_status, denied, _ = _request(
            base_url,
            "/api/tasks/panel-control-task/publish",
            method="POST",
            body={"confirm": True, "idempotency_key": "denied", "reason": "denied"},
            headers={"If-Match": '"task-draft-v1-' + "a" * 64 + '"'},
        )
    assert (denied_status, denied["code"]) == (403, "permission_denied")

    with _panel_server(
        tmp_path,
        context=ActorContext.direct(_auth_principal("viewer")),
        service=service,
        loader=None,
    ) as base_url:
        loader_status, loader_error, _ = _request(base_url, "/api/tasks/panel-control-task")
    assert (loader_status, loader_error["code"]) == (503, "active_task_loader_unavailable")


def test_mcp_delegation_records_actor_caller_and_server_channel(control_database, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LLS_TASK_SOURCE", "control")
    monkeypatch.setenv("LLS_MCP_ENABLE_WRITES", "1")
    context = ActorContext(actor=_auth_principal("experimenter"), caller=_mcp_principal())

    with _panel_server(
        tmp_path,
        context=context,
        service=control_database["service"],
        loader=FakeActiveTaskLoader(),
    ) as base_url:
        status, created, _ = _request(base_url, "/api/tasks", method="POST", body=_draft_spec())

    assert status == 200
    assert created["action"] == "created"
    with Session(control_database["engine"]) as session:
        event = session.scalar(select(AuditEvent).where(AuditEvent.event_type == "task.draft_created"))
        actor = session.get(Principal, event.actor_principal_id)
        caller = session.get(Principal, event.caller_principal_id)
    assert actor.subject == "experimenter"
    assert caller.subject == "mcp"
    assert event.channel == AuditChannel.MCP


def test_data_lake_import_requires_acl_and_active_materialization(
    control_database,
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("LLS_TASK_SOURCE", "control")
    monkeypatch.setattr(panel, "_apply_runtime_settings", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        panel.pipeline,
        "dry_run_data_lake_import",
        lambda *_args, **_kwargs: {"validation": {"ok": True}},
    )
    monkeypatch.setattr(
        panel.pipeline,
        "start_data_lake_import",
        lambda *_args, **_kwargs: {"job_id": "job-1"},
    )
    loader = FakeActiveTaskLoader(
        {("workspace-a", "panel-control-task"): _active_revision(task_config=object())},
    )

    with _panel_server(
        tmp_path,
        context=ActorContext.direct(_auth_principal("experimenter")),
        service=control_database["service"],
        loader=loader,
    ) as base_url:
        dry_status, dry, _ = _request(
            base_url,
            "/api/import/data_lake",
            method="POST",
            body={"task_id": "panel-control-task", "dry_run": True},
        )
        submit_status, submit, _ = _request(
            base_url,
            "/api/import/data_lake",
            method="POST",
            body={
                "task_id": "panel-control-task",
                "confirm": True,
                "idempotency_key": "import-001",
            },
        )

    assert dry_status == 200
    assert dry["dry_run"] is True
    assert submit_status == 200
    assert submit["job"]["job_id"] == "job-1"
    with Session(control_database["engine"]) as session:
        event = session.scalar(select(AuditEvent).where(AuditEvent.event_type == "task.import_requested"))
    assert event.channel == AuditChannel.PANEL

    with _panel_server(
        tmp_path,
        context=ActorContext.direct(_auth_principal("viewer")),
        service=control_database["service"],
        loader=loader,
    ) as base_url:
        denied_status, denied, _ = _request(
            base_url,
            "/api/import/data_lake",
            method="POST",
            body={"task_id": "panel-control-task", "dry_run": True},
        )
    assert (denied_status, denied["code"]) == (403, "permission_denied")
