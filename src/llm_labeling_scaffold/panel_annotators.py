from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from enum import Enum
from http import HTTPStatus
from typing import Any, Protocol
from uuid import UUID


SCHEMA_VERSION = "panel_annotators_v1"


def _workspace_manage_permission() -> str:
    try:
        from .db.rbac import Permission
    except ImportError:
        return "workspace:manage"
    return Permission.WORKSPACE_MANAGE.value


WORKSPACE_MANAGE_PERMISSION = _workspace_manage_permission()

SERVER_CONTEXT_FIELDS = frozenset(
    {
        "actor",
        "actor_id",
        "caller",
        "caller_id",
        "channel",
        "audit_channel",
        "permission",
        "permissions",
        "authorization",
    }
)
SENSITIVE_FIELDS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "initial_password",
        "current_password",
        "password_confirmation",
        "api_key",
        "apikey",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "owner_token",
        "authorization",
        "cookie",
        "credential",
        "credentials",
    }
)

ANNOTATOR_CREATE_REQUEST_FIELDS = frozenset(
    {
        "workspace",
        "scaffold_user_id",
        "personal_workspace_name",
        "initial_password",
    }
)
ANNOTATOR_BIND_REQUEST_FIELDS = frozenset(
    {
        "workspace",
        "scaffold_user_id",
        "argilla_user_id",
        "argilla_username",
        "personal_workspace_id",
    }
)
ANNOTATOR_VERIFY_REQUEST_FIELDS = frozenset({"workspace", "annotator_id"})
COHORT_CREATE_REQUEST_FIELDS = frozenset(
    {"workspace", "name", "default_capacity", "member_annotator_ids"}
)
COHORT_UPDATE_REQUEST_FIELDS = frozenset(
    {"workspace", "cohort_id", "name", "default_capacity", "expected_revision"}
)
COHORT_MEMBERS_REQUEST_FIELDS = frozenset(
    {
        "workspace",
        "cohort_id",
        "member_annotator_ids",
        "default_capacity",
        "expected_revision",
    }
)
REQUEST_FIELD_ALLOWLISTS = {
    "annotator_create": ANNOTATOR_CREATE_REQUEST_FIELDS,
    "annotator_bind": ANNOTATOR_BIND_REQUEST_FIELDS,
    "annotator_verify": ANNOTATOR_VERIFY_REQUEST_FIELDS,
    "cohort_create": COHORT_CREATE_REQUEST_FIELDS,
    "cohort_update": COHORT_UPDATE_REQUEST_FIELDS,
    "cohort_members": COHORT_MEMBERS_REQUEST_FIELDS,
}


