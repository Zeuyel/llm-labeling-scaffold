from __future__ import annotations

import ast
import json
from pathlib import Path
import re
import traceback
import types
from uuid import UUID

import pytest

from llm_labeling_scaffold.integrations import argilla_admin
from llm_labeling_scaffold.integrations.argilla_admin import (
    ArgillaAdminAdapter,
    ArgillaProvisioningError,
    derive_argilla_username,
    derive_personal_workspace_name,
)


_OPERATOR_ID = "00000000-0000-0000-0000-000000000900"
_USER_ID = "00000000-0000-0000-0000-000000000101"
_OTHER_USER_ID = "00000000-0000-0000-0000-000000000102"
_WORKSPACE_ID = "00000000-0000-0000-0000-000000000201"
_OTHER_WORKSPACE_ID = "00000000-0000-0000-0000-000000000202"
_VERIFIED_AT = "2026-07-14T12:00:00Z"


class _FakeUser:
    def __init__(
        self,
        username=None,
        first_name=None,
        last_name=None,
        role=None,
        password=None,
        id=None,
        client=None,
    ):
        self.username = username
        self.first_name = first_name
        self.last_name = last_name
        self.role = role
        self.password = password
        self.id = id
        self._client = client

    def __repr__(self):
        return (
            f"_FakeUser(username={self.username!r}, first_name={self.first_name!r}, "
            f"last_name={self.last_name!r}, password={self.password!r})"
        )

    def create(self):
        return self._client.create_user(self)


class _FakeWorkspaceUsers:
    def __init__(self, workspace):
        self._workspace = workspace

    def __iter__(self):
        client = self._workspace._client
        client.routes.append(f"GET /api/v1/workspaces/{self._workspace.id}/users")
        return iter(list(client.memberships.get(str(self._workspace.id), [])))


class _FakeWorkspace:
    def __init__(self, name=None, id=None, client=None):
        self.name = name
        self.id = id
        self._client = client

    def create(self):
        return self._client.create_workspace(self)

    @property
    def users(self):
        return _FakeWorkspaceUsers(self)

    def add_user(self, user):
        return self._client.add_membership(self, user)


class _FakeUsers:
    def __init__(self, client):
        self._client = client

    def __call__(self, username=None, id=None):
        if id is not None:
            user_id = str(UUID(str(id)))
            self._client.routes.append(f"GET /api/v1/users/{user_id}")
            return next((item for item in self._client.user_items if str(item.id) == user_id), None)
        self._client.routes.append("GET /api/v1/users")
        return next((item for item in self._client.user_items if item.username == username), None)

    def list(self):
        self._client.routes.append("GET /api/v1/users")
        return list(self._client.user_items)


class _FakeWorkspaces:
    def __init__(self, client):
        self._client = client

    def __call__(self, name=None, id=None):
        if id is not None:
            workspace_id = str(UUID(str(id)))
            self._client.routes.append(f"GET /api/v1/workspaces/{workspace_id}")
            return next((item for item in self._client.workspace_items if str(item.id) == workspace_id), None)
        self._client.routes.append("GET /api/v1/me/workspaces")
        return next((item for item in self._client.workspace_items if item.name == name), None)

    def list(self):
        self._client.routes.append("GET /api/v1/me/workspaces")
        return list(self._client.workspace_items)


