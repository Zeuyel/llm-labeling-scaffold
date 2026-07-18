from __future__ import annotations

import json

import pytest

from llm_labeling_scaffold import panel_annotators as api

try:
    from llm_labeling_scaffold.db.rbac import Permission
except ImportError:
    Permission = None


def _annotator_record(
    *,
    workspace: str = "workspace-a",
    annotator_id: str = "annotator-1",
    scaffold_user_id: str = "principal-1",
    argilla_user_id: str = "argilla-user-1",
    argilla_username: str = "annotator-one",
    role: str = "annotator",
    status: str | None = "verified",
    owner: bool = False,
) -> dict:
    verification = (
        {}
        if status is None
        else {"status": status, "last_verified_at": "2026-07-18T00:00:00+00:00"}
    )
    return {
        "workspace": workspace,
        "annotator_id": annotator_id,
        "scaffold_user_id": scaffold_user_id,
        "argilla_user_id": argilla_user_id,
        "argilla_username": argilla_username,
        "argilla_role": role,
        "personal_workspace_id": "argilla-workspace-1",
        "membership": {
            "workspace_id": "argilla-workspace-1",
            "present": True,
            "role": "annotator",
        },
        "verification": verification,
        "is_owner": owner,
        "active": True,
    }


def _external_snapshot(
    *,
    user_id: str = "argilla-user-1",
    username: str = "annotator-one",
    role: str = "annotator",
    membership_present: bool = True,
    workspace_id: str | None = "argilla-workspace-1",
    owner: bool = False,
) -> dict:
    return {
        "argilla_user_id": user_id,
        "argilla_username": username,
        "role": role,
        "personal_workspace_id": workspace_id,
        "membership_present": membership_present,
        "membership_role": "annotator",
        "is_owner": owner,
    }


class _Repository:
    def __init__(self, annotators: list[dict] | None = None):
        self.annotators = {item["annotator_id"]: item for item in annotators or []}
        self.cohorts: dict[str, dict] = {}
        self.get_annotator_calls: list[dict] = []
        self.create_annotator_calls: list[dict] = []
        self.create_cohort_calls: list[dict] = []
        self.verification_calls: list[dict] = []

    def list_annotators(self, *, workspace: str):
        return [
            item
            for item in self.annotators.values()
            if item.get("workspace") == workspace
        ]

    def get_annotator(self, *, workspace: str, annotator_id: str):
        self.get_annotator_calls.append(
            {"workspace": workspace, "annotator_id": annotator_id}
        )
        item = self.annotators.get(annotator_id)
        if item is None or item.get("workspace") != workspace:
            return None
        return item

    def create_annotator(self, **kwargs):
        self.create_annotator_calls.append(kwargs)
        external = kwargs["external"]
        record = _annotator_record(
            workspace=kwargs["workspace"],
            annotator_id="annotator-created",
            scaffold_user_id=kwargs["scaffold_user_id"],
            argilla_user_id=external.argilla_user_id,
            argilla_username=external.argilla_username,
        )
        record["verification"] = {}
        self.annotators[record["annotator_id"]] = record
        return record

    def update_annotator_verification(self, **kwargs):
        self.verification_calls.append(kwargs)
        record = self.annotators[kwargs["annotator_id"]]
        record["verification"] = kwargs["verification"].to_dict()
        return record

    def list_cohorts(self, *, workspace: str):
        return [
            item for item in self.cohorts.values() if item.get("workspace") == workspace
        ]

    def get_cohort(self, *, workspace: str, cohort_id: str):
        item = self.cohorts.get(cohort_id)
        if item is None or item.get("workspace") != workspace:
            return None
        return item

    def create_cohort(self, **kwargs):
        self.create_cohort_calls.append(kwargs)
        record = {
            "workspace": kwargs["workspace"],
            "cohort_id": "cohort-created",
            "name": kwargs["name"],
            "default_capacity": kwargs["default_capacity"],
            "member_annotator_ids": list(kwargs["member_annotator_ids"]),
            "revision": 1,
        }
        self.cohorts[record["cohort_id"]] = record
        return record

    def update_cohort(self, **kwargs):
        record = self.cohorts[kwargs["cohort_id"]]
        if kwargs["name"] is not None:
            record["name"] = kwargs["name"]
        if kwargs["default_capacity"] is not None:
            record["default_capacity"] = kwargs["default_capacity"]
        record["revision"] += 1
        return record

    def replace_cohort_members(self, **kwargs):
        record = self.cohorts[kwargs["cohort_id"]]
        record["member_annotator_ids"] = list(kwargs["member_annotator_ids"])
        record["revision"] += 1
        return record


