from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import UUID

import pytest
from http.server import ThreadingHTTPServer

from llm_labeling_scaffold import panel
from llm_labeling_scaffold.auth import build_panel_authenticator
from llm_labeling_scaffold.db import AuthorizationDenied, AuthorizationReason, IdempotencyConflict
from llm_labeling_scaffold.panel_annotators import ExternalAnnotatorSnapshot


WORKSPACE = "workspace-a"
MAPPING_ID = UUID("11111111-1111-1111-1111-111111111111")
ARGILLA_USER_ID = UUID("22222222-2222-2222-2222-222222222222")
ARGILLA_WORKSPACE_ID = UUID("33333333-3333-3333-3333-333333333333")
BINDING_ID = UUID("44444444-4444-4444-4444-444444444444")
COHORT_ID = UUID("55555555-5555-5555-5555-555555555555")
PRINCIPAL_ID = UUID("66666666-6666-6666-6666-666666666666")


class _FakeAuthorizationService:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def require_workspace(self, actor, workspace, permission):
        self.calls.append({"actor": actor, "workspace": workspace, "permission": permission})
        if workspace == "hidden":
            raise AuthorizationDenied(SimpleNamespace(reason=AuthorizationReason.RESOURCE_NOT_VISIBLE))
        if workspace == "forbidden":
            raise AuthorizationDenied(SimpleNamespace(reason=AuthorizationReason.ROLE_DENIED))
        return SimpleNamespace(workspace=SimpleNamespace(slug=workspace))

    def require_workspace_member_identity(self, *, workspace_slug, principal_id, permission):
        if workspace_slug != WORKSPACE or principal_id != PRINCIPAL_ID:
            raise AuthorizationDenied(SimpleNamespace(reason=AuthorizationReason.RESOURCE_NOT_VISIBLE))
        return SimpleNamespace(
            id=principal_id,
            issuer="https://target.example",
            subject="annotator-subject",
        )


class _FakeMember:
    def __init__(self, mapping, capacity: int) -> None:
        self.mapping = mapping
        self.default_capacity = capacity


class _FakeRevision:
    def __init__(self, members: list[_FakeMember], number: int = 1) -> None:
        self.members = tuple(members)
        self.revision_number = number


class _FakeCohort:
    def __init__(self, *, name: str = "reviewers", revision: int = 1) -> None:
        self.id = COHORT_ID
        self.name = name
        self.latest_revision = _FakeRevision([], revision)