class _FakeClient:
    def __init__(self, *, operator_role="owner", api_key="owner-api-key"):
        self.api_url = "https://argilla.example"
        self.api_key = api_key
        self.routes: list[str] = []
        self.user_items: list[_FakeUser] = []
        self.workspace_items: list[_FakeWorkspace] = []
        self.memberships: dict[str, list[_FakeUser]] = {}
        self.attempted_users: list[_FakeUser] = []
        self.user_create_mode = "success"
        self.workspace_create_mode = "success"
        self.membership_create_mode = "success"
        self.failure_message = "remote failure"
        self._next_user_id = _USER_ID
        self._next_workspace_id = _WORKSPACE_ID
        self._operator = _FakeUser(
            username="owner",
            first_name="Owner",
            last_name=None,
            role=types.SimpleNamespace(value=operator_role),
            id=_OPERATOR_ID,
            client=self,
        )
        self.users = _FakeUsers(self)
        self.workspaces = _FakeWorkspaces(self)

    @property
    def me(self):
        self.routes.append("GET /api/v1/me")
        return self._operator

    def create_user(self, user):
        self.routes.append("POST /api/v1/users")
        self.attempted_users.append(user)
        if user.id is None:
            user.id = self._next_user_id
        if self.user_create_mode == "raise_before":
            raise RuntimeError(f"{self.failure_message}: {user!r}")
        self.user_items.append(user)
        if self.user_create_mode == "raise_after":
            raise TimeoutError(f"{self.failure_message}: {user!r}")
        return user

    def create_workspace(self, workspace):
        self.routes.append("POST /api/v1/workspaces")
        if workspace.id is None:
            workspace.id = self._next_workspace_id
        if self.workspace_create_mode == "raise_before":
            raise RuntimeError(self.failure_message)
        self.workspace_items.append(workspace)
        self.memberships.setdefault(str(workspace.id), [])
        if self.workspace_create_mode == "raise_after":
            raise TimeoutError(self.failure_message)
        return workspace

    def add_membership(self, workspace, user):
        self.routes.append(f"POST /api/v1/workspaces/{workspace.id}/users")
        members = self.memberships.setdefault(str(workspace.id), [])
        if self.membership_create_mode == "raise_before":
            raise RuntimeError(self.failure_message)
        if not any(str(item.id) == str(user.id) for item in members):
            members.append(user)
        if self.membership_create_mode == "raise_after":
            raise TimeoutError(self.failure_message)
        return user


class _FakeSDK:
    __version__ = "2.8.0"
    User = _FakeUser
    Workspace = _FakeWorkspace

    def __init__(self, client):
        self.client = client
        self.client_calls: list[dict[str, str]] = []

    def Argilla(self, *, api_url, api_key):
        self.client_calls.append({"api_url": api_url, "api_key": api_key})
        self.client.api_url = api_url
        self.client.api_key = api_key
        return self.client


def _user(client, user_id, username, *, role="annotator", first_name="Ada", last_name="Lovelace"):
    return _FakeUser(
        id=user_id,
        username=username,
        first_name=first_name,
        last_name=last_name,
        role=types.SimpleNamespace(value=role),
        password=None,
        client=client,
    )


def _workspace(client, workspace_id, name):
    return _FakeWorkspace(id=workspace_id, name=name, client=client)


def _install_sdk(monkeypatch, client):
    sdk = _FakeSDK(client)
    monkeypatch.setenv("ARGILLA_API_KEY", client.api_key)
    monkeypatch.setattr(argilla_admin, "_load_argilla", lambda: sdk)

    def runtime_versions(rg, api_url):
        client.routes.append("GET /api/v1/version")
        assert rg is sdk
        assert api_url == client.api_url
        return {"sdk": "2.8.0", "server": "2.8.0"}

    monkeypatch.setattr(argilla_admin, "_runtime_versions", runtime_versions)
    monkeypatch.setattr(argilla_admin, "_verified_at", lambda: _VERIFIED_AT)
    return sdk


def _identity():
    issuer = "https://team.cloudflareaccess.com"
    subject = "immutable-user-subject"
    return issuer, subject, derive_argilla_username(issuer, subject), derive_personal_workspace_name(issuer, subject)


def test_principal_helpers_are_stable_and_workspace_name_is_url_safe():
    issuer, subject, username, workspace_name = _identity()

    assert derive_argilla_username(issuer, subject) == username
    assert derive_personal_workspace_name(issuer, subject) == workspace_name
    assert derive_argilla_username(issuer, f"{subject}-changed") != username
    assert re.fullmatch(r"[a-z0-9_]+", username)
    assert re.fullmatch(r"[A-Za-z0-9_-]+", workspace_name)
    assert "@" not in username
    assert "@" not in workspace_name


def test_adapter_uses_env_owner_key_and_validates_versions(monkeypatch):
    secret = "owner-secret-from-env"
    client = _FakeClient(operator_role="owner", api_key=secret)
    sdk = _install_sdk(monkeypatch, client)
    monkeypatch.setenv("ARGILLA_API_KEY", secret)

    adapter = ArgillaAdminAdapter(api_url=client.api_url)

    assert sdk.client_calls == [{"api_url": client.api_url, "api_key": secret}]
    assert client.routes == ["GET /api/v1/version", "GET /api/v1/me"]
    assert secret not in repr(adapter)


