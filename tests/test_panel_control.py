from __future__ import annotations

import asyncio
import json
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from llm_labeling_scaffold import data_lake, panel
from llm_labeling_scaffold.auth import (
    ActorContext,
    CloudflareAccessVerifier,
    Identity,
    PanelAuthenticator,
    Principal as AuthPrincipal,
)
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
from llm_labeling_scaffold.db.models import (
    AuditEvent,
    IdempotencyRecord,
    Principal,
    RoleBinding,
    Task,
    TaskDraft,
    Workspace,
)
from llm_labeling_scaffold.mcp_server import (
    McpServerConfig,
    PanelApiClient,
    create_streamable_http_app,
)


ISSUER = "https://team.cloudflareaccess.com"
PANEL_AUDIENCE = "panel-application-audience"
MCP_AUDIENCE = "mcp-application-audience"
SUBJECT = "mcp-user-7335d417"
INTERNAL_TOKEN = "mcp-internal-0123456789-abcdef-012"


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


def _base64url_uint(value: int) -> str:
    width = (value.bit_length() + 7) // 8
    return jwt.utils.base64url_encode(value.to_bytes(width, "big")).decode("ascii")


def _signing_key() -> tuple[rsa.RSAPrivateKey, dict[str, str]]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = private_key.public_key().public_numbers()
    return private_key, {
        "kid": "key-1",
        "kty": "RSA",
        "alg": "RS256",
        "use": "sig",
        "e": _base64url_uint(numbers.e),
        "n": _base64url_uint(numbers.n),
    }


def _access_assertion(private_key: rsa.RSAPrivateKey, audience: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "aud": [audience],
            "exp": now + 300,
            "iat": now - 1,
            "iss": ISSUER,
            "nbf": now - 1,
            "sub": SUBJECT,
            "type": "app",
            "email": "mcp-user@example.test",
            "name": "MCP User",
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "key-1", "typ": "JWT"},
    )