class _BatchRepository(_Repository):
    def __init__(self, annotators: list[dict] | None = None):
        super().__init__(annotators)
        self.batch_calls: list[dict] = []

    def get_annotators(self, *, workspace: str, annotator_ids):
        self.batch_calls.append(
            {"workspace": workspace, "annotator_ids": tuple(annotator_ids)}
        )
        return [
            item
            for annotator_id, item in self.annotators.items()
            if annotator_id in annotator_ids and item.get("workspace") == workspace
        ]


class _Adapter:
    def __init__(self, snapshot: dict | None = None):
        self.snapshot = snapshot or _external_snapshot()
        self.provision_calls: list[dict] = []
        self.bind_calls: list[dict] = []
        self.verify_calls: list[dict] = []

    def provision_annotator(self, **kwargs):
        self.provision_calls.append(kwargs)
        return self.snapshot

    def bind_annotator(self, **kwargs):
        self.bind_calls.append(kwargs)
        return self.snapshot

    def verify_annotator(self, **kwargs):
        self.verify_calls.append(kwargs)
        return self.snapshot


class _Authorizer:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.calls: list[dict] = []

    def require_workspace(self, *, workspace: str, actor, permission: str):
        self.calls.append(
            {"workspace": workspace, "actor": actor, "permission": permission}
        )
        if self.error is not None:
            raise self.error


def test_default_permission_uses_rbac_value_and_workspace_scope():
    repository = _Repository()
    authorizer = _Authorizer()
    service = api.PanelAnnotatorService(repository, authorizer=authorizer)

    expected_permission = (
        Permission.WORKSPACE_MANAGE.value
        if Permission is not None
        else "workspace:manage"
    )
    assert api.WORKSPACE_MANAGE_PERMISSION == expected_permission == "workspace:manage"
    service.list_annotators("workspace-a", actor="principal-1")

    assert authorizer.calls == [
        {
            "workspace": "workspace-a",
            "actor": "principal-1",
            "permission": "workspace:manage",
        }
    ]


def test_create_request_does_not_accept_external_username_and_bind_does():
    request = api.parse_create_annotator_request(
        {
            "workspace": "workspace-a",
            "scaffold_user_id": "principal-1",
            "initial_password": "one-time-secret",
        }
    )
    assert request.scaffold_user_id == "principal-1"
    assert "initial_password" not in request.safe_dict()

    with pytest.raises(api.AnnotatorDTOError) as exc_info:
        api.parse_create_annotator_request(
            {
                "workspace": "workspace-a",
                "scaffold_user_id": "principal-1",
                "argilla_username": "forged-name",
                "initial_password": "one-time-secret",
            }
        )
    assert exc_info.value.code == "unknown_field"

    bound = api.parse_bind_annotator_request(
        {
            "workspace": "workspace-a",
            "scaffold_user_id": "principal-1",
            "argilla_user_id": "argilla-user-1",
            "argilla_username": "existing-name",
        }
    )
    assert bound.argilla_user_id == "argilla-user-1"


def test_sensitive_fields_are_rejected_without_secret_values_in_errors():
    secret = "initial-password-value"
    with pytest.raises(api.SensitiveInputError) as exc_info:
        api.parse_bind_annotator_request(
            {
                "workspace": "workspace-a",
                "scaffold_user_id": "principal-1",
                "argilla_user_id": "argilla-user-1",
                "credentials": {"password": secret},
            }
        )
    assert secret not in str(exc_info.value)
    assert secret not in repr(exc_info.value)

    request = api.parse_create_annotator_request(
        {
            "workspace": "workspace-a",
            "scaffold_user_id": "principal-1",
            "initial_password": secret,
        }
    )
    assert secret in request.initial_password
    assert secret not in repr(request)
    assert secret not in json.dumps(request.safe_dict(), ensure_ascii=False)