class PanelAnnotatorError(ValueError):
    """Safe error contract for the future Panel route."""

    def __init__(
        self,
        code: str,
        message: str,
        status: int | HTTPStatus,
        *,
        field: str | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.status = int(status)
        self.field = field
        super().__init__(message)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": self.message, "code": self.code}
        if self.field is not None:
            payload["field"] = self.field
        return payload


class AnnotatorDTOError(PanelAnnotatorError):
    def __init__(self, code: str, message: str, *, field: str | None = None) -> None:
        super().__init__(code, message, HTTPStatus.UNPROCESSABLE_ENTITY, field=field)


class SensitiveInputError(AnnotatorDTOError):
    def __init__(self, *, field: str | None = None) -> None:
        super().__init__(
            "sensitive_input_forbidden",
            "敏感字段只能通过专用一次性接口提交",
            field=field,
        )


class CorruptRecordError(ValueError):
    pass


class InternalServiceError(PanelAnnotatorError):
    def __init__(self) -> None:
        super().__init__(
            "service_unavailable",
            "标注人员服务暂时不可用",
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )


class WorkspaceResourceNotFound(RuntimeError):
    code = "resource_not_found"


class WorkspacePermissionDenied(RuntimeError):
    code = "permission_denied"


class WorkspaceUnavailable(RuntimeError):
    code = "authorization_unavailable"


class RepositoryUnavailable(RuntimeError):
    code = "repository_unavailable"


class ExternalAdapterUnavailable(RuntimeError):
    code = "external_service_unavailable"


class VerificationStatus(str, Enum):
    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    REJECTED = "rejected"
    IDENTITY_DRIFT = "identity_drift"
    ROLE_ERROR = "role_error"
    MEMBERSHIP_MISSING = "membership_missing"
    EXTERNAL_UNAVAILABLE = "external_unavailable"

    @property
    def label(self) -> str:
        return {
            VerificationStatus.UNVERIFIED: "未验证",
            VerificationStatus.VERIFIED: "已验证",
            VerificationStatus.REJECTED: "已拒绝",
            VerificationStatus.IDENTITY_DRIFT: "身份漂移",
            VerificationStatus.ROLE_ERROR: "角色错误",
            VerificationStatus.MEMBERSHIP_MISSING: "工作区成员关系缺失",
            VerificationStatus.EXTERNAL_UNAVAILABLE: "外部服务不可用",
        }[self]

    @classmethod
    def parse(cls, value: Any) -> "VerificationStatus":
        if isinstance(value, cls):
            return value
        if isinstance(value, Enum):
            value = value.value
        if not isinstance(value, str):
            raise AnnotatorDTOError(
                "invalid_verification_status", "verification 状态无效"
            )
        normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {
            "not_verified": cls.UNVERIFIED,
            "workspace_membership_missing": cls.MEMBERSHIP_MISSING,
            "external_service_unavailable": cls.EXTERNAL_UNAVAILABLE,
            "unavailable": cls.EXTERNAL_UNAVAILABLE,
        }
        normalized = aliases.get(normalized, normalized)
        try:
            return cls(normalized)
        except ValueError as exc:
            raise AnnotatorDTOError(
                "invalid_verification_status", "verification 状态无效"
            ) from exc


@dataclass(frozen=True, slots=True)
class VerificationState:
    status: VerificationStatus = VerificationStatus.UNVERIFIED
    last_verified_at: str | None = None

    @classmethod
    def from_source(cls, source: Any) -> "VerificationState":
        if isinstance(source, cls):
            return source
        if isinstance(source, Mapping):
            status = source.get(
                "status", source.get("state", source.get("verification_status"))
            )
            timestamp = source.get(
                "last_verified_at",
                source.get("verified_at", source.get("checked_at")),
            )
        else:
            status = _source_value(
                source, "verification_status", "verification_state", "status"
            )
            timestamp = _source_value(
                source, "last_verified_at", "verified_at", "checked_at"
            )
        if status is None:
            status = VerificationStatus.UNVERIFIED
        return cls(
            status=VerificationStatus.parse(status),
            last_verified_at=_safe_timestamp(timestamp),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "label": self.status.label,
            "last_verified_at": self.last_verified_at,
        }


def serialize_verification(
    value: VerificationState | VerificationStatus | str | Mapping[str, Any],
) -> dict[str, Any]:
    try:
        if isinstance(value, str):
            return VerificationState(status=VerificationStatus.parse(value)).to_dict()
        return VerificationState.from_source(value).to_dict()
    except Exception:
        raise _internal_service_error() from None


@dataclass(frozen=True, slots=True)
class WorkspaceScope:
    workspace: str
    actor: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        _required_text(self.workspace, "workspace", max_length=255)

    @property
    def workspace_id(self) -> str:
        return self.workspace


@dataclass(frozen=True, slots=True)
class CreateAnnotatorRequest:
    workspace: str
    scaffold_user_id: str
    initial_password: str = field(repr=False)
    personal_workspace_name: str | None = None

    def safe_dict(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "scaffold_user_id": self.scaffold_user_id,
            "personal_workspace_name": self.personal_workspace_name,
        }


@dataclass(frozen=True, slots=True)
class BindAnnotatorRequest:
    workspace: str
    scaffold_user_id: str
    argilla_user_id: str
    argilla_username: str | None = None
    personal_workspace_id: str | None = None


@dataclass(frozen=True, slots=True)
class VerifyAnnotatorRequest:
    workspace: str
    annotator_id: str


@dataclass(frozen=True, slots=True)
class CreateCohortRequest:
    workspace: str
    name: str
    default_capacity: int
    member_annotator_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class UpdateCohortRequest:
    workspace: str
    cohort_id: str
    name: str | None = None
    default_capacity: int | None = None
    expected_revision: int | None = None


@dataclass(frozen=True, slots=True)
class CohortMembersRequest:
    workspace: str
    cohort_id: str
    member_annotator_ids: tuple[str, ...]
    default_capacity: int | None = None
    expected_revision: int | None = None


def reject_sensitive_fields(
    payload: Any, *, allow_initial_password: bool = False
) -> None:
    """Reject secrets at every request nesting level without reading them into errors."""

    def visit(value: Any, *, root: bool) -> None:
        if isinstance(value, Mapping):
            for key, nested in value.items():
                if not isinstance(key, str):
                    raise AnnotatorDTOError("invalid_field_name", "请求字段名无效")
                normalized = _field_name(key)
                if normalized in SERVER_CONTEXT_FIELDS:
                    raise AnnotatorDTOError(
                        "server_context_forbidden",
                        "actor、caller 和 permission 由服务端确定",
                        field=key,
                    )
                if normalized in SENSITIVE_FIELDS and not (
                    allow_initial_password and root and normalized == "initial_password"
                ):
                    raise SensitiveInputError(field=key)
                visit(nested, root=False)
        elif isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            for nested in value:
                visit(nested, root=False)

    visit(payload, root=True)


def parse_create_annotator_request(payload: Any) -> CreateAnnotatorRequest:
    root = _request_mapping(
        payload, ANNOTATOR_CREATE_REQUEST_FIELDS, "annotator_create"
    )
    reject_sensitive_fields(root, allow_initial_password=True)
    _ensure_fields(root, ANNOTATOR_CREATE_REQUEST_FIELDS, "annotator_create")
    return CreateAnnotatorRequest(
        workspace=_required_text(root.get("workspace"), "workspace"),
        scaffold_user_id=_required_text(
            root.get("scaffold_user_id"), "scaffold_user_id"
        ),
        initial_password=_initial_password(root.get("initial_password")),
        personal_workspace_name=_optional_text(
            root.get("personal_workspace_name"), "personal_workspace_name"
        ),
    )


def parse_bind_annotator_request(payload: Any) -> BindAnnotatorRequest:
    root = _request_mapping(payload, ANNOTATOR_BIND_REQUEST_FIELDS, "annotator_bind")
    reject_sensitive_fields(root)
    _ensure_fields(root, ANNOTATOR_BIND_REQUEST_FIELDS, "annotator_bind")
    return BindAnnotatorRequest(
        workspace=_required_text(root.get("workspace"), "workspace"),
        scaffold_user_id=_required_text(
            root.get("scaffold_user_id"), "scaffold_user_id"
        ),
        argilla_user_id=_required_text(root.get("argilla_user_id"), "argilla_user_id"),
        argilla_username=_optional_text(
            root.get("argilla_username"), "argilla_username"
        ),
        personal_workspace_id=_optional_text(
            root.get("personal_workspace_id"), "personal_workspace_id"
        ),
    )


def parse_verify_annotator_request(payload: Any) -> VerifyAnnotatorRequest:
    root = _request_mapping(
        payload, ANNOTATOR_VERIFY_REQUEST_FIELDS, "annotator_verify"
    )
    reject_sensitive_fields(root)
    _ensure_fields(root, ANNOTATOR_VERIFY_REQUEST_FIELDS, "annotator_verify")
    return VerifyAnnotatorRequest(
        workspace=_required_text(root.get("workspace"), "workspace"),
        annotator_id=_required_text(root.get("annotator_id"), "annotator_id"),
    )


def parse_create_cohort_request(payload: Any) -> CreateCohortRequest:
    root = _request_mapping(payload, COHORT_CREATE_REQUEST_FIELDS, "cohort_create")
    reject_sensitive_fields(root)
    _ensure_fields(root, COHORT_CREATE_REQUEST_FIELDS, "cohort_create")
    return CreateCohortRequest(
        workspace=_required_text(root.get("workspace"), "workspace"),
        name=_required_text(root.get("name"), "name"),
        default_capacity=_positive_int(
            root.get("default_capacity"), "default_capacity"
        ),
        member_annotator_ids=_identifier_list(
            root.get("member_annotator_ids", []), "member_annotator_ids"
        ),
    )


def parse_update_cohort_request(payload: Any) -> UpdateCohortRequest:
    root = _request_mapping(payload, COHORT_UPDATE_REQUEST_FIELDS, "cohort_update")
    reject_sensitive_fields(root)
    _ensure_fields(root, COHORT_UPDATE_REQUEST_FIELDS, "cohort_update")
    name = _optional_text(root.get("name"), "name")
    capacity = root.get("default_capacity")
    expected_revision = root.get("expected_revision")
    if name is None and capacity is None:
        raise AnnotatorDTOError("empty_update", "人员组更新至少需要一个可变字段")
    return UpdateCohortRequest(
        workspace=_required_text(root.get("workspace"), "workspace"),
        cohort_id=_required_text(root.get("cohort_id"), "cohort_id"),
        name=name,
        default_capacity=(
            _positive_int(capacity, "default_capacity")
            if capacity is not None
            else None
        ),
        expected_revision=(
            _nonnegative_int(expected_revision, "expected_revision")
            if expected_revision is not None
            else None
        ),
    )


def parse_cohort_members_request(payload: Any) -> CohortMembersRequest:
    root = _request_mapping(payload, COHORT_MEMBERS_REQUEST_FIELDS, "cohort_members")
    reject_sensitive_fields(root)
    _ensure_fields(root, COHORT_MEMBERS_REQUEST_FIELDS, "cohort_members")
    expected_revision = root.get("expected_revision")
    return CohortMembersRequest(
        workspace=_required_text(root.get("workspace"), "workspace"),
        cohort_id=_required_text(root.get("cohort_id"), "cohort_id"),
        member_annotator_ids=_identifier_list(
            root.get("member_annotator_ids"), "member_annotator_ids"
        ),
        default_capacity=(
            _positive_int(root.get("default_capacity"), "default_capacity")
            if root.get("default_capacity") is not None
            else None
        ),
        expected_revision=(
            _nonnegative_int(expected_revision, "expected_revision")
            if expected_revision is not None
            else None
        ),
    )


@dataclass(frozen=True, slots=True)
class WorkspaceMembershipResponse:
    workspace_id: str | None
    present: bool
    role: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "present": self.present,
            "role": self.role,
        }