def _access_verifier(jwk: dict[str, str], audience: str) -> CloudflareAccessVerifier:
    return CloudflareAccessVerifier(
        ISSUER,
        audience,
        jwks_fetcher=lambda *_: {"keys": [jwk]},
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
        cloudflare_user = Principal(
            issuer=ISSUER,
            subject=SUBJECT,
            principal_type=PrincipalType.USER,
        )
        workspace_a = Workspace(slug="workspace-a", name="Workspace A")
        workspace_b = Workspace(slug="workspace-b", name="Workspace B")
        session.add_all([*principals.values(), mcp, cloudflare_user, workspace_a, workspace_b])
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
                    principal_id=cloudflare_user.id,
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
    context: ActorContext | None = None,
    authenticator=None,
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
    if authenticator is None:
        if context is None:
            raise ValueError("context or authenticator is required")
        authenticator = ContextAuthenticator(context)
    panel._Handler.authenticator = authenticator
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


def test_authorization_runtime_readiness_requires_control_mode_and_active_loader(monkeypatch):
    service = object()
    loader = FakeActiveTaskLoader()

    monkeypatch.setenv("LLS_TASK_SOURCE", "control")
    assert panel._authorization_runtime_ready(service, loader) is True
    assert panel._authorization_runtime_ready(None, loader) is False
    assert panel._authorization_runtime_ready(service, None) is False

    monkeypatch.setenv("LLS_TASK_SOURCE", "local")
    assert panel._authorization_runtime_ready(service, loader) is False


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
    task_id = "created-control-task"
    spec = {**_draft_spec(), "task_id": task_id}

    with _panel_server(tmp_path, context=context, service=service, loader=FakeActiveTaskLoader()) as base_url:
        create_status, created, create_headers = _request(
            base_url,
            "/api/tasks",
            method="POST",
            body={**spec, "idempotency_key": "create-control-task-001"},
        )
        capabilities_status, capabilities, _ = _request(base_url, "/api/capabilities")
        etag = create_headers["ETag"]
        draft_status, draft, draft_headers = _request(
            base_url,
            f"/api/task/control?task_id={task_id}",
        )
        missing_status, missing, _ = _request(
            base_url,
            f"/api/tasks/{task_id}",
            method="PUT",
            body={**spec, "annotation_guidelines": "missing"},
        )
        weak_status, weak, _ = _request(
            base_url,
            f"/api/tasks/{task_id}",
            method="PUT",
            body={**spec, "annotation_guidelines": "weak"},
            headers={"If-Match": f"W/{etag}"},
        )
        invalid_status, invalid, _ = _request(
            base_url,
            f"/api/tasks/{task_id}",
            method="PUT",
            body={**spec, "text_fields": []},
            headers={"If-Match": etag},
        )
        update_status, updated, update_headers = _request(
            base_url,
            f"/api/tasks/{task_id}",
            method="PUT",
            body={**spec, "annotation_guidelines": "updated"},
            headers={"If-Match": etag},
        )
        current_etag = update_headers["ETag"]
        stale_status, stale, stale_headers = _request(
            base_url,
            f"/api/tasks/{task_id}",
            method="PUT",
            body={**spec, "annotation_guidelines": "stale"},
            headers={"If-Match": etag},
        )
        no_reason_status, no_reason, _ = _request(
            base_url,
            f"/api/tasks/{task_id}/publish",
            method="POST",
            body={"confirm": True, "idempotency_key": "publish-001"},
            headers={"If-Match": current_etag},
        )
        no_match_status, no_match, _ = _request(
            base_url,
            f"/api/tasks/{task_id}/publish",
            method="POST",
            body={"confirm": True, "idempotency_key": "publish-001", "reason": "release"},
        )
        publish_status, published, _ = _request(
            base_url,
            f"/api/tasks/{task_id}/publish",
            method="POST",
            body={"confirm": True, "idempotency_key": "publish-001", "reason": "release"},
            headers={"If-Match": current_etag},
        )

    assert create_status == 201
    assert capabilities_status == 200
    publish_endpoint = next(
        item
        for item in capabilities["endpoints"]
        if item["method"] == "POST" and item["path"] == "/api/tasks/{task_id}/publish"
    )
    assert "reason" in publish_endpoint["request_schema"]["required"]
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
    assert (stale_status, stale["code"]) == (412, "task_draft_conflict")
    assert stale_headers["ETag"] == current_etag
    assert (no_reason_status, no_reason["code"]) == (422, "task_publish_reason_required")
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


def test_control_task_create_is_idempotent_and_audited(control_database, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LLS_TASK_SOURCE", "control")
    service = control_database["service"]
    context = ActorContext.direct(_auth_principal("experimenter"))
    task_id = "idempotent-created-task"
    spec = {**_draft_spec(), "task_id": task_id}

    with _panel_server(tmp_path, context=context, service=service, loader=FakeActiveTaskLoader()) as base_url:
        first_status, first, first_headers = _request(
            base_url,
            "/api/tasks",
            method="POST",
            body={**spec, "idempotency_key": "task-create-001"},
        )
        replay_status, replay, replay_headers = _request(
            base_url,
            "/api/tasks",
            method="POST",
            body={**spec, "idempotency_key": "task-create-001"},
        )
        conflict_status, conflict, _ = _request(
            base_url,
            "/api/tasks",
            method="POST",
            body={**spec, "idempotency_key": "task-create-002"},
        )
        fingerprint_conflict_status, fingerprint_conflict, _ = _request(
            base_url,
            "/api/tasks",
            method="POST",
            body={
                **spec,
                "annotation_guidelines": "different request",
                "idempotency_key": "task-create-001",
            },
        )

    assert first_status == 201
    assert first["action"] == "created"
    assert replay_status == 201
    assert replay["action"] == "replayed"
    assert replay_headers["ETag"] == first_headers["ETag"]
    assert (conflict_status, conflict["code"]) == (409, "task_create_conflict")
    assert (fingerprint_conflict_status, fingerprint_conflict["code"]) == (409, "idempotency_conflict")

    with Session(control_database["engine"]) as session:
        task = session.scalar(select(Task).where(Task.task_key == task_id))
        assert task is not None
        assert session.scalar(select(TaskDraft).where(TaskDraft.task_id == task.id)) is not None
        assert session.scalar(
            select(func.count()).select_from(Task).where(Task.task_key == task_id)
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(IdempotencyRecord).where(
                IdempotencyRecord.operation == "task.create"
            )
        ) == 1
        event_types = {
            event.event_type
            for event in session.scalars(
                select(AuditEvent).where(AuditEvent.resource_id == str(task.id))
            )
        }
    assert {"task.created", "task.draft_created"}.issubset(event_types)


def test_control_task_list_reports_published_with_draft(control_database, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LLS_TASK_SOURCE", "control")
    service = control_database["service"]
    definition = {**_draft_spec(), "annotation_guidelines": "changed draft"}
    rendered_task = "task_id: panel-control-task\nannotation: changed\n"
    service.save_task_draft(
        actor_identity=ExternalIdentity("urn:lls:basic-dev", "experimenter"),
        caller_identity=ExternalIdentity("urn:lls:basic-dev", "experimenter"),
        workspace_slug="workspace-a",
        task_key="panel-control-task",
        definition=definition,
        rendered_task=rendered_task,
        if_match=None,
        if_none_match="*",
        channel=AuditChannel.PANEL,
    )
    loader = FakeActiveTaskLoader({("workspace-a", "panel-control-task"): _active_revision()})

    with _panel_server(
        tmp_path,
        context=ActorContext.direct(_auth_principal("experimenter")),
        service=service,
        loader=loader,
    ) as base_url:
        status, payload, _ = _request(base_url, "/api/tasks?workspace=workspace-a")

    item = next(item for item in payload["tasks"] if item["task_id"] == "panel-control-task")
    assert status == 200
    assert item["status"] == "published_with_draft"
    assert item["draft_version"] == 1


def test_control_task_lifecycle_routes_are_idempotent_and_hide_archived_tasks(
    control_database,
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("LLS_TASK_SOURCE", "control")
    service = control_database["service"]
    loader = FakeActiveTaskLoader({("workspace-a", "panel-control-task"): _active_revision()})

    with _panel_server(
        tmp_path,
        context=ActorContext.direct(_auth_principal("experimenter")),
        service=service,
        loader=loader,
    ) as base_url:
        disable_status, disabled, _ = _request(
            base_url,
            "/api/tasks/panel-control-task/disable?workspace=workspace-a",
            method="POST",
            body={"reason": "pause", "idempotency_key": "panel-disable-1"},
        )
        disable_replay_status, disable_replay, _ = _request(
            base_url,
            "/api/tasks/panel-control-task/disable?workspace=workspace-a",
            method="POST",
            body={"reason": "pause", "idempotency_key": "panel-disable-1"},
        )
        archive_status, archived, _ = _request(
            base_url,
            "/api/tasks/panel-control-task/archive?workspace=workspace-a",
            method="POST",
            body={"reason": "retention", "idempotency_key": "panel-archive-1"},
        )
        invalid_status, invalid, _ = _request(
            base_url,
            "/api/tasks/panel-control-task/disable?workspace=workspace-a",
            method="POST",
            body={"reason": "invalid", "idempotency_key": "panel-invalid-1"},
        )
        list_status, listed, _ = _request(base_url, "/api/tasks?workspace=workspace-a")
        archived_list_status, archived_list, _ = _request(
            base_url,
            "/api/tasks?workspace=workspace-a&include_archived=true",
        )
        detail_status, detail, _ = _request(
            base_url,
            "/api/tasks/panel-control-task?workspace=workspace-a",
        )
        restore_status, restored, _ = _request(
            base_url,
            "/api/tasks/panel-control-task/restore?workspace=workspace-a",
            method="POST",
            body={"reason": "reopen", "idempotency_key": "panel-restore-1"},
        )

    assert (disable_status, disabled["task"]["lifecycle_state"]) == (200, "disabled")
    assert (disable_replay_status, disable_replay["replayed"]) == (200, True)
    assert (archive_status, archived["task"]["lifecycle_state"]) == (200, "archived")
    assert (invalid_status, invalid["code"]) == (409, "task_lifecycle_transition_invalid")
    assert list_status == 200
    assert not any(item["task_id"] == "panel-control-task" for item in listed["tasks"])
    assert archived_list_status == 200
    archived_item = next(item for item in archived_list["tasks"] if item["task_id"] == "panel-control-task")
    assert archived_item["lifecycle_state"] == "archived"
    assert (detail_status, detail["code"]) == (409, "task_lifecycle_not_active")
    assert (restore_status, restored["task"]["lifecycle_state"]) == (200, "active")


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
    spec = {**_draft_spec(), "task_id": "mcp-created-task"}

    with _panel_server(
        tmp_path,
        context=context,
        service=control_database["service"],
        loader=FakeActiveTaskLoader(),
    ) as base_url:
        status, created, _ = _request(base_url, "/api/tasks", method="POST", body=spec)

    assert status == 201
    assert created["action"] == "created"
    with Session(control_database["engine"]) as session:
        event = session.scalar(select(AuditEvent).where(AuditEvent.event_type == "task.draft_created"))
        actor = session.get(Principal, event.actor_principal_id)
        caller = session.get(Principal, event.caller_principal_id)
    assert actor.subject == "experimenter"
    assert caller.subject == "mcp"
    assert event.channel == AuditChannel.MCP


def test_mcp_asgi_tool_to_panel_http_preserves_delegated_actor_and_independent_audience(
    control_database,
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("LLS_TASK_SOURCE", "control")
    monkeypatch.setenv("LLS_MCP_ENABLE_WRITES", "1")
    private_key, jwk = _signing_key()
    panel_assertion = _access_assertion(private_key, PANEL_AUDIENCE)
    mcp_assertion = _access_assertion(private_key, MCP_AUDIENCE)
    panel_authenticator = PanelAuthenticator(
        mode="cloudflare_access",
        cloudflare_verifier=_access_verifier(jwk, PANEL_AUDIENCE),
        mcp_cloudflare_verifier=_access_verifier(jwk, MCP_AUDIENCE),
        internal_token=INTERNAL_TOKEN,
    )

    with _panel_server(
        tmp_path,
        authenticator=panel_authenticator,
        service=control_database["service"],
        loader=FakeActiveTaskLoader(),
    ) as base_url:
        config = McpServerConfig(
            panel_url=base_url,
            internal_token=INTERNAL_TOKEN,
            auth_mode="cloudflare_access",
            cloudflare_issuer=ISSUER,
            cloudflare_audience=MCP_AUDIENCE,
            panel_cloudflare_audience=PANEL_AUDIENCE,
            enable_writes=True,
        )
        panel_client = PanelApiClient(config)
        app = create_streamable_http_app(
            config,
            panel_client=panel_client,
            cloudflare_verifier=_access_verifier(jwk, MCP_AUDIENCE),
        )

        async def run_asgi_requests():
            transport = httpx.ASGITransport(app=app)
            try:
                async with app.app.router.lifespan_context(app.app):
                    async with httpx.AsyncClient(
                        transport=transport,
                        base_url="http://localhost:8766",
                        headers={"Cf-Access-Jwt-Assertion": mcp_assertion},
                    ) as client:
                        async with streamable_http_client(
                            "http://localhost:8766/mcp",
                            http_client=client,
                            terminate_on_close=False,
                        ) as streams:
                            async with ClientSession(streams[0], streams[1]) as session:
                                await session.initialize()
                                valid = await session.call_tool(
                                    "scaffold_task_draft_create",
                                    {
                                        "spec": {**_draft_spec(), "task_id": "mcp-asgi-created-task"},
                                        "workspace": "workspace-a",
                                    },
                                )
                        wrong_audience = await client.post(
                            "/mcp",
                            headers={"Cf-Access-Jwt-Assertion": panel_assertion},
                        )
                return valid, wrong_audience
            finally:
                await panel_client.aclose()

        valid, wrong_audience = asyncio.run(run_asgi_requests())
        direct_mcp_status, direct_mcp, _ = _request(
            base_url,
            "/api/tasks?workspace=workspace-a",
            headers={"Cf-Access-Jwt-Assertion": mcp_assertion},
        )
        bearer_only_status, bearer_only, _ = _request(
            base_url,
            "/api/tasks?workspace=workspace-a",
            headers={"Authorization": f"Bearer {INTERNAL_TOKEN}"},
        )
        invalid_delegation_status, invalid_delegation, _ = _request(
            base_url,
            "/api/tasks?workspace=workspace-a",
            headers={
                "Authorization": f"Bearer {INTERNAL_TOKEN}",
                "Cf-Access-Jwt-Assertion": panel_assertion,
            },
        )

    assert valid.isError is False
    assert (wrong_audience.status_code, wrong_audience.json()["code"]) == (
        403,
        "access_assertion_not_for_application",
    )
    assert (direct_mcp_status, direct_mcp["code"]) == (403, "access_assertion_not_for_application")
    assert (bearer_only_status, bearer_only["code"]) == (403, "delegated_actor_required")
    assert (invalid_delegation_status, invalid_delegation["code"]) == (
        403,
        "access_assertion_not_for_application",
    )

    with Session(control_database["engine"]) as session:
        event = session.scalar(select(AuditEvent).where(AuditEvent.event_type == "task.draft_created"))
        actor = session.get(Principal, event.actor_principal_id)
        caller = session.get(Principal, event.caller_principal_id)
    assert (actor.issuer, actor.subject) == (ISSUER, SUBJECT)
    assert (caller.issuer, caller.subject) == ("urn:lls:mcp-service", "mcp")
    assert event.channel == AuditChannel.MCP


def test_data_lake_import_requires_acl_and_active_materialization(
    control_database,
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("LLS_TASK_SOURCE", "control")
    monkeypatch.setattr(panel, "_apply_runtime_settings", lambda *_args, **_kwargs: {})
    runtime_roots: list[Path] = []

    def dry_run(runs_root, *_args, **_kwargs):
        runtime_roots.append(Path(runs_root))
        return {"validation": {"ok": True}}

    def submit(runs_root, *_args, **_kwargs):
        runtime_roots.append(Path(runs_root))
        return {"job_id": "job-1"}

    monkeypatch.setattr(
        panel.pipeline,
        "dry_run_data_lake_import",
        dry_run,
    )
    monkeypatch.setattr(
        panel.pipeline,
        "start_data_lake_import",
        submit,
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
            body={"task_id": "panel-control-task", "workspace": "workspace-a", "dry_run": True},
        )
        submit_status, submit, _ = _request(
            base_url,
            "/api/import/data_lake",
            method="POST",
            body={
                "task_id": "panel-control-task",
                "workspace": "workspace-a",
                "confirm": True,
                "idempotency_key": "import-001",
            },
        )

    assert dry_status == 200
    assert dry["dry_run"] is True
    assert submit_status == 200
    assert submit["job"]["job_id"] == "job-1"
    assert runtime_roots == [
        tmp_path / "runs" / "workspace-a",
        tmp_path / "runs" / "workspace-a",
    ]
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
            body={"task_id": "panel-control-task", "workspace": "workspace-a", "dry_run": True},
        )
    assert (denied_status, denied["code"]) == (403, "permission_denied")


def test_control_runtime_reads_require_task_acl_and_isolate_workspace_paths(
    control_database,
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("LLS_TASK_SOURCE", "control")
    monkeypatch.setattr(panel, "_apply_runtime_settings", lambda *_args, **_kwargs: {})
    observed: list[tuple[str, Path, str]] = []

    def list_imports(runs_root, task_id, **_kwargs):
        observed.append(("imports", Path(runs_root), task_id))
        return [{"import_id": Path(runs_root).name}]

    def import_detail(runs_root, task_id, import_id, **_kwargs):
        observed.append(("import", Path(runs_root), task_id))
        return {"import_id": import_id, "namespace": Path(runs_root).name}

    def jobs_for_task(runs_root, task_id):
        observed.append(("jobs", Path(runs_root), task_id))
        return [{"job_id": Path(runs_root).name}]

    monkeypatch.setattr(panel.pipeline, "list_imports", list_imports)
    monkeypatch.setattr(panel.pipeline, "import_detail", import_detail)
    monkeypatch.setattr(panel.pipeline, "jobs_for_task", jobs_for_task)
    monkeypatch.setattr(data_lake, "preview_source", lambda task: {"source": task.data_lake["source_dataset_id"]})

    def task_config(workspace: str):
        return SimpleNamespace(
            id_field="record_id",
            path=tmp_path / workspace / "task.yaml",
            profile="manual_labeling_cv_v1",
            data_lake={"source_dataset_id": workspace},
        )

    loader = FakeActiveTaskLoader({
        ("workspace-a", "panel-control-task"): _active_revision(task_config=task_config("workspace-a")),
        ("workspace-b", "hidden-task"): _active_revision(task_config=task_config("workspace-b")),
    })

    handler = SimpleNamespace(runs_root=tmp_path / "runs")
    same_key_a = panel._Handler._workspace_runs_root(handler, "workspace-a") / "same-task"
    same_key_b = panel._Handler._workspace_runs_root(handler, "workspace-b") / "same-task"
    assert same_key_a == tmp_path / "runs" / "workspace-a" / "same-task"
    assert same_key_b == tmp_path / "runs" / "workspace-b" / "same-task"
    assert same_key_a != same_key_b
    with pytest.raises(panel._PanelRouteError) as exc_info:
        panel._Handler._workspace_runs_root(handler, ".")
    assert exc_info.value.code == "invalid_workspace"

    with _panel_server(
        tmp_path,
        context=ActorContext.direct(_auth_principal("admin")),
        service=control_database["service"],
        loader=loader,
    ) as base_url:
        imports_a_status, imports_a, _ = _request(
            base_url,
            "/api/task/imports?task_id=panel-control-task&workspace=workspace-a",
        )
        imports_b_status, imports_b, _ = _request(
            base_url,
            "/api/task/imports?task_id=hidden-task&workspace=workspace-b",
        )
        detail_status, detail, _ = _request(
            base_url,
            "/api/import/detail?task_id=hidden-task&import_id=import-1&workspace=workspace-b",
        )
        jobs_status, jobs, _ = _request(
            base_url,
            "/api/jobs?task_id=panel-control-task&workspace=workspace-a",
        )
        preview_status, preview, _ = _request(
            base_url,
            "/api/task/data_lake?task_id=hidden-task&workspace=workspace-b",
        )
        check_status, check, _ = _request(
            base_url,
            "/api/tasks/panel-control-task/check",
            method="POST",
            body={"workspace": "workspace-a"},
        )

    assert imports_a_status == imports_b_status == detail_status == jobs_status == preview_status == check_status == 200
    assert imports_a["imports"][0]["import_id"] == "workspace-a"
    assert imports_b["imports"][0]["import_id"] == "workspace-b"
    assert detail["import"]["namespace"] == "workspace-b"
    assert jobs["jobs"][0]["job_id"] == "workspace-a"
    assert preview["preview"]["source"] == "workspace-b"
    assert check["ok"] is True
    assert observed == [
        ("imports", tmp_path / "runs" / "workspace-a", "panel-control-task"),
        ("imports", tmp_path / "runs" / "workspace-b", "hidden-task"),
        ("import", tmp_path / "runs" / "workspace-b", "hidden-task"),
        ("jobs", tmp_path / "runs" / "workspace-a", "panel-control-task"),
    ]

    with _panel_server(
        tmp_path,
        context=ActorContext.direct(_auth_principal("experimenter")),
        service=control_database["service"],
        loader=loader,
    ) as base_url:
        hidden_status, hidden, _ = _request(
            base_url,
            "/api/jobs?task_id=hidden-task&workspace=workspace-b",
        )

    assert (hidden_status, hidden["code"]) == (404, "resource_not_found")