def test_create_annotator_derives_external_username_and_never_returns_password():
    secret = "one-time-password"
    repository = _Repository()
    adapter = _Adapter(_external_snapshot(username="derived-from-principal"))
    service = api.PanelAnnotatorService(repository, adapter)

    response = service.create_annotator(
        {
            "workspace": "workspace-a",
            "scaffold_user_id": "principal-1",
            "initial_password": secret,
        }
    )
    payload = response.to_dict()

    assert adapter.provision_calls == [
        {
            "workspace": "workspace-a",
            "scaffold_user_id": "principal-1",
            "personal_workspace_name": None,
            "initial_password": secret,
        }
    ]
    assert "argilla_username" not in adapter.provision_calls[0]
    assert "initial_password" not in repository.create_annotator_calls[0]
    assert secret not in json.dumps(payload, ensure_ascii=False)
    assert "active" not in payload


def test_adapter_exception_does_not_expose_initial_password():
    secret = "one-time-password"

    class _FailingAdapter(_Adapter):
        def provision_annotator(self, **kwargs):
            raise api.PanelAnnotatorError(
                "adapter_failed",
                f"adapter failed with {kwargs['initial_password']}",
                503,
            )

    service = api.PanelAnnotatorService(_Repository(), _FailingAdapter())
    with pytest.raises(api.PanelAnnotatorError) as exc_info:
        service.create_annotator(
            {
                "workspace": "workspace-a",
                "scaffold_user_id": "principal-1",
                "initial_password": secret,
            }
        )
    assert exc_info.value.code == "external_service_unavailable"
    assert secret not in str(exc_info.value)
    assert secret not in repr(exc_info.value)


def test_response_is_whitelisted_and_missing_verification_defaults_unverified():
    secret = "response-secret"
    record = _annotator_record(status=None)
    record["password"] = secret
    record["raw_external_dto"] = {"password": secret, "active": True}

    payload = api.serialize_annotator(record, workspace="workspace-a")

    assert payload["verification"]["status"] == "unverified"
    assert payload["verification"]["label"] == "未验证"
    assert payload["membership"]["present"] is True
    assert "password" not in json.dumps(payload, ensure_ascii=False)
    assert "raw_external_dto" not in payload
    assert "active" not in payload


@pytest.mark.parametrize("value", [2, -1, 0.0, 1.0, "true", "0"])
def test_bool_value_rejects_non_boolean_and_non_binary_values(value):
    with pytest.raises(api.CorruptRecordError):
        api._bool_value(value)


def test_bool_value_accepts_bool_binary_and_missing_values():
    assert api._bool_value(True) is True
    assert api._bool_value(False) is False
    assert api._bool_value(1) is True
    assert api._bool_value(0) is False
    assert api._bool_value(None) is False


def test_corrupt_repository_records_map_to_internal_error_without_secret_text():
    secret = "repository-password"
    record = _annotator_record()
    record["membership"]["present"] = 2
    record["password"] = secret

    with pytest.raises(api.PanelAnnotatorError) as exc_info:
        api.serialize_annotator(record, workspace="workspace-a")

    assert exc_info.value.status == 500
    assert exc_info.value.code == "service_unavailable"
    assert secret not in str(exc_info.value)
    assert secret not in json.dumps(exc_info.value.to_payload(), ensure_ascii=False)

    with pytest.raises(api.PanelAnnotatorError) as verification_error:
        api.serialize_verification({"status": "not-a-status", "token": secret})
    assert verification_error.value.status == 500
    assert verification_error.value.code == "service_unavailable"
    assert secret not in str(verification_error.value)