@dataclass(frozen=True, slots=True)
class AnnotatorResponse:
    annotator_id: str
    workspace: str
    scaffold_user_id: str
    argilla_user_id: str | None
    argilla_username: str | None
    argilla_role: str | None
    personal_workspace_id: str | None
    membership: WorkspaceMembershipResponse
    verification: VerificationState
    created_at: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_record(cls, record: Any, *, workspace: str) -> "AnnotatorResponse":
        try:
            _assert_record_workspace(record, workspace)
            membership = _membership_from_record(record)
            return cls(
                annotator_id=_record_id(record, "annotator_id", "id", "mapping_id"),
                workspace=workspace,
                scaffold_user_id=_record_id(
                    record, "scaffold_user_id", "principal_id", "user_id"
                ),
                argilla_user_id=_optional_id(
                    record, "argilla_user_id", "argilla_user_uuid"
                ),
                argilla_username=_optional_text_value(
                    record, "argilla_username", "username"
                ),
                argilla_role=_optional_role(record, "argilla_role", "role"),
                personal_workspace_id=_optional_id(
                    record,
                    "personal_workspace_id",
                    "personal_argilla_workspace_id",
                    "argilla_workspace_id",
                ),
                membership=membership,
                verification=_verification_from_record(record),
                created_at=_safe_timestamp(_source_value(record, "created_at")),
                updated_at=_safe_timestamp(_source_value(record, "updated_at")),
            )
        except Exception:
            raise _internal_service_error() from None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "annotator_id": self.annotator_id,
            "workspace": self.workspace,
            "scaffold_user_id": self.scaffold_user_id,
            "argilla_user_id": self.argilla_user_id,
            "argilla_username": self.argilla_username,
            "argilla_role": self.argilla_role,
            "personal_workspace_id": self.personal_workspace_id,
            "membership": self.membership.to_dict(),
            "verification": self.verification.to_dict(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class CohortResponse:
    cohort_id: str
    workspace: str
    name: str
    default_capacity: int
    member_annotator_ids: tuple[str, ...]
    revision: int | None = None
    created_at: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_record(cls, record: Any, *, workspace: str) -> "CohortResponse":
        try:
            _assert_record_workspace(record, workspace)
            member_ids = _source_value(
                record, "member_annotator_ids", "annotator_ids", "members"
            )
            if isinstance(member_ids, Mapping):
                member_ids = list(member_ids)
            if member_ids is None:
                member_ids = []
            if not isinstance(member_ids, Sequence) or isinstance(
                member_ids, (str, bytes, bytearray)
            ):
                raise CorruptRecordError()
            parsed_members: list[str] = []
            for value in member_ids:
                parsed_members.append(_safe_identifier(value, "member_annotator_ids"))
            capacity = _source_value(record, "default_capacity", "capacity")
            if (
                isinstance(capacity, bool)
                or not isinstance(capacity, int)
                or capacity <= 0
            ):
                raise CorruptRecordError()
            revision = _source_value(record, "revision", "version")
            if revision is not None and (
                isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision < 0
            ):
                raise CorruptRecordError()
            return cls(
                cohort_id=_record_id(record, "cohort_id", "id"),
                workspace=workspace,
                name=_required_text(_source_value(record, "name"), "name"),
                default_capacity=capacity,
                member_annotator_ids=tuple(parsed_members),
                revision=revision,
                created_at=_safe_timestamp(_source_value(record, "created_at")),
                updated_at=_safe_timestamp(_source_value(record, "updated_at")),
            )
        except Exception:
            raise _internal_service_error() from None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "cohort_id": self.cohort_id,
            "workspace": self.workspace,
            "name": self.name,
            "default_capacity": self.default_capacity,
            "member_annotator_ids": list(self.member_annotator_ids),
            "member_count": len(self.member_annotator_ids),
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def serialize_annotator(record: Any, *, workspace: str) -> dict[str, Any]:
    return AnnotatorResponse.from_record(record, workspace=workspace).to_dict()


def serialize_cohort(record: Any, *, workspace: str) -> dict[str, Any]:
    return CohortResponse.from_record(record, workspace=workspace).to_dict()


@dataclass(frozen=True, slots=True)
class ExternalAnnotatorSnapshot:
    argilla_user_id: str
    argilla_username: str
    role: str
    personal_workspace_id: str | None
    membership_present: bool
    membership_role: str | None = None
    is_owner: bool = False

    @classmethod
    def from_source(cls, source: Any) -> "ExternalAnnotatorSnapshot":
        membership = _source_value(source, "membership")
        membership_present = _bool_value(
            _source_value(membership, "present", "is_member", "exists")
            if membership is not None
            else _source_value(source, "membership_present", "is_member")
        )
        membership_role = _optional_role(
            membership if membership is not None else source,
            "role",
            "membership_role",
        )
        return cls(
            argilla_user_id=_record_id(
                source, "argilla_user_id", "argilla_user_uuid", "user_id"
            ),
            argilla_username=_required_text(
                _source_value(source, "argilla_username", "username"),
                "argilla_username",
            ),
            role=_required_role(_source_value(source, "role", "argilla_role")),
            personal_workspace_id=_optional_id(
                source,
                "personal_workspace_id",
                "argilla_workspace_id",
                "workspace_id",
            ),
            membership_present=membership_present,
            membership_role=membership_role,
            is_owner=_bool_value(_source_value(source, "is_owner", "owner")),
        )

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "argilla_user_id": self.argilla_user_id,
            "argilla_username": self.argilla_username,
            "role": self.role,
            "personal_workspace_id": self.personal_workspace_id,
            "membership_present": self.membership_present,
            "membership_role": self.membership_role,
            "is_owner": self.is_owner,
        }