def test_adapter_constructor_does_not_accept_api_key():
    with pytest.raises(TypeError, match="api_key"):
        ArgillaAdminAdapter(api_key="must-not-be-accepted")


def test_version_validation_failure_prevents_client_initialization(monkeypatch):
    client = _FakeClient()
    sdk = _install_sdk(monkeypatch, client)

    def reject_versions(rg, api_url):
        raise RuntimeError("Argilla SDK 必须是 2.8.x")

    monkeypatch.setattr(argilla_admin, "_runtime_versions", reject_versions)

    with pytest.raises(RuntimeError, match="2.8.x"):
        ArgillaAdminAdapter(api_url=client.api_url)

    assert sdk.client_calls == []
    assert client.routes == []


@pytest.mark.parametrize("operator_role", ["admin", "annotator"])
def test_adapter_rejects_non_owner_operator(monkeypatch, operator_role):
    client = _FakeClient(operator_role=operator_role)
    _install_sdk(monkeypatch, client)

    with pytest.raises(ArgillaProvisioningError) as exc_info:
        ArgillaAdminAdapter(api_url=client.api_url)

    assert exc_info.value.code == "insufficient_operator_role"


def test_create_routes_and_whitelist_dto(monkeypatch):
    secret = "one-time-password"
    owner_key = "owner-key-must-not-leak"
    issuer, subject, username, workspace_name = _identity()
    client = _FakeClient(api_key=owner_key)
    _install_sdk(monkeypatch, client)
    adapter = ArgillaAdminAdapter(api_url=client.api_url)

    result = adapter.ensure_annotator(
        principal_issuer=issuer,
        principal_subject=subject,
        password=secret,
        first_name="Ada",
        last_name="Lovelace",
    )

    assert client.routes == [
        "GET /api/v1/version",
        "GET /api/v1/me",
        "GET /api/v1/users",
        "POST /api/v1/users",
        "GET /api/v1/me/workspaces",
        "POST /api/v1/workspaces",
        f"GET /api/v1/workspaces/{_WORKSPACE_ID}/users",
        f"POST /api/v1/workspaces/{_WORKSPACE_ID}/users",
        f"GET /api/v1/workspaces/{_WORKSPACE_ID}/users",
    ]
    assert result.action.to_dict() == {"user": "created", "workspace": "created", "membership": "created"}
    assert result.verification_state == "verified"
    assert result.verified_at == _VERIFIED_AT
    assert result.user.username == username
    assert result.workspace.name == workspace_name
    assert client.attempted_users[0].password is None

    payload = result.to_dict()
    assert list(payload) == ["user", "workspace", "verification_state", "verified_at", "action"]
    assert list(payload["user"]) == ["uuid", "username", "role"]
    assert list(payload["workspace"]) == ["uuid", "name"]
    assert list(payload["action"]) == ["user", "workspace", "membership"]
    serialized = json.dumps(payload, ensure_ascii=False)
    for sensitive in (
        secret,
        owner_key,
        "api_key",
        "password",
        "first_name",
        "last_name",
        "status",
        "active",
    ):
        assert sensitive not in serialized


def test_expected_ids_are_authoritative_and_existing_membership_performs_zero_posts(monkeypatch):
    issuer, subject, username, workspace_name = _identity()
    client = _FakeClient()
    user = _user(client, _USER_ID, username)
    extra = _user(client, _OTHER_USER_ID, "unrelated-annotator")
    workspace = _workspace(client, _WORKSPACE_ID, workspace_name)
    client.user_items.extend([extra, user])
    client.workspace_items.append(workspace)
    client.memberships[_WORKSPACE_ID] = [extra, user]
    _install_sdk(monkeypatch, client)
    adapter = ArgillaAdminAdapter(api_url=client.api_url)

    result = adapter.ensure_annotator(
        principal_issuer=issuer,
        principal_subject=subject,
        password="unused-secret",
        expected_user_uuid=_USER_ID,
        expected_workspace_uuid=_WORKSPACE_ID,
    )

    assert result.action.to_dict() == {"user": "reused", "workspace": "reused", "membership": "reused"}
    assert client.routes == [
        "GET /api/v1/version",
        "GET /api/v1/me",
        f"GET /api/v1/users/{_USER_ID}",
        f"GET /api/v1/workspaces/{_WORKSPACE_ID}",
        f"GET /api/v1/workspaces/{_WORKSPACE_ID}/users",
    ]
    assert client.memberships[_WORKSPACE_ID] == [extra, user]
    assert not any(route.startswith("POST ") or route.startswith("DELETE ") for route in client.routes)