@pytest.mark.parametrize(
    ("snapshot", "status"),
    [
        (
            _external_snapshot(user_id="other-user"),
            api.VerificationStatus.IDENTITY_DRIFT,
        ),
        (_external_snapshot(role="viewer"), api.VerificationStatus.ROLE_ERROR),
        (
            _external_snapshot(membership_present=False),
            api.VerificationStatus.MEMBERSHIP_MISSING,
        ),
        (_external_snapshot(owner=True), api.VerificationStatus.ROLE_ERROR),
        (_external_snapshot(), api.VerificationStatus.VERIFIED),
    ],
)
def test_verify_serializes_fail_closed_statuses(
    snapshot: dict, status: api.VerificationStatus
):
    record = _annotator_record()
    repository = _Repository([record])
    service = api.PanelAnnotatorService(repository, _Adapter(snapshot))

    response = service.verify_annotator(
        {"workspace": "workspace-a", "annotator_id": "annotator-1"}
    )

    assert response.verification.status is status
    assert repository.verification_calls[-1]["verification"].status is status


@pytest.mark.parametrize(
    "status", ["unverified", "rejected", "identity_drift", "role_error"]
)
def test_cohort_rejects_members_without_verified_status(status: str):
    repository = _Repository([_annotator_record(status=status)])
    service = api.PanelAnnotatorService(repository)

    with pytest.raises(api.PanelAnnotatorError) as exc_info:
        service.create_cohort(
            {
                "workspace": "workspace-a",
                "name": "reviewers",
                "default_capacity": 3,
                "member_annotator_ids": ["annotator-1"],
            }
        )

    assert exc_info.value.code == "annotator_not_verified"
    assert repository.create_cohort_calls == []


@pytest.mark.parametrize(
    ("role", "owner", "code"),
    [
        ("owner", False, "owner_account_forbidden"),
        ("annotator", True, "owner_account_forbidden"),
        ("viewer", False, "annotator_role_required"),
    ],
)
def test_cohort_rejects_owner_and_non_annotator_members(
    role: str, owner: bool, code: str
):
    repository = _Repository(
        [_annotator_record(role=role, owner=owner, status="verified")]
    )
    service = api.PanelAnnotatorService(repository)

    with pytest.raises(api.PanelAnnotatorError) as exc_info:
        service.create_cohort(
            {
                "workspace": "workspace-a",
                "name": "reviewers",
                "default_capacity": 3,
                "member_annotator_ids": ["annotator-1"],
            }
        )

    assert exc_info.value.code == code
    assert repository.create_cohort_calls == []


def test_cohort_accepts_only_verified_annotators_and_keeps_workspace_scope():
    repository = _Repository([_annotator_record(status="verified")])
    service = api.PanelAnnotatorService(repository)

    response = service.create_cohort(
        {
            "workspace": "workspace-a",
            "name": "reviewers",
            "default_capacity": 3,
            "member_annotator_ids": ["annotator-1"],
        }
    )

    assert response.workspace == "workspace-a"
    assert response.member_annotator_ids == ("annotator-1",)
    assert repository.create_cohort_calls[0]["workspace"] == "workspace-a"


def test_cohort_uses_optional_batch_repository_path_without_n_plus_one_reads():
    repository = _BatchRepository([_annotator_record(status="verified")])
    service = api.PanelAnnotatorService(repository)

    response = service.create_cohort(
        {
            "workspace": "workspace-a",
            "name": "reviewers",
            "default_capacity": 3,
            "member_annotator_ids": ["annotator-1"],
        }
    )

    assert response.member_annotator_ids == ("annotator-1",)
    assert repository.batch_calls == [
        {"workspace": "workspace-a", "annotator_ids": ("annotator-1",)}
    ]
    assert repository.get_annotator_calls == []


def test_workspace_scope_errors_map_to_safe_http_contract():
    authorizer = _Authorizer(api.WorkspaceResourceNotFound())
    service = api.PanelAnnotatorService(_Repository(), authorizer=authorizer)

    with pytest.raises(api.PanelAnnotatorError) as exc_info:
        service.list_annotators("workspace-hidden", actor="principal-1")

    assert exc_info.value.status == 404
    assert exc_info.value.to_payload() == {
        "error": "资源不存在",
        "code": "resource_not_found",
    }

    payload = api.workspace_error_payload(api.WorkspacePermissionDenied())
    assert payload == {"error": "权限不足", "code": "permission_denied"}