@dataclass(frozen=True, slots=True)
class AnnotatorLookup:
    workspace: str
    annotator_id: str
    scaffold_user_id: str
    argilla_user_id: str | None
    argilla_username: str | None
    personal_workspace_id: str | None


class WorkspaceAuthorizer(Protocol):
    def require_workspace(
        self, *, workspace: str, actor: Any, permission: str
    ) -> Any: ...


class AnnotatorRepository(Protocol):
    def list_annotators(self, *, workspace: str) -> Iterable[Any]: ...

    def get_annotator(self, *, workspace: str, annotator_id: str) -> Any | None: ...

    def create_annotator(
        self,
        *,
        workspace: str,
        scaffold_user_id: str,
        external: ExternalAnnotatorSnapshot,
    ) -> Any: ...

    def update_annotator_verification(
        self,
        *,
        workspace: str,
        annotator_id: str,
        verification: VerificationState,
    ) -> Any | None: ...

    def list_cohorts(self, *, workspace: str) -> Iterable[Any]: ...

    def get_cohort(self, *, workspace: str, cohort_id: str) -> Any | None: ...

    def create_cohort(
        self,
        *,
        workspace: str,
        name: str,
        default_capacity: int,
        member_annotator_ids: Sequence[str],
    ) -> Any: ...

    def update_cohort(
        self,
        *,
        workspace: str,
        cohort_id: str,
        name: str | None,
        default_capacity: int | None,
        expected_revision: int | None,
    ) -> Any: ...

    def replace_cohort_members(
        self,
        *,
        workspace: str,
        cohort_id: str,
        member_annotator_ids: Sequence[str],
        default_capacity: int | None,
        expected_revision: int | None,
    ) -> Any: ...


class BatchAnnotatorRepository(Protocol):
    def get_annotators(
        self, *, workspace: str, annotator_ids: Sequence[str]
    ) -> Iterable[Any]: ...


class AnnotatorAdapter(Protocol):
    def provision_annotator(
        self,
        *,
        workspace: str,
        scaffold_user_id: str,
        personal_workspace_name: str | None,
        initial_password: str,
    ) -> Any: ...

    def bind_annotator(
        self,
        *,
        workspace: str,
        scaffold_user_id: str,
        argilla_user_id: str,
        argilla_username: str | None,
        personal_workspace_id: str | None,
    ) -> Any: ...

    def verify_annotator(self, *, annotator: AnnotatorLookup) -> Any: ...