def test_name_based_retry_reuses_all_resources_without_posts(monkeypatch):
    issuer, subject, username, workspace_name = _identity()
    client = _FakeClient()
    user = _user(client, _USER_ID, username)
    workspace = _workspace(client, _WORKSPACE_ID, workspace_name)
    client.user_items.append(user)
    client.workspace_items.append(workspace)
    client.memberships[_WORKSPACE_ID] = [user]
    _install_sdk(monkeypatch, client)
    adapter = ArgillaAdminAdapter(api_url=client.api_url)

    result = adapter.ensure_annotator(principal_issuer=issuer, principal_subject=subject)

    assert result.action.to_dict() == {"user": "reused", "workspace": "reused", "membership": "reused"}
    assert not any(route.startswith("POST ") for route in client.routes)


def test_membership_verification_ignores_mutable_name_snapshots(monkeypatch):
    issuer, subject, username, workspace_name = _identity()
    client = _FakeClient()
    user = _user(client, _USER_ID, username, first_name="Current", last_name="Snapshot")
    member = _user(client, _USER_ID, username, first_name="Older", last_name="Snapshot")
    workspace = _workspace(client, _WORKSPACE_ID, workspace_name)
    client.user_items.append(user)
    client.workspace_items.append(workspace)
    client.memberships[_WORKSPACE_ID] = [member]
    _install_sdk(monkeypatch, client)
    adapter = ArgillaAdminAdapter(api_url=client.api_url)

    result = adapter.ensure_annotator(
        principal_issuer=issuer,
        principal_subject=subject,
        expected_user_uuid=_USER_ID,
        expected_workspace_uuid=_WORKSPACE_ID,
    )

    assert result.action.membership == "reused"
    assert not hasattr(result.user, "first_name")
    assert "Current" not in repr(result)
    assert "Older" not in repr(result)
    assert client.routes.count(f"POST /api/v1/workspaces/{_WORKSPACE_ID}/users") == 0


def test_missing_expected_user_uuid_never_falls_back_to_same_username(monkeypatch):
    issuer, subject, username, _ = _identity()
    client = _FakeClient()
    client.user_items.append(_user(client, _USER_ID, username))
    _install_sdk(monkeypatch, client)
    adapter = ArgillaAdminAdapter(api_url=client.api_url)

    with pytest.raises(ArgillaProvisioningError) as exc_info:
        adapter.ensure_annotator(
            principal_issuer=issuer,
            principal_subject=subject,
            password="unused-secret",
            expected_user_uuid=_OTHER_USER_ID,
        )

    assert exc_info.value.code == "expected_user_missing"
    assert client.routes[-1] == f"GET /api/v1/users/{_OTHER_USER_ID}"
    assert "GET /api/v1/users" not in client.routes


def test_missing_expected_workspace_uuid_never_falls_back_to_same_name(monkeypatch):
    issuer, subject, username, workspace_name = _identity()
    client = _FakeClient()
    client.user_items.append(_user(client, _USER_ID, username))
    client.workspace_items.append(_workspace(client, _WORKSPACE_ID, workspace_name))
    _install_sdk(monkeypatch, client)
    adapter = ArgillaAdminAdapter(api_url=client.api_url)

    with pytest.raises(ArgillaProvisioningError) as exc_info:
        adapter.ensure_annotator(
            principal_issuer=issuer,
            principal_subject=subject,
            expected_user_uuid=_USER_ID,
            expected_workspace_uuid=_OTHER_WORKSPACE_ID,
        )

    assert exc_info.value.code == "expected_workspace_missing"
    assert client.routes[-1] == f"GET /api/v1/workspaces/{_OTHER_WORKSPACE_ID}"
    assert "GET /api/v1/me/workspaces" not in client.routes