class _FakeRepository:
    def __init__(self) -> None:
        self.mapping = SimpleNamespace(
            id=MAPPING_ID,
            principal_subject="annotator-subject",
            principal_id=PRINCIPAL_ID,
            argilla_user_id=ARGILLA_USER_ID,
            username="annotator-one",
            personal_argilla_workspace_id=ARGILLA_WORKSPACE_ID,
            verification_state="verified",
            verified_at=datetime.now(timezone.utc),
            argilla_role="annotator",
            membership_verified=True,
        )
        self.binding = SimpleNamespace(id=BINDING_ID)
        self.cohort = _FakeCohort()
        self.calls: list[tuple[str, dict]] = []
        self.audit_details: list[dict] = []
        self.raise_idempotency = False

    def _call(self, method_name: str, **kwargs):
        self.calls.append((method_name, kwargs))

    def list_workspace_bindings(self, **kwargs):
        self._call("list_workspace_bindings", **kwargs)
        return (self.binding,)

    def list_annotators(self, *, workspace_slug, identity, **kwargs):
        self._call("list_annotators", workspace_slug=workspace_slug, identity=identity, **kwargs)
        return (self.mapping,) if workspace_slug == WORKSPACE else ()

    def get_annotator(self, *, workspace_slug, identity, mapping_id):
        self._call("get_annotator", workspace_slug=workspace_slug, identity=identity, mapping_id=mapping_id)
        if workspace_slug != WORKSPACE or mapping_id != MAPPING_ID:
            return None
        return self.mapping

    def create_annotator_mapping(self, **kwargs):
        self._call("create_annotator_mapping", **kwargs)
        self.mapping.argilla_user_id = kwargs["argilla_user_id"]
        self.mapping.personal_argilla_workspace_id = kwargs["personal_argilla_workspace_id"]
        self.mapping.username = kwargs["username"]
        self.mapping.principal_subject = kwargs["principal_identity"].subject
        self.mapping.verification_state = "verified" if kwargs["membership_verified"] else "unverified"
        return SimpleNamespace(mapping=self.mapping, response_status=201, replayed=False)

    def verify_annotator(self, **kwargs):
        self._call("verify_annotator", **kwargs)
        if self.raise_idempotency:
            raise IdempotencyConflict()
        self.mapping.verification_state = "verified"
        self.mapping.verified_at = datetime.now(timezone.utc)
        self.audit_details.append({"event": "annotator.mapping.verified"})
        return SimpleNamespace(mapping=self.mapping, response_status=200, replayed=False)

    def list_cohorts(self, *, workspace_slug, identity, **kwargs):
        self._call("list_cohorts", workspace_slug=workspace_slug, identity=identity, **kwargs)
        return (self.cohort,) if workspace_slug == WORKSPACE else ()

    def get_cohort(self, *, workspace_slug, identity, cohort_id):
        self._call("get_cohort", workspace_slug=workspace_slug, identity=identity, cohort_id=cohort_id)
        if workspace_slug != WORKSPACE or cohort_id != COHORT_ID:
            return None
        return self.cohort

    def list_cohort_revisions(self, *, workspace_slug, identity, cohort_id):
        self._call("list_cohort_revisions", workspace_slug=workspace_slug, identity=identity, cohort_id=cohort_id)
        return (self.cohort.latest_revision,)

    def create_cohort(self, **kwargs):
        self._call("create_cohort", **kwargs)
        self.cohort.name = kwargs["name"]
        self.cohort.latest_revision = _FakeRevision(
            [_FakeMember(self.mapping, item.default_capacity) for item in kwargs["members"]],
            1,
        )
        return SimpleNamespace(cohort=self.cohort, response_status=201, replayed=False)

    def create_cohort_revision(self, **kwargs):
        self._call("create_cohort_revision", **kwargs)
        number = self.cohort.latest_revision.revision_number + 1
        self.cohort.latest_revision = _FakeRevision(
            [_FakeMember(self.mapping, item.default_capacity) for item in kwargs["members"]],
            number,
        )
        return SimpleNamespace(revision=self.cohort.latest_revision, response_status=201, replayed=False)

    def update_cohort(self, **kwargs):
        self._call("update_cohort", **kwargs)
        if kwargs["name"] is not None:
            self.cohort.name = kwargs["name"]
        capacity = kwargs["default_capacity"]
        number = self.cohort.latest_revision.revision_number + 1
        self.cohort.latest_revision = _FakeRevision(
            [
                _FakeMember(self.mapping, capacity if capacity is not None else item.default_capacity)
                for item in self.cohort.latest_revision.members
            ],
            number,
        )
        return SimpleNamespace(cohort=self.cohort, response_status=201, replayed=False)


class _FakeAdapter:
    def __init__(self, *, role: str = "annotator", membership: bool = True) -> None:
        self.role = role
        self.membership = membership
        self.calls: list[dict] = []

    def ensure_annotator(self, **kwargs):
        self.calls.append(kwargs)
        return ExternalAnnotatorSnapshot(
            argilla_user_id=str(ARGILLA_USER_ID),
            argilla_username="annotator-one",
            role=self.role,
            personal_workspace_id=str(ARGILLA_WORKSPACE_ID),
            membership_present=self.membership,
            membership_role=self.role,
            is_owner=self.role == "owner",
        )


@contextmanager
def _server(monkeypatch, repository, adapter=None, authorization=None):
    names = (
        "runs_root", "tasks_root", "static_dir", "authenticator",
        "authorization_service", "allocation_repository", "annotator_repository",
        "annotator_adapter", "authorization_ready", "active_task_loader",
    )
    old = {name: getattr(panel._Handler, name) for name in names}
    panel._Handler.runs_root = Path("runs")
    panel._Handler.tasks_root = Path("tasks")
    panel._Handler.static_dir = None
    panel._Handler.authenticator = build_panel_authenticator(
        mode="basic_dev", basic_user="admin", basic_password="secret"
    )
    panel._Handler.authorization_service = authorization or _FakeAuthorizationService()
    panel._Handler.allocation_repository = None
    panel._Handler.annotator_repository = repository
    panel._Handler.annotator_adapter = adapter
    panel._Handler.authorization_ready = True
    panel._Handler.active_task_loader = None
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), panel._Handler)
    import threading
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        for name, value in old.items():
            setattr(panel._Handler, name, value)