class PanelAnnotatorService:
    def __init__(
        self,
        repository: AnnotatorRepository,
        adapter: AnnotatorAdapter | None = None,
        authorizer: WorkspaceAuthorizer | None = None,
        *,
        permission: str = WORKSPACE_MANAGE_PERMISSION,
    ) -> None:
        self.repository = repository
        self.adapter = adapter
        self.authorizer = authorizer
        self.permission = permission

    def list_annotators(
        self, scope: WorkspaceScope | str, *, actor: Any = None
    ) -> list[AnnotatorResponse]:
        resolved = self._scope(scope, actor=actor)
        self._authorize(resolved)
        records = self._repository_call("list_annotators", workspace=resolved.workspace)
        return [
            AnnotatorResponse.from_record(record, workspace=resolved.workspace)
            for record in records
        ]

    def get_annotator(
        self,
        scope: WorkspaceScope | str,
        annotator_id: str,
        *,
        actor: Any = None,
    ) -> AnnotatorResponse:
        resolved = self._scope(scope, actor=actor)
        self._authorize(resolved)
        record = self._repository_call(
            "get_annotator",
            workspace=resolved.workspace,
            annotator_id=_required_text(annotator_id, "annotator_id"),
        )
        if record is None:
            raise _not_found()
        return AnnotatorResponse.from_record(record, workspace=resolved.workspace)

    def create_annotator(self, payload: Any, *, actor: Any = None) -> AnnotatorResponse:
        request = (
            payload
            if isinstance(payload, CreateAnnotatorRequest)
            else parse_create_annotator_request(payload)
        )
        resolved = self._scope(request.workspace, actor=actor)
        self._authorize(resolved)
        if self.adapter is None:
            raise _external_unavailable()
        workspace = request.workspace
        scaffold_user_id = request.scaffold_user_id
        workspace_name = request.personal_workspace_name
        initial_password = request.initial_password
        del request
        try:
            external = self._adapter_call(
                "provision_annotator",
                workspace=workspace,
                scaffold_user_id=scaffold_user_id,
                personal_workspace_name=workspace_name,
                initial_password=initial_password,
            )
        finally:
            initial_password = ""
        snapshot = self._validated_snapshot(external)
        record = self._repository_call(
            "create_annotator",
            workspace=workspace,
            scaffold_user_id=scaffold_user_id,
            external=snapshot,
        )
        return AnnotatorResponse.from_record(record, workspace=workspace)

    def bind_annotator(self, payload: Any, *, actor: Any = None) -> AnnotatorResponse:
        request = (
            payload
            if isinstance(payload, BindAnnotatorRequest)
            else parse_bind_annotator_request(payload)
        )
        resolved = self._scope(request.workspace, actor=actor)
        self._authorize(resolved)
        if self.adapter is None:
            raise _external_unavailable()
        external = self._validated_snapshot(
            self._adapter_call(
                "bind_annotator",
                workspace=request.workspace,
                scaffold_user_id=request.scaffold_user_id,
                argilla_user_id=request.argilla_user_id,
                argilla_username=request.argilla_username,
                personal_workspace_id=request.personal_workspace_id,
            )
        )
        _ensure_same_identity(request, external)
        record = self._repository_call(
            "create_annotator",
            workspace=request.workspace,
            scaffold_user_id=request.scaffold_user_id,
            external=external,
        )
        return AnnotatorResponse.from_record(record, workspace=request.workspace)

    def verify_annotator(self, payload: Any, *, actor: Any = None) -> AnnotatorResponse:
        request = (
            payload
            if isinstance(payload, VerifyAnnotatorRequest)
            else parse_verify_annotator_request(payload)
        )
        resolved = self._scope(request.workspace, actor=actor)
        self._authorize(resolved)
        record = self._repository_call(
            "get_annotator",
            workspace=request.workspace,
            annotator_id=request.annotator_id,
        )
        if record is None:
            raise _not_found()
        current = AnnotatorResponse.from_record(record, workspace=request.workspace)
        if self.adapter is None:
            return replace(
                current,
                verification=VerificationState(
                    VerificationStatus.EXTERNAL_UNAVAILABLE,
                    current.verification.last_verified_at,
                ),
            )
        lookup = AnnotatorLookup(
            workspace=request.workspace,
            annotator_id=current.annotator_id,
            scaffold_user_id=current.scaffold_user_id,
            argilla_user_id=current.argilla_user_id,
            argilla_username=current.argilla_username,
            personal_workspace_id=current.personal_workspace_id,
        )
        try:
            snapshot = self._snapshot_from_source(
                self._adapter_call("verify_annotator", annotator=lookup)
            )
        except PanelAnnotatorError as exc:
            if exc.code != "external_service_unavailable":
                raise
            return replace(
                current,
                verification=VerificationState(
                    VerificationStatus.EXTERNAL_UNAVAILABLE,
                    current.verification.last_verified_at,
                ),
            )
        verification = _verification_for(current, snapshot)
        updated = self._repository_call_optional(
            "update_annotator_verification",
            workspace=request.workspace,
            annotator_id=current.annotator_id,
            verification=verification,
        )
        if updated is None:
            return replace(current, verification=verification)
        return AnnotatorResponse.from_record(updated, workspace=request.workspace)

    def list_cohorts(
        self, scope: WorkspaceScope | str, *, actor: Any = None
    ) -> list[CohortResponse]:
        resolved = self._scope(scope, actor=actor)
        self._authorize(resolved)
        records = self._repository_call("list_cohorts", workspace=resolved.workspace)
        return [
            CohortResponse.from_record(record, workspace=resolved.workspace)
            for record in records
        ]

    def get_cohort(
        self,
        scope: WorkspaceScope | str,
        cohort_id: str,
        *,
        actor: Any = None,
    ) -> CohortResponse:
        resolved = self._scope(scope, actor=actor)
        self._authorize(resolved)
        record = self._repository_call(
            "get_cohort",
            workspace=resolved.workspace,
            cohort_id=_required_text(cohort_id, "cohort_id"),
        )
        if record is None:
            raise _not_found()
        return CohortResponse.from_record(record, workspace=resolved.workspace)

    def create_cohort(self, payload: Any, *, actor: Any = None) -> CohortResponse:
        request = (
            payload
            if isinstance(payload, CreateCohortRequest)
            else parse_create_cohort_request(payload)
        )
        resolved = self._scope(request.workspace, actor=actor)
        self._authorize(resolved)
        self._validate_members(resolved.workspace, request.member_annotator_ids)
        record = self._repository_call(
            "create_cohort",
            workspace=request.workspace,
            name=request.name,
            default_capacity=request.default_capacity,
            member_annotator_ids=request.member_annotator_ids,
        )
        return CohortResponse.from_record(record, workspace=request.workspace)

    def update_cohort(self, payload: Any, *, actor: Any = None) -> CohortResponse:
        request = (
            payload
            if isinstance(payload, UpdateCohortRequest)
            else parse_update_cohort_request(payload)
        )
        resolved = self._scope(request.workspace, actor=actor)
        self._authorize(resolved)
        record = self._repository_call(
            "update_cohort",
            workspace=request.workspace,
            cohort_id=request.cohort_id,
            name=request.name,
            default_capacity=request.default_capacity,
            expected_revision=request.expected_revision,
        )
        if record is None:
            raise _not_found()
        return CohortResponse.from_record(record, workspace=request.workspace)

    def replace_cohort_members(
        self, payload: Any, *, actor: Any = None
    ) -> CohortResponse:
        request = (
            payload
            if isinstance(payload, CohortMembersRequest)
            else parse_cohort_members_request(payload)
        )
        resolved = self._scope(request.workspace, actor=actor)
        self._authorize(resolved)
        self._validate_members(resolved.workspace, request.member_annotator_ids)
        record = self._repository_call(
            "replace_cohort_members",
            workspace=request.workspace,
            cohort_id=request.cohort_id,
            member_annotator_ids=request.member_annotator_ids,
            default_capacity=request.default_capacity,
            expected_revision=request.expected_revision,
        )
        if record is None:
            raise _not_found()
        return CohortResponse.from_record(record, workspace=request.workspace)

    def _scope(self, value: WorkspaceScope | str, *, actor: Any) -> WorkspaceScope:
        if isinstance(value, WorkspaceScope):
            if actor is None:
                return value
            return WorkspaceScope(value.workspace, actor)
        return WorkspaceScope(_required_text(value, "workspace"), actor)

    def _authorize(self, scope: WorkspaceScope) -> None:
        if self.authorizer is None:
            return
        try:
            method = getattr(self.authorizer, "require_workspace", None)
            if callable(method):
                decision = method(
                    workspace=scope.workspace,
                    actor=scope.actor,
                    permission=self.permission,
                )
            else:
                method = getattr(self.authorizer, "authorize_workspace", None)
                if callable(method):
                    decision = method(scope.actor, scope.workspace, self.permission)
                    require = getattr(self.authorizer, "require", None)
                    if callable(require):
                        require(decision)
                elif callable(self.authorizer):
                    decision = self.authorizer(
                        scope.workspace, scope.actor, self.permission
                    )
                else:
                    raise WorkspaceUnavailable()
            if decision is False:
                raise WorkspacePermissionDenied()
        except PanelAnnotatorError:
            raise
        except Exception as exc:
            raise map_workspace_error(exc) from None

    def _repository_call(self, method_name: str, **kwargs: Any) -> Any:
        method = getattr(self.repository, method_name, None)
        if not callable(method):
            raise map_workspace_error(RepositoryUnavailable())
        try:
            return method(**kwargs)
        except PanelAnnotatorError:
            raise
        except Exception as exc:
            raise map_workspace_error(exc) from None

    def _repository_call_optional(self, method_name: str, **kwargs: Any) -> Any | None:
        method = getattr(self.repository, method_name, None)
        if not callable(method):
            return None
        try:
            return method(**kwargs)
        except PanelAnnotatorError:
            raise
        except Exception as exc:
            raise map_workspace_error(exc) from None

    def _adapter_call(self, name: str, **kwargs: Any) -> Any:
        if self.adapter is None:
            raise _external_unavailable()
        method = getattr(self.adapter, name, None)
        if not callable(method):
            raise _external_unavailable()
        try:
            return method(**kwargs)
        except Exception:
            pass
        raise _external_unavailable()

    def _snapshot_from_source(self, source: Any) -> ExternalAnnotatorSnapshot:
        try:
            snapshot = (
                source
                if isinstance(source, ExternalAnnotatorSnapshot)
                else ExternalAnnotatorSnapshot.from_source(source)
            )
        except PanelAnnotatorError:
            raise _external_unavailable()
        except Exception:
            raise _external_unavailable() from None
        return snapshot

    def _validated_snapshot(self, source: Any) -> ExternalAnnotatorSnapshot:
        snapshot = self._snapshot_from_source(source)
        if snapshot.is_owner:
            raise PanelAnnotatorError(
                "owner_account_forbidden",
                "owner 账号不能作为业务标注人员",
                HTTPStatus.UNPROCESSABLE_ENTITY,
            )
        if snapshot.role != "annotator":
            raise PanelAnnotatorError(
                "annotator_role_required",
                "Argilla 用户必须具有 annotator 角色",
                HTTPStatus.UNPROCESSABLE_ENTITY,
            )
        if not snapshot.membership_present:
            raise PanelAnnotatorError(
                "workspace_membership_required",
                "Argilla 工作区成员关系缺失",
                HTTPStatus.UNPROCESSABLE_ENTITY,
            )
        return snapshot

    def _validate_members(self, workspace: str, member_ids: Sequence[str]) -> None:
        for record in self._member_records(workspace, member_ids):
            self._validate_member_record(record)

    def _member_records(self, workspace: str, member_ids: Sequence[str]) -> list[Any]:
        batch_method = getattr(self.repository, "get_annotators", None)
        if callable(batch_method):
            records = self._repository_call(
                "get_annotators",
                workspace=workspace,
                annotator_ids=tuple(member_ids),
            )
            try:
                by_id: dict[str, Any] = {}
                for record in records:
                    _assert_record_workspace(record, workspace)
                    annotator_id = _record_id(
                        record, "annotator_id", "id", "mapping_id"
                    )
                    if annotator_id in by_id:
                        raise CorruptRecordError()
                    by_id[annotator_id] = record
            except Exception:
                raise _internal_service_error() from None
            if any(annotator_id not in by_id for annotator_id in member_ids):
                raise _not_found()
            return [by_id[annotator_id] for annotator_id in member_ids]

        records = []
        for annotator_id in member_ids:
            record = self._repository_call(
                "get_annotator",
                workspace=workspace,
                annotator_id=annotator_id,
            )
            if record is None:
                raise _not_found()
            records.append(record)
        return records

    def _validate_member_record(self, record: Any) -> None:
        try:
            role = _optional_role(record, "argilla_role", "role")
            owner = _bool_value(_source_value(record, "is_owner", "owner"))
            verification = _verification_from_record(record)
        except Exception:
            raise _internal_service_error() from None
        if role is None:
            raise _internal_service_error()
        if owner or role == "owner":
            raise PanelAnnotatorError(
                "owner_account_forbidden",
                "owner 账号不能加入人员组",
                HTTPStatus.UNPROCESSABLE_ENTITY,
            )
        if role != "annotator":
            raise PanelAnnotatorError(
                "annotator_role_required",
                "人员组成员必须是 annotator",
                HTTPStatus.UNPROCESSABLE_ENTITY,
            )
        if verification.status != VerificationStatus.VERIFIED:
            raise PanelAnnotatorError(
                "annotator_not_verified",
                "未验证的标注人员不能加入人员组",
                HTTPStatus.CONFLICT,
            )