def test_expected_workspace_name_drift_fails_closed(monkeypatch):
    issuer, subject, username, _ = _identity()
    client = _FakeClient()
    client.user_items.append(_user(client, _USER_ID, username))
    client.workspace_items.append(_workspace(client, _WORKSPACE_ID, "different-workspace"))
    _install_sdk(monkeypatch, client)
    adapter = ArgillaAdminAdapter(api_url=client.api_url)

    with pytest.raises(ArgillaProvisioningError) as exc_info:
        adapter.ensure_annotator(
            principal_issuer=issuer,
            principal_subject=subject,
            expected_user_uuid=_USER_ID,
            expected_workspace_uuid=_WORKSPACE_ID,
        )

    assert exc_info.value.code == "workspace_name_drift"


@pytest.mark.parametrize("role", ["admin", "owner"])
def test_non_annotator_role_fails_closed(monkeypatch, role):
    issuer, subject, username, _ = _identity()
    client = _FakeClient()
    client.user_items.append(_user(client, _USER_ID, username, role=role))
    _install_sdk(monkeypatch, client)
    adapter = ArgillaAdminAdapter(api_url=client.api_url)

    with pytest.raises(ArgillaProvisioningError) as exc_info:
        adapter.ensure_annotator(principal_issuer=issuer, principal_subject=subject)

    assert exc_info.value.code == "user_role_drift"


def test_owner_identity_cannot_be_returned_as_annotator(monkeypatch):
    issuer, subject, username, _ = _identity()
    client = _FakeClient()
    client.user_items.append(_user(client, _OPERATOR_ID, username))
    _install_sdk(monkeypatch, client)
    adapter = ArgillaAdminAdapter(api_url=client.api_url)

    with pytest.raises(ArgillaProvisioningError) as exc_info:
        adapter.ensure_annotator(principal_issuer=issuer, principal_subject=subject)

    assert exc_info.value.code == "owner_identity_conflict"


def test_expected_user_username_drift_fails_closed(monkeypatch):
    issuer, subject, _, _ = _identity()
    client = _FakeClient()
    client.user_items.append(_user(client, _USER_ID, "different-username"))
    _install_sdk(monkeypatch, client)
    adapter = ArgillaAdminAdapter(api_url=client.api_url)

    with pytest.raises(ArgillaProvisioningError) as exc_info:
        adapter.ensure_annotator(
            principal_issuer=issuer,
            principal_subject=subject,
            expected_user_uuid=_USER_ID,
        )

    assert exc_info.value.code == "username_drift"


def test_workspace_visibility_conflict_fails_closed(monkeypatch):
    issuer, subject, username, _ = _identity()
    client = _FakeClient()
    client.user_items.append(_user(client, _USER_ID, username))
    client.workspace_create_mode = "raise_before"
    _install_sdk(monkeypatch, client)
    adapter = ArgillaAdminAdapter(api_url=client.api_url)

    with pytest.raises(ArgillaProvisioningError) as exc_info:
        adapter.ensure_annotator(principal_issuer=issuer, principal_subject=subject)

    assert exc_info.value.code == "workspace_visibility_conflict"
    assert client.routes.count("GET /api/v1/me/workspaces") == 2
    assert client.routes.count("POST /api/v1/workspaces") == 1


def test_create_timeout_and_membership_conflict_recover_by_reread(monkeypatch):
    issuer, subject, _, _ = _identity()
    client = _FakeClient()
    client.user_create_mode = "raise_after"
    client.workspace_create_mode = "raise_after"
    client.membership_create_mode = "raise_after"
    _install_sdk(monkeypatch, client)
    adapter = ArgillaAdminAdapter(api_url=client.api_url)

    result = adapter.ensure_annotator(
        principal_issuer=issuer,
        principal_subject=subject,
        password="one-time-password",
    )

    assert result.action.to_dict() == {"user": "recovered", "workspace": "recovered", "membership": "recovered"}
    assert client.attempted_users[0].password is None
    assert client.routes.count("POST /api/v1/users") == 1
    assert client.routes.count("POST /api/v1/workspaces") == 1
    assert client.routes.count(f"POST /api/v1/workspaces/{_WORKSPACE_ID}/users") == 1