def _request(base_url: str, path: str, *, method: str = "GET", body=None, headers=None):
    request_headers = {
        "Authorization": "Basic " + base64.b64encode(b"admin:secret").decode("ascii"),
        "Content-Type": "application/json",
    }
    request_headers.update(headers or {})
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = Request(base_url + path, data=data, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


@pytest.fixture
def control_plane(monkeypatch):
    monkeypatch.setenv("LLS_TASK_SOURCE", "control")


def test_http_get_dispatch_and_workspace_scope(control_plane):
    repository = _FakeRepository()
    with _server(control_plane, repository) as base_url:
        status, listing = _request(base_url, "/api/annotators?workspace=workspace-a")
        assert status == 200
        assert listing["annotators"][0]["annotator_id"] == str(MAPPING_ID)

        status, detail = _request(
            base_url,
            f"/api/annotators/{MAPPING_ID}?workspace=workspace-a",
        )
        assert status == 200
        assert detail["annotator"]["argilla_user_id"] == str(ARGILLA_USER_ID)

        status, cohorts = _request(base_url, "/api/cohorts?workspace=workspace-a")
        assert status == 200
        assert cohorts["cohorts"][0]["cohort_id"] == str(COHORT_ID)

        status, cohort = _request(
            base_url,
            f"/api/cohorts/{COHORT_ID}?workspace=workspace-a",
        )
        assert status == 200
        assert cohort["cohort"]["cohort_id"] == str(COHORT_ID)

        status, payload = _request(base_url, "/api/annotators")
        assert status == 422
        assert payload["code"] == "workspace_required"

        status, payload = _request(
            base_url,
            f"/api/annotators/{MAPPING_ID}?workspace=hidden",
        )
        assert status == 404
        assert payload["code"] == "resource_not_found"

        status, payload = _request(
            base_url,
            f"/api/annotators/{MAPPING_ID}?workspace=forbidden",
        )
        assert status == 403
        assert payload["code"] == "permission_denied"

        status, payload = _request(
            base_url,
            "/api/annotators/not-a-uuid?workspace=workspace-a",
        )
        assert status == 422
        assert payload["code"] == "invalid_uuid"

        status, _ = _request(
            base_url,
            "/api/annotators",
            method="POST",
            body={"workspace": WORKSPACE},
            headers={"Idempotency-Key": "removed-alias"},
        )
        assert status == 404

        status, capabilities = _request(base_url, "/api/capabilities")
        assert status == 200
        capability_paths = [item["path"] for item in capabilities["endpoints"]]
        assert capability_paths.count("/api/annotators") == 1
        assert "/api/annotators/provision" in capability_paths
        assert not any(
            item["method"] == "POST" and item["path"] == "/api/annotators"
            for item in capabilities["endpoints"]
        )


def test_http_write_dispatch_uses_idempotency_and_serializes_update(control_plane):
    repository = _FakeRepository()
    adapter = _FakeAdapter()
    with _server(control_plane, repository, adapter) as base_url:
        status, provisioned = _request(
            base_url,
            "/api/annotators/provision",
            method="POST",
            body={
                "workspace": WORKSPACE,
                "principal_id": str(PRINCIPAL_ID),
                "initial_password": "one-time-secret",
            },
            headers={"Idempotency-Key": "provision-1"},
        )
        assert status == 201
        assert "one-time-secret" not in json.dumps(provisioned, ensure_ascii=False)
        assert adapter.calls[0]["password"] == "one-time-secret"
        assert all("initial_password" not in kwargs for _, kwargs in repository.calls)
        assert "one-time-secret" not in json.dumps(repository.audit_details, ensure_ascii=False)

        status, _ = _request(
            base_url,
            "/api/annotators/bind",
            method="POST",
            body={
                "workspace": WORKSPACE,
                "principal_id": str(PRINCIPAL_ID),
                "argilla_user_id": str(ARGILLA_USER_ID),
                "personal_workspace_id": str(ARGILLA_WORKSPACE_ID),
            },
            headers={"Idempotency-Key": "bind-1"},
        )
        assert status == 201

        status, _ = _request(
            base_url,
            f"/api/annotators/{MAPPING_ID}/verify",
            method="POST",
            body={"workspace": WORKSPACE, "annotator_id": str(MAPPING_ID)},
            headers={"Idempotency-Key": "verify-1"},
        )
        assert status == 200

        status, created = _request(
            base_url,
            "/api/cohorts",
            method="POST",
            body={
                "workspace": WORKSPACE,
                "name": "reviewers",
                "default_capacity": 3,
                "member_annotator_ids": [str(MAPPING_ID)],
            },
            headers={"Idempotency-Key": "cohort-create-1"},
        )
        assert status == 201
        assert created["cohort"]["name"] == "reviewers"

        status, updated = _request(
            base_url,
            f"/api/cohorts/{COHORT_ID}",
            method="PUT",
            body={
                "workspace": WORKSPACE,
                "cohort_id": str(COHORT_ID),
                "name": "reviewers-renamed",
                "default_capacity": 4,
                "expected_revision": 1,
            },
            headers={"Idempotency-Key": "cohort-update-1"},
        )
        assert status == 201
        assert updated["cohort"]["name"] == "reviewers-renamed"
        update_call = next(
            kwargs for name, kwargs in repository.calls if name == "update_cohort"
        )
        assert update_call["name"] == "reviewers-renamed"
        assert update_call["default_capacity"] == 4
        assert update_call["expected_revision"] == 1

        status, replaced = _request(
            base_url,
            f"/api/cohorts/{COHORT_ID}/members",
            method="PUT",
            body={
                "workspace": WORKSPACE,
                "cohort_id": str(COHORT_ID),
                "member_annotator_ids": [str(MAPPING_ID)],
                "expected_revision": 2,
            },
            headers={"Idempotency-Key": "cohort-members-1"},
        )
        assert status == 201
        assert replaced["cohort"]["revision"] == 3

        status, revision = _request(
            base_url,
            f"/api/cohorts/{COHORT_ID}/revisions",
            method="POST",
            body={
                "workspace": WORKSPACE,
                "cohort_id": str(COHORT_ID),
                "member_annotator_ids": [str(MAPPING_ID)],
                "default_capacity": 5,
                "expected_revision": 3,
            },
            headers={"Idempotency-Key": "cohort-revision-1"},
        )
        assert status == 201
        assert revision["cohort"]["revision"] == 4

        write_keys = [
            kwargs["idempotency_key"]
            for name, kwargs in repository.calls
            if name in {
                "create_annotator_mapping",
                "verify_annotator",
                "create_cohort",
                "update_cohort",
                "create_cohort_revision",
            }
        ]
        assert write_keys == [
            "provision-1",
            "bind-1",
            "verify-1",
            "cohort-create-1",
            "cohort-update-1",
            "cohort-members-1",
            "cohort-revision-1",
        ]


def test_http_write_idempotency_and_external_boundaries(control_plane):
    repository = _FakeRepository()
    adapter = _FakeAdapter()
    with _server(control_plane, repository, adapter) as base_url:
        body = {
            "workspace": WORKSPACE,
            "principal_id": str(PRINCIPAL_ID),
            "initial_password": "secret-value",
        }
        status, payload = _request(base_url, "/api/annotators/provision", method="POST", body=body)
        assert status == 422
        assert payload["code"] == "missing_idempotency_key"
        assert "secret-value" not in json.dumps(payload, ensure_ascii=False)

        status, payload = _request(
            base_url,
            "/api/annotators/provision",
            method="POST",
            body=body,
            headers={"Idempotency-Key": " "},
        )
        assert status == 422
        assert payload["code"] == "missing_idempotency_key"

        repository.raise_idempotency = True
        status, payload = _request(
            base_url,
            f"/api/annotators/{MAPPING_ID}/verify",
            method="POST",
            body={"workspace": WORKSPACE},
            headers={"Idempotency-Key": "verify-conflict"},
        )
        assert status == 409
        assert payload["code"] == "idempotency_conflict"

        repository.mapping.argilla_role = "owner"
        status, payload = _request(
            base_url,
            "/api/cohorts",
            method="POST",
            body={
                "workspace": WORKSPACE,
                "name": "owner-cohort",
                "default_capacity": 1,
                "member_annotator_ids": [str(MAPPING_ID)],
            },
            headers={"Idempotency-Key": "owner-cohort-1"},
        )
        assert status == 422
        assert payload["code"] == "owner_account_forbidden"

        repository.mapping.argilla_role = "annotator"
        repository.mapping.verification_state = "unverified"
        status, payload = _request(
            base_url,
            "/api/cohorts",
            method="POST",
            body={
                "workspace": WORKSPACE,
                "name": "unverified-cohort",
                "default_capacity": 1,
                "member_annotator_ids": [str(MAPPING_ID)],
            },
            headers={"Idempotency-Key": "unverified-cohort-1"},
        )
        assert status == 409
        assert payload["code"] == "annotator_not_verified"

        status, payload = _request(
            base_url,
            "/api/annotators/bind",
            method="POST",
            body={"workspace": WORKSPACE},
            headers={"Idempotency-Key": "bind-invalid"},
        )
        assert status == 422
        assert payload["code"] == "invalid_field"


def test_annotator_routes_fail_closed_outside_control_plane(monkeypatch):
    repository = _FakeRepository()
    monkeypatch.setenv("LLS_TASK_SOURCE", "r2")
    with _server(monkeypatch, repository) as base_url:
        status, payload = _request(base_url, "/api/annotators?workspace=workspace-a")
    assert status == 503
    assert payload["code"] == "authorization_unavailable"
    assert panel._database_authorized_route("GET", "/api/annotators") is False


def test_annotator_runtime_unavailable_returns_503(control_plane):
    repository = _FakeRepository()
    authorization = _FakeAuthorizationService()
    with _server(control_plane, repository, authorization=authorization) as base_url:
        panel._Handler.authorization_ready = False
        status, payload = _request(base_url, "/api/cohorts?workspace=workspace-a")
    assert status == 503
    assert payload["code"] == "authorization_unavailable"


def test_external_annotator_runtime_unavailable_returns_503(control_plane):
    repository = _FakeRepository()
    with _server(control_plane, repository, adapter=None) as base_url:
        status, payload = _request(
            base_url,
            "/api/annotators/provision",
            method="POST",
            body={
                "workspace": WORKSPACE,
                "principal_id": str(PRINCIPAL_ID),
                "initial_password": "one-time-secret",
            },
            headers={"Idempotency-Key": "adapter-missing"},
        )
    assert status == 503
    assert payload["code"] == "external_service_unavailable"


def test_serve_panel_wires_and_closes_annotator_runtime(monkeypatch):
    observed = {}
    repository = _FakeRepository()
    adapter = _FakeAdapter()
    authorization = _FakeAuthorizationService()

    class _Loader:
        pass

    class _Httpd:
        def __init__(self, address, handler):
            self.address = address
            self.handler = handler

        def serve_forever(self):
            observed["repository"] = panel._Handler.annotator_repository
            observed["adapter"] = panel._Handler.annotator_adapter
            raise KeyboardInterrupt()

        def server_close(self):
            observed["closed"] = True

    monkeypatch.setenv("LLS_TASK_SOURCE", "control")
    monkeypatch.setattr(panel, "_build_authorization_service", lambda: authorization)
    monkeypatch.setattr(panel, "_authorization_runtime_ready", lambda service, loader: True)
    monkeypatch.setattr(panel, "_panel_server_type", lambda host: _Httpd)
    monkeypatch.setattr(panel.TrustedAllocationRepository, "from_url", lambda: None)
    monkeypatch.setattr(panel.AnnotatorControlRepository, "from_url", lambda: repository)
    monkeypatch.setattr(panel, "ArgillaAdminAdapter", lambda: adapter)

    closed = []
    repository.close = lambda: closed.append("repository")
    authorization.close = lambda: closed.append("authorization")

    panel.serve_panel(
        runs_root=Path("runs"),
        tasks_root=Path("tasks"),
        port=0,
        user="admin",
        password="secret",
        auth_mode="basic_dev",
        active_task_loader=_Loader(),
    )

    assert observed["repository"] is repository
    assert observed["adapter"] is adapter
    assert observed["closed"] is True
    assert closed == ["repository", "authorization"]