AnnotatorService = PanelAnnotatorService


def map_workspace_error(exc: BaseException) -> PanelAnnotatorError:
    if isinstance(exc, PanelAnnotatorError):
        return exc
    code = _dependency_code(exc)
    if code in {
        "resource_not_found",
        "workspace_not_found",
        "resource_not_visible",
        "unknown_workspace",
        "unknown_principal",
        "inactive_principal",
        "inactive_workspace",
        "not_found",
    }:
        return _not_found()
    if code in {
        "permission_denied",
        "role_denied",
        "forbidden",
        "access_denied",
        "invalid_audit_context",
    }:
        return PanelAnnotatorError(
            "permission_denied", "权限不足", HTTPStatus.FORBIDDEN
        )
    if (
        code in {"conflict", "already_exists", "duplicate", "idempotency_conflict"}
        or "conflict" in code
    ):
        return PanelAnnotatorError(
            "resource_conflict", "资源状态冲突", HTTPStatus.CONFLICT
        )
    if code in {
        "invalid_definition",
        "invalid_request",
        "validation_error",
        "unprocessable_entity",
    }:
        return AnnotatorDTOError("invalid_request", "请求定义无效")
    if code == "authorization_unavailable":
        return PanelAnnotatorError(
            "authorization_unavailable",
            "数据库授权暂时不可用",
            HTTPStatus.SERVICE_UNAVAILABLE,
        )
    if code == "repository_unavailable":
        return PanelAnnotatorError(
            "repository_unavailable",
            "标注人员资料库暂时不可用",
            HTTPStatus.SERVICE_UNAVAILABLE,
        )
    if code == "external_service_unavailable":
        return _external_unavailable()
    return PanelAnnotatorError(
        "service_unavailable",
        "标注人员服务暂时不可用",
        HTTPStatus.SERVICE_UNAVAILABLE,
    )