def test_membership_same_username_different_uuid_fails_closed(monkeypatch):
    issuer, subject, username, workspace_name = _identity()
    client = _FakeClient()
    user = _user(client, _USER_ID, username)
    imposter = _user(client, _OTHER_USER_ID, username)
    workspace = _workspace(client, _WORKSPACE_ID, workspace_name)
    client.user_items.append(user)
    client.workspace_items.append(workspace)
    client.memberships[_WORKSPACE_ID] = [user, imposter]
    _install_sdk(monkeypatch, client)
    adapter = ArgillaAdminAdapter(api_url=client.api_url)

    with pytest.raises(ArgillaProvisioningError) as exc_info:
        adapter.ensure_annotator(principal_issuer=issuer, principal_subject=subject)

    assert exc_info.value.code == "membership_identity_conflict"
    assert client.routes.count(f"POST /api/v1/workspaces/{_WORKSPACE_ID}/users") == 0


def test_identity_conflict_sorting_is_stable_across_sdk_list_order(monkeypatch):
    issuer, subject, username, _ = _identity()

    def conflict_message(items):
        client = _FakeClient()
        client.user_items.extend(items(client))
        _install_sdk(monkeypatch, client)
        adapter = ArgillaAdminAdapter(api_url=client.api_url)
        with pytest.raises(ArgillaProvisioningError) as exc_info:
            adapter.ensure_annotator(principal_issuer=issuer, principal_subject=subject)
        return str(exc_info.value)

    ascending = conflict_message(
        lambda client: [_user(client, _USER_ID, username), _user(client, _OTHER_USER_ID, username)]
    )
    descending = conflict_message(
        lambda client: [_user(client, _OTHER_USER_ID, username), _user(client, _USER_ID, username)]
    )

    assert ascending == descending
    assert ascending.index(_USER_ID) < ascending.index(_OTHER_USER_ID)


def test_create_only_names_matching_password_do_not_leak_through_dto_repr_or_logs(monkeypatch, caplog):
    secret = "shared-create-secret"
    issuer, subject, _, _ = _identity()
    client = _FakeClient()
    _install_sdk(monkeypatch, client)
    adapter = ArgillaAdminAdapter(api_url=client.api_url)

    result = adapter.ensure_annotator(
        principal_issuer=issuer,
        principal_subject=subject,
        password=secret,
        first_name=secret,
        last_name=secret,
    )

    observed = "\n".join(
        [
            json.dumps(result.to_dict(), ensure_ascii=False),
            repr(result),
            repr(result.user),
            caplog.text,
        ]
    )
    assert secret not in observed
    assert client.attempted_users[0].first_name == secret
    assert client.attempted_users[0].last_name == secret
    assert client.attempted_users[0].password is None


def test_password_and_owner_key_do_not_leak_through_errors_repr_logs_or_dto(monkeypatch, caplog):
    password = "password-must-not-leak"
    owner_key = "owner-key-must-not-leak"
    issuer, subject, _, _ = _identity()
    client = _FakeClient(api_key=owner_key)
    client.user_create_mode = "raise_before"
    client.failure_message = f"remote echoed {password} and {owner_key}"
    _install_sdk(monkeypatch, client)
    adapter = ArgillaAdminAdapter(api_url=client.api_url)

    with pytest.raises(ArgillaProvisioningError) as exc_info:
        adapter.ensure_annotator(
            principal_issuer=issuer,
            principal_subject=subject,
            password=password,
            first_name=password,
            last_name=password,
        )

    rendered = "".join(traceback.format_exception(exc_info.value))
    observed = "\n".join(
        [
            str(exc_info.value),
            repr(exc_info.value),
            rendered,
            repr(adapter),
            caplog.text,
        ]
    )
    assert password not in observed
    assert owner_key not in observed
    assert exc_info.value.__context__ is None
    assert client.attempted_users[0].password is None


def test_module_does_not_import_argilla_private_modules():
    path = Path(argilla_admin.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)

    assert not any(name.startswith("argilla._") for name in imported)