def workspace_error_payload(exc: BaseException) -> dict[str, Any]:
    return map_workspace_error(exc).to_payload()


def _request_mapping(
    payload: Any, allowed: frozenset[str], name: str
) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise AnnotatorDTOError("invalid_request", "请求体必须是对象")
    for key in payload:
        if not isinstance(key, str):
            raise AnnotatorDTOError("invalid_field_name", "请求字段名无效")
    return payload


def _ensure_fields(
    payload: Mapping[str, Any], allowed: frozenset[str], name: str
) -> None:
    for key in payload:
        if key not in allowed:
            raise AnnotatorDTOError("unknown_field", "请求包含不支持的字段", field=key)


def _required_text(value: Any, field_name: str, *, max_length: int = 255) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.strip()) > max_length
    ):
        raise AnnotatorDTOError("invalid_field", "请求字段无效", field=field_name)
    return value.strip()


def _optional_text(value: Any, field_name: str, *, max_length: int = 255) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name, max_length=max_length)


def _initial_password(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise AnnotatorDTOError(
            "invalid_initial_password", "一次性初始密码无效", field="initial_password"
        )
    return value


def _positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AnnotatorDTOError("invalid_field", "请求字段无效", field=field_name)
    return value


def _nonnegative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AnnotatorDTOError("invalid_field", "请求字段无效", field=field_name)
    return value


def _identifier_list(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise AnnotatorDTOError("invalid_field", "请求字段无效", field=field_name)
    result = tuple(_safe_identifier(item, field_name) for item in value)
    if len(set(result)) != len(result):
        raise AnnotatorDTOError(
            "duplicate_member", "人员组成员不能重复", field=field_name
        )
    return result


def _safe_identifier(value: Any, field_name: str) -> str:
    if isinstance(value, UUID):
        return str(value)
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 255:
        raise AnnotatorDTOError("invalid_field", "请求字段无效", field=field_name)
    return value.strip()


def _field_name(value: str) -> str:
    return value.strip().lower().replace("-", "_")


def _source_value(source: Any, *names: str) -> Any:
    if source is None:
        return None
    if isinstance(source, Mapping):
        for name in names:
            if name in source:
                return source[name]
        return None
    for name in names:
        try:
            return getattr(source, name)
        except AttributeError:
            continue
    return None


def _record_id(record: Any, *names: str) -> str:
    for name in names:
        value = _source_value(record, name)
        if value is not None:
            return _safe_identifier(value, name)
    raise AnnotatorDTOError("invalid_repository_record", "标注人员记录无效")


def _optional_id(record: Any, *names: str) -> str | None:
    for name in names:
        value = _source_value(record, name)
        if value is not None:
            return _safe_identifier(value, name)
    return None


def _optional_text_value(record: Any, *names: str) -> str | None:
    for name in names:
        value = _source_value(record, name)
        if value is not None:
            return _required_text(value, name)
    return None


def _required_role(value: Any) -> str:
    role = _optional_role_value(value)
    if role is None:
        raise AnnotatorDTOError("invalid_external_response", "外部标注人员响应无效")
    return role


def _optional_role(record: Any, *names: str) -> str | None:
    for name in names:
        value = _source_value(record, name)
        if value is not None:
            return _optional_role_value(value)
    return None


def _optional_role_value(value: Any) -> str | None:
    if isinstance(value, Enum):
        value = value.value
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().lower()


def _bool_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    raise CorruptRecordError()


def _safe_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _membership_from_record(record: Any) -> WorkspaceMembershipResponse:
    nested = _source_value(record, "membership")
    source = nested if nested is not None else record
    present = _source_value(
        source, "present", "is_member", "membership_present", "exists"
    )
    role = _optional_role(source, "role", "membership_role")
    workspace_id = _optional_id(
        source, "workspace_id", "argilla_workspace_id", "personal_workspace_id"
    )
    return WorkspaceMembershipResponse(
        workspace_id=workspace_id,
        present=_bool_value(present),
        role=role,
    )


def _verification_from_record(record: Any) -> VerificationState:
    try:
        nested = _source_value(record, "verification")
        if nested is not None:
            return VerificationState.from_source(nested)
        return VerificationState.from_source(record)
    except Exception:
        raise _internal_service_error() from None


def _assert_record_workspace(record: Any, workspace: str) -> None:
    record_workspace = _source_value(
        record, "workspace", "workspace_slug", "scaffold_workspace"
    )
    if record_workspace is not None and str(record_workspace) != workspace:
        raise _not_found()


def _verification_for(
    current: AnnotatorResponse, snapshot: ExternalAnnotatorSnapshot
) -> VerificationState:
    now = datetime.now(timezone.utc).isoformat()
    if snapshot.is_owner or snapshot.role != "annotator":
        status = VerificationStatus.ROLE_ERROR
    elif not snapshot.membership_present:
        status = VerificationStatus.MEMBERSHIP_MISSING
    elif (
        current.argilla_user_id != snapshot.argilla_user_id
        or current.argilla_username != snapshot.argilla_username
        or current.personal_workspace_id != snapshot.personal_workspace_id
    ):
        status = VerificationStatus.IDENTITY_DRIFT
    else:
        status = VerificationStatus.VERIFIED
    return VerificationState(status=status, last_verified_at=now)


def _ensure_same_identity(
    request: BindAnnotatorRequest, snapshot: ExternalAnnotatorSnapshot
) -> None:
    if request.argilla_user_id != snapshot.argilla_user_id:
        raise PanelAnnotatorError(
            "identity_drift",
            "Argilla 用户身份与请求不一致",
            HTTPStatus.CONFLICT,
        )
    if (
        request.argilla_username is not None
        and request.argilla_username != snapshot.argilla_username
    ):
        raise PanelAnnotatorError(
            "identity_drift",
            "Argilla 用户身份与请求不一致",
            HTTPStatus.CONFLICT,
        )
    if (
        request.personal_workspace_id is not None
        and request.personal_workspace_id != snapshot.personal_workspace_id
    ):
        raise PanelAnnotatorError(
            "identity_drift",
            "Argilla workspace 身份与请求不一致",
            HTTPStatus.CONFLICT,
        )


def _dependency_code(exc: BaseException) -> str:
    decision = getattr(exc, "decision", None)
    reason = getattr(decision, "reason", None)
    reason_value = getattr(reason, "value", None)
    for value in (getattr(exc, "code", None), reason_value, exc.__class__.__name__):
        if isinstance(value, str) and value.strip():
            return _field_name(value)
    return "unknown_error"


def _not_found() -> PanelAnnotatorError:
    return PanelAnnotatorError("resource_not_found", "资源不存在", HTTPStatus.NOT_FOUND)


def _external_unavailable() -> PanelAnnotatorError:
    return PanelAnnotatorError(
        "external_service_unavailable",
        "外部标注服务暂时不可用",
        HTTPStatus.SERVICE_UNAVAILABLE,
    )


def _internal_service_error() -> InternalServiceError:
    return InternalServiceError()


__all__ = [
    "ANNOTATOR_BIND_REQUEST_FIELDS",
    "ANNOTATOR_CREATE_REQUEST_FIELDS",
    "ANNOTATOR_VERIFY_REQUEST_FIELDS",
    "AnnotatorAdapter",
    "AnnotatorDTOError",
    "AnnotatorLookup",
    "AnnotatorRepository",
    "AnnotatorResponse",
    "AnnotatorService",
    "BatchAnnotatorRepository",
    "BindAnnotatorRequest",
    "COHORT_CREATE_REQUEST_FIELDS",
    "COHORT_MEMBERS_REQUEST_FIELDS",
    "COHORT_UPDATE_REQUEST_FIELDS",
    "CohortMembersRequest",
    "CohortResponse",
    "CreateAnnotatorRequest",
    "CreateCohortRequest",
    "ExternalAnnotatorSnapshot",
    "CorruptRecordError",
    "InternalServiceError",
    "PanelAnnotatorError",
    "PanelAnnotatorService",
    "REQUEST_FIELD_ALLOWLISTS",
    "SENSITIVE_FIELDS",
    "SensitiveInputError",
    "VerificationState",
    "VerificationStatus",
    "WorkspaceAuthorizer",
    "WorkspaceMembershipResponse",
    "WorkspaceScope",
    "map_workspace_error",
    "parse_bind_annotator_request",
    "parse_cohort_members_request",
    "parse_create_annotator_request",
    "parse_create_cohort_request",
    "parse_update_cohort_request",
    "parse_verify_annotator_request",
    "reject_sensitive_fields",
    "serialize_annotator",
    "serialize_cohort",
    "serialize_verification",
    "workspace_error_payload",
]
