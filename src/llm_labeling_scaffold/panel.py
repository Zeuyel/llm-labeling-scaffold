from __future__ import annotations

import ipaddress
import json
import os
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, unquote, urlparse

import yaml
from alembic.script import ScriptDirectory
from sqlalchemy import text

from . import __version__
from .auth import (
    ActorContext,
    PanelAuthenticationError,
    PanelAuthenticator,
    build_panel_authenticator,
    resolve_panel_auth_mode,
)
from .config import load_task
from .db import (
    AllocationNaturalKeyConflict,
    AllocationNotReady,
    AllocationOperationInProgress,
    AllocationRepositoryError,
    AllocationRepositoryValidationError,
    AllocationResourceNotFound,
    AnnotatorControlRepository,
    AnnotatorNaturalKeyConflict,
    AnnotatorNotReady,
    AnnotatorOperationInProgress,
    AnnotatorRepositoryError,
    AnnotatorRepositoryValidationError,
    AnnotatorResourceNotFound,
    AnnotatorVerificationRejected,
    AuditChannel,
    AuthorizationDenied,
    AuthorizationReason,
    AuthorizationUnavailable,
    CohortMemberInput,
    ControlTaskSnapshotLoader,
    DatabaseService,
    ExternalIdentity,
    IdempotencyConflict,
    Permission,
    TaskCreateConflict,
    TaskCreateInProgress,
    TaskDraftConflict,
    TaskDraftNotFound,
    TaskLifecycleInProgress,
    TaskLifecycleNotActive,
    TaskLifecycleReasonRequired,
    TaskLifecycleState,
    TaskLifecycleStateInvalid,
    TaskLifecycleTransitionInvalid,
    TaskPreconditionRequired,
    TaskPublishReasonRequired,
    TaskPublishInProgress,
    TrustedAllocationRepository,
    task_definition_fingerprint,
)
from .db.database import create_database_engine, create_session_factory
from .db.migration import build_alembic_config
from .io import read_json, read_jsonl, write_jsonl
from . import pipeline
from . import panel_settings
from .integrations.argilla_admin import (
    ArgillaAdminAdapter,
    ArgillaProvisioningError,
    derive_personal_workspace_name,
)
from .panel_allocation import (
    AllocationPreviewDTOError,
    parse_allocation_plan_confirm_json,
    parse_allocation_plan_create_json,
    parse_allocation_preview_json,
    serialize_allocation_confirmation_result,
    serialize_allocation_plan_result,
    serialize_allocation_progress,
    preview_allocation_dto,
)
from .panel_annotators import (
    AnnotatorLookup,
    AnnotatorResponse,
    ExternalAnnotatorSnapshot,
    PanelAnnotatorError,
    PanelAnnotatorService,
    VerificationState,
    VerificationStatus,
    WORKSPACE_MANAGE_PERMISSION,
)
from .redaction import redact_text

API_CONTRACT_VERSION = "2026-07-18"
AUTHORIZATION_READY = "ready"
AUTHORIZATION_UNAVAILABLE = "unavailable"

POOL_FILES = {
    "merged": ("merged", "merged_clean.jsonl"),
    "missing": ("merged", "missing_pool.jsonl"),
    "duplicate": ("merged", "duplicate_pool.jsonl"),
    "conflict": ("merged", "conflict_pool.jsonl"),
}

_TASK_REGISTRY_SYNC_CACHE: dict[tuple[str, str, str, str], tuple[float, object]] = {}
_TASK_REGISTRY_SYNC_CACHE_LOCK = threading.Lock()
_DEFAULT_TASK_REGISTRY_SYNC_TTL_SECONDS = 5.0
_PANEL_DEPLOYMENT_MODES = {"host", "docker_loopback", "docker_tunnel"}


class _IPv6ThreadingHTTPServer(ThreadingHTTPServer):
    address_family = socket.AF_INET6


def _resolve_panel_deployment_mode(explicit: str | None = None) -> str:
    mode = str(explicit or os.environ.get("LLS_PANEL_DEPLOYMENT_MODE") or "host").strip().lower()
    if mode not in _PANEL_DEPLOYMENT_MODES:
        raise ValueError(
            "LLS_PANEL_DEPLOYMENT_MODE 只能是 host、docker_loopback 或 docker_tunnel"
        )
    return mode


def _ip_address(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(str(host or "").strip())
    except ValueError:
        return None


def _normalize_bind_host(host: str) -> str:
    value = str(host or "").strip()
    if value.lower().rstrip(".") == "localhost":
        return "127.0.0.1"
    return value


def _is_loopback_bind_host(host: str) -> bool:
    value = _normalize_bind_host(host)
    address = _ip_address(value)
    if address is None:
        return False
    if address.is_loopback:
        return True
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(mapped and mapped.is_loopback)


def _is_unspecified_bind_host(host: str) -> bool:
    address = _ip_address(host)
    return bool(address and address.is_unspecified)


def _container_runtime_detected() -> bool:
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def _validate_panel_startup(
    *,
    host: str,
    auth_mode: str,
    deployment_mode: str,
    containerized: bool | None = None,
) -> None:
    host = _normalize_bind_host(host)
    if deployment_mode == "host":
        if not _is_loopback_bind_host(host):
            raise ValueError(
                f"{auth_mode} 主机模式只允许绑定 IPv4/IPv6 loopback 地址"
            )
        return

    if deployment_mode == "docker_tunnel" and auth_mode != "cloudflare_access":
        raise ValueError("docker_tunnel 部署模式只允许 cloudflare_access 认证")
    if containerized is None:
        containerized = _container_runtime_detected()
    if not containerized:
        raise ValueError(f"{deployment_mode} 部署模式只能在容器内使用")
    if not _is_unspecified_bind_host(host):
        raise ValueError(
            f"{deployment_mode} 部署模式要求容器进程绑定 0.0.0.0 或 ::"
        )


def _panel_server_type(host: str) -> type[ThreadingHTTPServer]:
    address = _ip_address(host)
    if isinstance(address, ipaddress.IPv6Address):
        return _IPv6ThreadingHTTPServer
    return ThreadingHTTPServer


def _display_bind_host(host: str) -> str:
    address = _ip_address(host)
    if isinstance(address, ipaddress.IPv6Address):
        return f"[{host}]"
    return host


def _safe_segment(value: str) -> bool:
    return (
        bool(value)
        and value != "."
        and ".." not in value
        and "/" not in value
        and "\\" not in value
        and not any(ord(char) < 32 for char in value)
    )


def _allocation_path_segment(value: str) -> str | None:
    value = unquote(str(value or "")).strip()
    if (
        not value
        or "/" in value
        or "\\" in value
        or ".." in value
        or any(ord(char) < 32 for char in value)
    ):
        return None
    return value


def _allocation_uuid(value: str, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value).strip())
    except (AttributeError, ValueError, TypeError) as exc:
        raise AllocationPreviewDTOError("invalid_uuid", f"{field} 必须是合法 UUID", field=field) from exc


def _truthy_env(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def _truthy_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _valid_idempotency_key(value: str) -> bool:
    return bool(value) and len(value) <= 200 and not any(ord(ch) < 32 for ch in value)


def _strong_if_match(value: str | None, *, allow_wildcard: bool = False) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if allow_wildcard and text == "*":
        return text
    if text.startswith("W/") or len(text) < 2 or text[0] != '"' or text[-1] != '"' or "," in text:
        raise ValueError("If-Match 必须是 GET draft 返回的强 ETag")
    return text


@dataclass(frozen=True)
class ActiveTaskRevision:
    revision_id: str
    revision_number: int
    definition: dict[str, Any]
    rendered_task: str
    task_config: Any | None = None


class ActiveTaskLoader(Protocol):
    def load_active_revision(self, workspace_slug: str, task_key: str) -> ActiveTaskRevision | None: ...


class ActiveTaskLoaderUnavailable(RuntimeError):
    pass


class _PanelRouteError(RuntimeError):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers


def _external_identity(principal) -> ExternalIdentity:
    return ExternalIdentity(
        issuer=principal.identity.issuer,
        subject=principal.identity.subject,
        email_snapshot=principal.identity.email,
        display_name_snapshot=principal.identity.display_name,
    )


def _annotator_uuid(value: Any, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value).strip())
    except (AttributeError, TypeError, ValueError) as exc:
        raise PanelAnnotatorError(
            "invalid_uuid",
            f"{field} 必须是合法 UUID",
            HTTPStatus.UNPROCESSABLE_ENTITY,
            field=field,
        ) from exc


def _annotator_enum_value(value: Any) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value", value) or "").strip().lower() or None


def _map_annotator_dependency_error(exc: BaseException) -> PanelAnnotatorError:
    if isinstance(exc, PanelAnnotatorError):
        return exc
    if isinstance(exc, AuthorizationDenied):
        reason = _annotator_enum_value(getattr(getattr(exc, "decision", None), "reason", None))
        if reason in {
            AuthorizationReason.UNKNOWN_PRINCIPAL.value,
            AuthorizationReason.INACTIVE_PRINCIPAL.value,
            AuthorizationReason.RESOURCE_NOT_VISIBLE.value,
            AuthorizationReason.INACTIVE_WORKSPACE.value,
        }:
            return PanelAnnotatorError(
                "resource_not_found", "资源不存在", HTTPStatus.NOT_FOUND
            )
        if reason in {
            AuthorizationReason.ROLE_DENIED.value,
            AuthorizationReason.INVALID_AUDIT_CONTEXT.value,
        }:
            return PanelAnnotatorError(
                "permission_denied", "权限不足", HTTPStatus.FORBIDDEN
            )
        return PanelAnnotatorError(
            "authorization_unavailable",
            "数据库授权暂时不可用",
            HTTPStatus.SERVICE_UNAVAILABLE,
        )
    if isinstance(exc, AuthorizationUnavailable):
        return PanelAnnotatorError(
            "authorization_unavailable",
            "数据库授权暂时不可用",
            HTTPStatus.SERVICE_UNAVAILABLE,
        )
    if isinstance(exc, AnnotatorResourceNotFound):
        return PanelAnnotatorError(
            "resource_not_found", "资源不存在", HTTPStatus.NOT_FOUND
        )
    if isinstance(exc, AnnotatorVerificationRejected):
        reason = str(exc.reason or "verification_rejected").strip().lower()
        if "role" in reason:
            code = "role_error"
            message = "Argilla 用户角色校验失败"
        elif "membership" in reason:
            code = "membership_missing"
            message = "Argilla 工作区成员关系缺失"
        elif "mismatch" in reason or "already_mapped" in reason:
            code = "identity_drift"
            message = "Argilla 用户身份校验失败"
        else:
            code = "verification_rejected"
            message = "标注人员验证未通过"
        return PanelAnnotatorError(code, message, HTTPStatus.CONFLICT)
    if isinstance(exc, AnnotatorNaturalKeyConflict):
        return PanelAnnotatorError(
            "resource_conflict", "资源状态冲突", HTTPStatus.CONFLICT
        )
    if isinstance(exc, IdempotencyConflict):
        return PanelAnnotatorError(
            "idempotency_conflict", "请求与现有幂等操作冲突", HTTPStatus.CONFLICT
        )
    if isinstance(exc, AnnotatorOperationInProgress):
        return PanelAnnotatorError(
            "operation_in_progress",
            "操作正在处理中，请稍后重试",
            HTTPStatus.CONFLICT,
        )
    if isinstance(exc, AnnotatorNotReady):
        return PanelAnnotatorError(
            "annotator_not_ready", "标注人员控制资源尚未就绪", HTTPStatus.CONFLICT
        )
    if isinstance(exc, AnnotatorRepositoryValidationError) or isinstance(exc, ValueError):
        return PanelAnnotatorError(
            "invalid_request", "请求定义无效", HTTPStatus.UNPROCESSABLE_ENTITY
        )
    if isinstance(exc, AnnotatorRepositoryError):
        return PanelAnnotatorError(
            "repository_unavailable",
            "标注人员资料库暂时不可用",
            HTTPStatus.SERVICE_UNAVAILABLE,
        )
    return PanelAnnotatorError(
        "service_unavailable",
        "标注人员服务暂时不可用",
        HTTPStatus.SERVICE_UNAVAILABLE,
    )


class _PanelAnnotatorAuthorizer:
    def __init__(self, service: DatabaseService):
        self.service = service

    def require_workspace(self, *, workspace: str, actor: Any, permission: str) -> Any:
        if permission != WORKSPACE_MANAGE_PERMISSION or not isinstance(actor, ExternalIdentity):
            raise PanelAnnotatorError("permission_denied", "权限不足", HTTPStatus.FORBIDDEN)
        try:
            return self.service.require_workspace(
                actor,
                workspace,
                Permission.WORKSPACE_MANAGE,
            )
        except Exception as exc:
            raise _map_annotator_dependency_error(exc) from None


class _PanelAnnotatorRepositoryFacade:
    def __init__(
        self,
        repository: AnnotatorControlRepository,
        *,
        actor_identity: ExternalIdentity,
        caller_identity: ExternalIdentity,
        channel: AuditChannel,
        idempotency_key: str = "",
        request_id: str | None = None,
    ) -> None:
        self.repository = repository
        self.actor_identity = actor_identity
        self.caller_identity = caller_identity
        self.channel = channel
        self.idempotency_key = idempotency_key
        self.request_id = request_id
        self.default_capacity = 1
        self.expected_revision: int | None = None
        self.pending_verification_snapshot: ExternalAnnotatorSnapshot | None = None
        self.last_status = HTTPStatus.OK
        self.last_replayed = False

    def _call(self, method_name: str, **kwargs: Any) -> Any:
        method = getattr(self.repository, method_name, None)
        if not callable(method):
            raise PanelAnnotatorError(
                "repository_unavailable",
                "标注人员资料库暂时不可用",
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
        try:
            return method(**kwargs)
        except Exception as exc:
            raise _map_annotator_dependency_error(exc) from None

    def _binding(self, workspace: str) -> Any:
        bindings = self._call(
            "list_workspace_bindings",
            workspace_slug=workspace,
            identity=self.actor_identity,
        )
        if len(bindings) != 1:
            raise PanelAnnotatorError(
                "repository_unavailable",
                "Argilla workspace binding 尚未唯一确定",
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
        return bindings[0]

    @staticmethod
    def _mapping_record(
        mapping: Any,
        workspace: str,
        *,
        role: str | None = None,
        membership_verified: bool | None = None,
    ) -> dict[str, Any]:
        verification = _annotator_enum_value(getattr(mapping, "verification_state", None))
        if verification not in {item.value for item in VerificationStatus}:
            verification = VerificationStatus.UNVERIFIED.value
        role = role or _annotator_enum_value(getattr(mapping, "argilla_role", None)) or "annotator"
        if membership_verified is None:
            membership_verified = bool(
                getattr(mapping, "membership_verified", None)
                if getattr(mapping, "membership_verified", None) is not None
                else verification == VerificationStatus.VERIFIED.value
            )
        scaffold_user_id = getattr(mapping, "principal_subject", None)
        if scaffold_user_id is None:
            scaffold_user_id = getattr(mapping, "principal_id", None)
        return {
            "workspace": workspace,
            "annotator_id": str(mapping.id),
            "scaffold_user_id": str(scaffold_user_id),
            "argilla_user_id": (
                str(mapping.argilla_user_id)
                if mapping.argilla_user_id is not None
                else None
            ),
            "argilla_username": str(mapping.username),
            "argilla_role": role,
            "personal_workspace_id": (
                str(mapping.personal_argilla_workspace_id)
                if mapping.personal_argilla_workspace_id is not None
                else None
            ),
            "membership": {
                "workspace_id": (
                    str(mapping.personal_argilla_workspace_id)
                    if mapping.personal_argilla_workspace_id is not None
                    else None
                ),
                "present": bool(membership_verified),
                "role": role,
            },
            "verification": {
                "status": verification,
                "last_verified_at": (
                    mapping.verified_at.isoformat()
                    if getattr(mapping, "verified_at", None) is not None
                    else None
                ),
            },
        }

    @classmethod
    def _cohort_record(cls, cohort: Any, workspace: str) -> dict[str, Any]:
        revision = getattr(cohort, "latest_revision", None)
        members = tuple(getattr(revision, "members", ()) or ())
        capacities = [int(member.default_capacity) for member in members]
        return {
            "workspace": workspace,
            "cohort_id": str(cohort.id),
            "name": str(cohort.name),
            "default_capacity": capacities[0] if capacities else 1,
            "member_annotator_ids": [str(member.mapping.id) for member in members],
            "member_capacities": capacities,
            "revision": (
                int(revision.revision_number) if revision is not None else None
            ),
        }

    def list_annotators(self, *, workspace: str) -> list[dict[str, Any]]:
        mappings = self._call(
            "list_annotators",
            workspace_slug=workspace,
            identity=self.actor_identity,
        )
        return [self._mapping_record(item, workspace) for item in mappings]

    def get_annotator(self, *, workspace: str, annotator_id: str) -> dict[str, Any] | None:
        mapping = self._call(
            "get_annotator",
            workspace_slug=workspace,
            identity=self.actor_identity,
            mapping_id=_annotator_uuid(annotator_id, "annotator_id"),
        )
        return self._mapping_record(mapping, workspace) if mapping is not None else None

    def create_annotator(
        self,
        *,
        workspace: str,
        scaffold_user_id: str,
        external: ExternalAnnotatorSnapshot,
    ) -> dict[str, Any]:
        user_id = _annotator_uuid(external.argilla_user_id, "argilla_user_id")
        personal_workspace_id = (
            _annotator_uuid(external.personal_workspace_id, "personal_workspace_id")
            if external.personal_workspace_id is not None
            else None
        )
        membership_verified = bool(external.membership_present and personal_workspace_id is not None)
        result = self._call(
            "create_annotator_mapping",
            workspace_slug=workspace,
            binding_id=self._binding(workspace).id,
            principal_identity=ExternalIdentity(
                self.actor_identity.issuer,
                scaffold_user_id,
            ),
            username=external.argilla_username,
            actor_identity=self.actor_identity,
            caller_identity=self.caller_identity,
            idempotency_key=self.idempotency_key,
            argilla_user_id=user_id,
            personal_argilla_workspace_id=personal_workspace_id,
            argilla_role=external.role,
            membership_verified=membership_verified,
            channel=self.channel,
            request_id=self.request_id,
        )
        self.last_status = int(result.response_status)
        self.last_replayed = bool(result.replayed)
        return self._mapping_record(
            result.mapping,
            workspace,
            role=external.role,
            membership_verified=membership_verified,
        )

    def update_annotator_verification(
        self,
        *,
        workspace: str,
        annotator_id: str,
        verification: VerificationState,
    ) -> dict[str, Any] | None:
        snapshot = self.pending_verification_snapshot
        self.pending_verification_snapshot = None
        if snapshot is None:
            raise PanelAnnotatorError(
                "external_service_unavailable",
                "外部标注服务暂时不可用",
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
        result = self._call(
            "verify_annotator",
            workspace_slug=workspace,
            mapping_id=_annotator_uuid(annotator_id, "annotator_id"),
            argilla_user_id=_annotator_uuid(snapshot.argilla_user_id, "argilla_user_id"),
            username=snapshot.argilla_username,
            personal_argilla_workspace_id=(
                _annotator_uuid(snapshot.personal_workspace_id, "personal_workspace_id")
                if snapshot.personal_workspace_id is not None
                else None
            ),
            membership_verified=bool(snapshot.membership_present),
            argilla_role=snapshot.role,
            actor_identity=self.actor_identity,
            caller_identity=self.caller_identity,
            idempotency_key=self.idempotency_key,
            channel=self.channel,
            request_id=self.request_id,
        )
        self.last_status = int(result.response_status)
        self.last_replayed = bool(result.replayed)
        return self._mapping_record(
            result.mapping,
            workspace,
            role=snapshot.role,
            membership_verified=snapshot.membership_present,
        )

    def list_cohorts(self, *, workspace: str) -> list[dict[str, Any]]:
        cohorts = self._call(
            "list_cohorts",
            workspace_slug=workspace,
            identity=self.actor_identity,
        )
        return [self._cohort_record(item, workspace) for item in cohorts]

    def get_cohort(self, *, workspace: str, cohort_id: str) -> dict[str, Any] | None:
        cohort = self._call(
            "get_cohort",
            workspace_slug=workspace,
            identity=self.actor_identity,
            cohort_id=_annotator_uuid(cohort_id, "cohort_id"),
        )
        return self._cohort_record(cohort, workspace) if cohort is not None else None

    def create_cohort(
        self,
        *,
        workspace: str,
        name: str,
        default_capacity: int,
        member_annotator_ids: list[str] | tuple[str, ...],
    ) -> dict[str, Any]:
        members = tuple(
            CohortMemberInput(_annotator_uuid(item, "member_annotator_ids"), default_capacity)
            for item in member_annotator_ids
        )
        result = self._call(
            "create_cohort",
            workspace_slug=workspace,
            name=name,
            binding_id=self._binding(workspace).id,
            members=members,
            actor_identity=self.actor_identity,
            caller_identity=self.caller_identity,
            idempotency_key=self.idempotency_key,
            channel=self.channel,
            request_id=self.request_id,
        )
        self.last_status = int(result.response_status)
        self.last_replayed = bool(result.replayed)
        return self._cohort_record(result.cohort, workspace)

    def replace_cohort_members(
        self,
        *,
        workspace: str,
        cohort_id: str,
        member_annotator_ids: list[str] | tuple[str, ...],
        default_capacity: int | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any] | None:
        normalized_cohort_id = _annotator_uuid(cohort_id, "cohort_id")
        if expected_revision is not None:
            revisions = self._call(
                "list_cohort_revisions",
                workspace_slug=workspace,
                identity=self.actor_identity,
                cohort_id=normalized_cohort_id,
            )
            latest = max(
                (int(item.revision_number) for item in revisions),
                default=0,
            )
            if latest != expected_revision:
                raise PanelAnnotatorError(
                    "revision_conflict",
                    "人员组 revision 已变化，请重新读取",
                    HTTPStatus.CONFLICT,
                )
        cohort = self._call(
            "get_cohort",
            workspace_slug=workspace,
            identity=self.actor_identity,
            cohort_id=normalized_cohort_id,
        )
        if cohort is None:
            raise PanelAnnotatorError("resource_not_found", "资源不存在", HTTPStatus.NOT_FOUND)
        existing_capacities = {
            str(member.mapping.id): int(member.default_capacity)
            for member in tuple(getattr(getattr(cohort, "latest_revision", None), "members", ()) or ())
        }
        fallback_capacity = (
            default_capacity
            if default_capacity is not None
            else next(iter(existing_capacities.values()), self.default_capacity)
        )
        members = tuple(
            CohortMemberInput(
                mapping_id := _annotator_uuid(item, "member_annotator_ids"),
                default_capacity
                if default_capacity is not None
                else existing_capacities.get(str(mapping_id), fallback_capacity),
            )
            for item in member_annotator_ids
        )
        result = self._call(
            "create_cohort_revision",
            workspace_slug=workspace,
            cohort_id=normalized_cohort_id,
            binding_id=self._binding(workspace).id,
            members=members,
            actor_identity=self.actor_identity,
            caller_identity=self.caller_identity,
            idempotency_key=self.idempotency_key,
            channel=self.channel,
            request_id=self.request_id,
        )
        self.last_status = int(result.response_status)
        self.last_replayed = bool(result.replayed)
        cohort = self._call(
            "get_cohort",
            workspace_slug=workspace,
            identity=self.actor_identity,
            cohort_id=normalized_cohort_id,
        )
        return self._cohort_record(cohort, workspace) if cohort is not None else None

    def update_cohort(
        self,
        *,
        workspace: str,
        cohort_id: str,
        name: str | None,
        default_capacity: int | None,
        expected_revision: int | None,
    ) -> dict[str, Any] | None:
        result = self._call(
            "update_cohort",
            workspace_slug=workspace,
            cohort_id=_annotator_uuid(cohort_id, "cohort_id"),
            name=name,
            default_capacity=default_capacity,
            expected_revision=expected_revision,
            actor_identity=self.actor_identity,
            caller_identity=self.caller_identity,
            idempotency_key=self.idempotency_key,
            channel=self.channel,
            request_id=self.request_id,
        )
        self.last_status = int(result.response_status)
        self.last_replayed = bool(result.replayed)
        return self._cohort_record(result.cohort, workspace)


class _PanelArgillaAdminFacade:
    def __init__(
        self,
        adapter: ArgillaAdminAdapter,
        *,
        principal_issuer: str,
        repository: _PanelAnnotatorRepositoryFacade | None = None,
    ) -> None:
        self.adapter = adapter
        self.principal_issuer = principal_issuer
        self.repository = repository

    def _snapshot(
        self,
        result: Any,
        *,
        personal_workspace_id: str | None,
        membership_present: bool,
    ) -> ExternalAnnotatorSnapshot:
        if isinstance(result, ExternalAnnotatorSnapshot):
            source = result.to_safe_dict()
        elif isinstance(result, dict):
            source = {
                "argilla_user_id": result.get("argilla_user_id", result.get("uuid")),
                "argilla_username": result.get("argilla_username", result.get("username")),
                "role": result.get("role", result.get("argilla_role")),
            }
        else:
            source = {
                "argilla_user_id": getattr(result, "uuid", getattr(result, "argilla_user_id", None)),
                "argilla_username": getattr(result, "username", getattr(result, "argilla_username", None)),
                "role": getattr(result, "role", getattr(result, "argilla_role", None)),
            }
        source.update(
            {
                "personal_workspace_id": personal_workspace_id,
                "membership_present": membership_present,
                "membership_role": "annotator",
                "is_owner": str(source.get("role") or "").strip().lower() == "owner",
            }
        )
        snapshot = ExternalAnnotatorSnapshot.from_source(source)
        _annotator_uuid(snapshot.argilla_user_id, "argilla_user_id")
        if snapshot.personal_workspace_id is not None:
            _annotator_uuid(snapshot.personal_workspace_id, "personal_workspace_id")
        return snapshot

    def _ensure(
        self,
        *,
        scaffold_user_id: str,
        password: str | None,
        personal_workspace_name: str | None,
        expected_user_uuid: str | None = None,
        expected_workspace_uuid: str | None = None,
        membership_present: bool = True,
    ) -> ExternalAnnotatorSnapshot:
        if personal_workspace_name is not None:
            expected_name = derive_personal_workspace_name(
                self.principal_issuer,
                scaffold_user_id,
            )
            if personal_workspace_name != expected_name:
                raise PanelAnnotatorError(
                    "invalid_personal_workspace_name",
                    "personal workspace 名称必须由稳定 Scaffold 身份派生",
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    field="personal_workspace_name",
                )
        try:
            result = self.adapter.ensure_annotator(
                principal_issuer=self.principal_issuer,
                principal_subject=scaffold_user_id,
                password=password,
                expected_user_uuid=(
                    _annotator_uuid(expected_user_uuid, "argilla_user_id")
                    if expected_user_uuid is not None
                    else None
                ),
                expected_workspace_uuid=(
                    _annotator_uuid(expected_workspace_uuid, "personal_workspace_id")
                    if expected_workspace_uuid is not None
                    else None
                ),
            )
        except PanelAnnotatorError:
            raise
        except ArgillaProvisioningError:
            raise PanelAnnotatorError(
                "external_service_unavailable",
                "外部标注服务暂时不可用",
                HTTPStatus.SERVICE_UNAVAILABLE,
            ) from None
        except Exception:
            raise PanelAnnotatorError(
                "external_service_unavailable",
                "外部标注服务暂时不可用",
                HTTPStatus.SERVICE_UNAVAILABLE,
            ) from None
        resolved_workspace_id = expected_workspace_uuid
        if resolved_workspace_id is None:
            if isinstance(result, ExternalAnnotatorSnapshot):
                resolved_workspace_id = result.personal_workspace_id
            elif isinstance(result, dict):
                resolved_workspace_id = result.get(
                    "personal_workspace_id",
                    result.get("personal_argilla_workspace_id"),
                )
            else:
                resolved_workspace_id = getattr(
                    result,
                    "personal_workspace_id",
                    getattr(result, "personal_argilla_workspace_id", None),
                )
        if resolved_workspace_id is None:
            workspace_lookup = getattr(self.adapter, "ensure_personal_workspace", None)
            if callable(workspace_lookup):
                try:
                    resolved_workspace_id = workspace_lookup(
                        principal_issuer=self.principal_issuer,
                        principal_subject=scaffold_user_id,
                    )
                except Exception:
                    raise PanelAnnotatorError(
                        "external_service_unavailable",
                        "外部标注服务暂时不可用",
                        HTTPStatus.SERVICE_UNAVAILABLE,
                    ) from None
        snapshot = self._snapshot(
            result,
            personal_workspace_id=resolved_workspace_id,
            membership_present=membership_present,
        )
        return snapshot

    def provision_annotator(
        self,
        *,
        workspace: str,
        scaffold_user_id: str,
        personal_workspace_name: str | None,
        initial_password: str,
    ) -> ExternalAnnotatorSnapshot:
        del workspace
        return self._ensure(
            scaffold_user_id=scaffold_user_id,
            password=initial_password,
            personal_workspace_name=personal_workspace_name,
        )

    def bind_annotator(
        self,
        *,
        workspace: str,
        scaffold_user_id: str,
        argilla_user_id: str,
        argilla_username: str | None,
        personal_workspace_id: str | None,
    ) -> ExternalAnnotatorSnapshot:
        del workspace, argilla_username
        return self._ensure(
            scaffold_user_id=scaffold_user_id,
            password=None,
            personal_workspace_name=None,
            expected_user_uuid=argilla_user_id,
            expected_workspace_uuid=personal_workspace_id,
        )

    def verify_annotator(self, *, annotator: AnnotatorLookup) -> ExternalAnnotatorSnapshot:
        if annotator.argilla_user_id is None or annotator.personal_workspace_id is None:
            raise PanelAnnotatorError(
                "identity_incomplete",
                "标注人员缺少可验证的 Argilla UUID",
                HTTPStatus.CONFLICT,
            )
        snapshot = self._ensure(
            scaffold_user_id=annotator.scaffold_user_id,
            password=None,
            personal_workspace_name=None,
            expected_user_uuid=annotator.argilla_user_id,
            expected_workspace_uuid=annotator.personal_workspace_id,
        )
        if self.repository is not None:
            self.repository.pending_verification_snapshot = snapshot
        return snapshot


def _workspace_access_payload(access) -> dict[str, Any]:
    return {
        "slug": access.workspace.slug,
        "name": access.workspace.name,
        "roles": [role.value for role in access.roles],
        "workspace_capabilities": [permission.value for permission in access.workspace_capabilities],
        "task_capabilities": [permission.value for permission in access.task_capabilities],
    }


def _database_migration_head() -> str:
    config = build_alembic_config("sqlite+pysqlite:///:memory:")
    heads = tuple(ScriptDirectory.from_config(config).get_heads())
    if len(heads) != 1:
        raise AuthorizationUnavailable("database migration graph is not at a single head")
    return heads[0]


def _build_authorization_service(database_url: str | None = None) -> DatabaseService:
    engine = create_database_engine(database_url)
    try:
        with engine.connect() as connection:
            revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
        if revision != _database_migration_head():
            raise AuthorizationUnavailable("database schema revision is not ready")
        return DatabaseService(create_session_factory(engine), engine)
    except Exception:
        engine.dispose()
        raise


def _mcp_writes_enabled() -> bool:
    return _truthy_env("LLS_MCP_ENABLE_WRITES")


def _allow_data_lake_overrides() -> bool:
    return panel_settings.allow_data_lake_overrides()


def _allow_manual_imports() -> bool:
    return panel_settings.allow_manual_imports()


def _task_source_mode() -> str:
    return panel_settings.task_source_mode()


def _r2_task_source_enabled() -> bool:
    return _task_source_mode() == "r2"


def _control_task_source_enabled() -> bool:
    return _task_source_mode() == "control"


def _authorization_runtime_ready(
    service: DatabaseService | None,
    active_task_loader: ActiveTaskLoader | None,
) -> bool:
    return service is not None and _control_task_source_enabled() and active_task_loader is not None


def _task_registry_sync_ttl_seconds() -> float:
    raw = os.environ.get("LLS_TASK_REGISTRY_SYNC_TTL_SECONDS")
    if raw is None:
        return _DEFAULT_TASK_REGISTRY_SYNC_TTL_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        return _DEFAULT_TASK_REGISTRY_SYNC_TTL_SECONDS


def _task_registry_sync_cache_key(tasks_root: str | Path, settings: dict) -> tuple[str, str, str, str]:
    return (
        str(Path(tasks_root).expanduser().resolve()),
        str(settings.get("task_registry_uri") or ""),
        str(settings.get("data_lake_r2_prefix") or ""),
        str(settings.get("rclone_config_path") or ""),
    )


def _invalidate_task_registry_sync_cache() -> None:
    with _TASK_REGISTRY_SYNC_CACHE_LOCK:
        _TASK_REGISTRY_SYNC_CACHE.clear()


def _apply_runtime_settings(runs_root: str | Path, settings: dict | None = None) -> dict:
    effective = settings or panel_settings.effective_settings(runs_root)
    from .data_lake import set_allowed_r2_prefix_override, set_default_registry_uri_override

    set_default_registry_uri_override(effective["task_registry_uri"])
    set_allowed_r2_prefix_override(effective["data_lake_r2_prefix"])
    return effective


def _settings_response(settings: dict, *, include_legacy: bool = False) -> dict:
    payload = {"settings": settings}
    if include_legacy:
        payload.update({
            "allow_data_lake_overrides": settings["allow_data_lake_overrides"],
            "task_source": settings["task_source"],
            "task_registry_uri": settings["task_registry_uri"] if settings["task_source"] == "r2" else None,
        })
    return payload


def _public_settings_response(settings: dict) -> dict:
    return {
        "settings": {
            "task_source": settings["task_source"],
            "allow_manual_imports": settings["allow_manual_imports"],
            "allow_data_lake_overrides": settings["allow_data_lake_overrides"],
            "task_registry_configured": bool(settings["task_registry_uri"]),
            "data_lake_r2_prefix_configured": bool(settings["data_lake_r2_prefix"]),
            "rclone_configured": bool(settings["rclone_config_path"]),
            "mcp_writes_enabled": _mcp_writes_enabled(),
        }
    }


def _annotator_contract_endpoints() -> list[dict[str, Any]]:
    workspace_query = {"workspace": {"type": "string", "required": True}}
    write_headers = {"Idempotency-Key": {"type": "string", "required": True}}
    return [
        {
            "method": "GET",
            "path": "/api/annotators",
            "action": "annotators_list",
            "side_effects": False,
            "required_permission": WORKSPACE_MANAGE_PERMISSION,
            "query_params": workspace_query,
            "response_schema": {"type": "object", "required": ["annotators", "workspace"]},
        },
        {
            "method": "GET",
            "path": "/api/annotators/{annotator_id}",
            "action": "annotator_detail",
            "side_effects": False,
            "required_permission": WORKSPACE_MANAGE_PERMISSION,
            "path_params": {"annotator_id": {"type": "string", "format": "uuid"}},
            "query_params": workspace_query,
            "response_schema": {"type": "object", "required": ["annotator"]},
        },
        {
            "method": "POST",
            "path": "/api/annotators/provision",
            "action": "annotator_provision",
            "side_effects": True,
            "required_permission": WORKSPACE_MANAGE_PERMISSION,
            "required_headers": write_headers,
            "request_schema": {"type": "object", "required": ["workspace", "scaffold_user_id", "initial_password"]},
            "response_schema": {"type": "object", "required": ["annotator", "replayed"]},
        },
        {
            "method": "POST",
            "path": "/api/annotators/bind",
            "action": "annotator_bind",
            "side_effects": True,
            "required_permission": WORKSPACE_MANAGE_PERMISSION,
            "required_headers": write_headers,
            "request_schema": {"type": "object", "required": ["workspace", "scaffold_user_id", "argilla_user_id"]},
            "response_schema": {"type": "object", "required": ["annotator", "replayed"]},
        },
        {
            "method": "POST",
            "path": "/api/annotators/{annotator_id}/verify",
            "action": "annotator_verify",
            "side_effects": True,
            "required_permission": WORKSPACE_MANAGE_PERMISSION,
            "required_headers": write_headers,
            "path_params": {"annotator_id": {"type": "string", "format": "uuid"}},
            "request_schema": {"type": "object", "required": ["workspace", "annotator_id"]},
            "response_schema": {"type": "object", "required": ["annotator", "replayed"]},
        },
        {
            "method": "GET",
            "path": "/api/cohorts",
            "action": "cohorts_list",
            "side_effects": False,
            "required_permission": WORKSPACE_MANAGE_PERMISSION,
            "query_params": workspace_query,
            "response_schema": {"type": "object", "required": ["cohorts", "workspace"]},
        },
        {
            "method": "GET",
            "path": "/api/cohorts/{cohort_id}",
            "action": "cohort_detail",
            "side_effects": False,
            "required_permission": WORKSPACE_MANAGE_PERMISSION,
            "path_params": {"cohort_id": {"type": "string", "format": "uuid"}},
            "query_params": workspace_query,
            "response_schema": {"type": "object", "required": ["cohort"]},
        },
        {
            "method": "POST",
            "path": "/api/cohorts",
            "action": "cohort_create",
            "side_effects": True,
            "required_permission": WORKSPACE_MANAGE_PERMISSION,
            "required_headers": write_headers,
            "request_schema": {"type": "object", "required": ["workspace", "name", "default_capacity", "member_annotator_ids"]},
            "response_schema": {"type": "object", "required": ["cohort", "replayed"]},
        },
        {
            "method": "PUT",
            "path": "/api/cohorts/{cohort_id}",
            "action": "cohort_revision_create",
            "side_effects": True,
            "required_permission": WORKSPACE_MANAGE_PERMISSION,
            "required_headers": write_headers,
            "path_params": {"cohort_id": {"type": "string", "format": "uuid"}},
            "request_schema": {"type": "object", "required": ["workspace", "cohort_id", "name", "default_capacity", "expected_revision"]},
            "response_schema": {"type": "object", "required": ["cohort", "replayed"]},
        },
        {
            "method": "PUT",
            "path": "/api/cohorts/{cohort_id}/members",
            "action": "cohort_revision_create",
            "side_effects": True,
            "required_permission": WORKSPACE_MANAGE_PERMISSION,
            "required_headers": write_headers,
            "path_params": {"cohort_id": {"type": "string", "format": "uuid"}},
            "request_schema": {"type": "object", "required": ["workspace", "cohort_id", "member_annotator_ids", "expected_revision"]},
            "response_schema": {"type": "object", "required": ["cohort", "replayed"]},
        },
        {
            "method": "POST",
            "path": "/api/cohorts/{cohort_id}/revisions",
            "action": "cohort_revision_create",
            "side_effects": True,
            "required_permission": WORKSPACE_MANAGE_PERMISSION,
            "required_headers": write_headers,
            "path_params": {"cohort_id": {"type": "string", "format": "uuid"}},
            "request_schema": {"type": "object", "required": ["workspace", "cohort_id", "member_annotator_ids", "expected_revision"]},
            "response_schema": {"type": "object", "required": ["cohort", "replayed"]},
        },
    ]


def _contract_capabilities(
    auth_mode: str = "unconfigured",
    authorization_state: str = AUTHORIZATION_UNAVAILABLE,
) -> dict[str, Any]:
    payload = {
        "service": "llm-labeling-scaffold",
        "api_contract_version": API_CONTRACT_VERSION,
        "auth": {
            "active_type": auth_mode,
            "types": ["cloudflare_access", "basic_dev", "mcp_service_bearer"],
            "multi_user": auth_mode == "cloudflare_access",
        },
        "authorization": {"state": authorization_state},
        "endpoints": [
            *_annotator_contract_endpoints(),
            {
                "method": "GET",
                "path": "/api/health",
                "action": "health",
                "side_effects": False,
                "response_schema": {
                    "type": "object",
                    "required": ["ok", "status", "service"],
                },
            },
            {
                "method": "GET",
                "path": "/api/version",
                "action": "version",
                "side_effects": False,
                "response_schema": {
                    "type": "object",
                    "required": ["service", "version", "api_contract_version"],
                },
            },
            {
                "method": "GET",
                "path": "/api/session",
                "action": "session",
                "side_effects": False,
                "response_schema": {
                    "type": "object",
                    "required": ["authenticated", "user", "authentication", "authorization"],
                    "properties": {
                        "authenticated": {"const": True},
                        "user": {
                            "type": "object",
                            "properties": {
                                "display_name": {"type": "string"},
                                "email": {"type": "string"},
                            },
                        },
                        "authorization": {
                            "type": "object",
                            "required": ["state"],
                            "properties": {"state": {"enum": [AUTHORIZATION_READY, AUTHORIZATION_UNAVAILABLE]}},
                        },
                    },
                },
            },
            {
                "method": "GET",
                "path": "/api/capabilities",
                "action": "capabilities",
                "side_effects": False,
                "response_schema": {"type": "object", "required": ["endpoints"]},
            },
            {
                "method": "GET",
                "path": "/api/settings/public",
                "action": "settings_public",
                "side_effects": False,
                "response_schema": {
                    "type": "object",
                    "required": ["settings"],
                    "properties": {
                        "settings": {
                            "type": "object",
                            "required": [
                                "task_source",
                                "allow_manual_imports",
                                "allow_data_lake_overrides",
                                "task_registry_configured",
                                "data_lake_r2_prefix_configured",
                                "rclone_configured",
                                "mcp_writes_enabled",
                            ],
                        }
                    },
                },
            },
            {
                "method": "GET",
                "path": "/api/data_lake/catalog",
                "action": "data_lake_catalog",
                "side_effects": False,
                "query_params": {
                    "dataset_id": {"type": "string", "required": False},
                },
                "response_schema": {
                    "type": "object",
                    "required": ["backend", "registry_uri", "datasets"],
                },
            },
            {
                "method": "GET",
                "path": "/api/tasks/{task_id}",
                "action": "task_detail",
                "side_effects": False,
                "path_params": {"task_id": {"type": "string"}},
                "query_params": {"workspace": {"type": "string", "required": True}},
                "response_schema": {
                    "type": "object",
                    "required": ["task"],
                    "properties": {"task": {"type": "object", "required": ["task_id", "profile", "data_lake"]}},
                },
            },
            {
                "method": "GET",
                "path": "/api/tasks",
                "action": "tasks_list",
                "side_effects": False,
                "query_params": {"workspace": {"type": "string", "required": True}},
                "response_schema": {"type": "object", "required": ["tasks"]},
            },
            {
                "method": "GET",
                "path": "/api/task/control",
                "action": "task_control_detail",
                "side_effects": False,
                "requires_task_source": "control",
                "query_params": {
                    "task_id": {"type": "string", "required": True},
                    "workspace": {"type": "string", "required": True},
                },
                "response_schema": {"type": "object", "required": ["task"]},
            },
            {
                "method": "POST",
                "path": "/api/tasks",
                "action": "task_control_create",
                "side_effects": True,
                "requires_task_source": "control",
                "request_schema": {
                    "type": "object",
                    "required": ["task_id", "text_fields", "primary_label_values", "workspace"],
                    "properties": {
                        "workspace": {"type": "string"},
                        "idempotency_key": {
                            "type": "string",
                            "description": "可选；省略时由任务定义生成稳定幂等键",
                        },
                    },
                },
                "response_schema": {"type": "object", "required": ["ok", "task", "record", "action"]},
            },
            {
                "method": "PUT",
                "path": "/api/tasks/{task_id}",
                "action": "task_control_update_draft",
                "side_effects": True,
                "requires_task_source": "control",
                "path_params": {"task_id": {"type": "string"}},
                "required_headers": {"If-Match": {"type": "string", "format": "strong-etag"}},
                "request_schema": {
                    "type": "object",
                    "required": ["task_id", "text_fields", "primary_label_values", "workspace"],
                    "properties": {"workspace": {"type": "string"}},
                },
                "response_schema": {"type": "object", "required": ["ok", "task"]},
            },
            {
                "method": "POST",
                "path": "/api/tasks/{task_id}/publish",
                "action": "task_control_publish",
                "side_effects": True,
                "requires_task_source": "control",
                "path_params": {"task_id": {"type": "string"}},
                "request_schema": {
                    "type": "object",
                    "required": ["confirm", "idempotency_key", "reason", "workspace"],
                    "properties": {
                        "confirm": {"const": True},
                        "idempotency_key": {"type": "string"},
                        "reason": {"type": "string"},
                        "workspace": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
                "required_headers": {"If-Match": {"type": "string", "format": "strong-etag"}},
                "response_schema": {"type": "object", "required": ["ok", "task", "published"]},
            },
            *[
                {
                    "method": "POST",
                    "path": f"/api/tasks/{{task_id}}/{action}",
                    "action": f"task_{action}",
                    "side_effects": True,
                    "requires_task_source": "control",
                    "path_params": {"task_id": {"type": "string"}},
                    "request_schema": {
                        "type": "object",
                        "required": ["idempotency_key", "reason", "workspace"],
                        "properties": {
                            "idempotency_key": {"type": "string"},
                            "reason": {"type": "string"},
                            "workspace": {"type": "string"},
                        },
                    },
                    "response_schema": {"type": "object", "required": ["ok", "task", "replayed"]},
                }
                for action in ("disable", "archive", "restore")
            ],
            {
                "method": "POST",
                "path": "/api/tasks/{task_id}/check",
                "action": "task_check",
                "side_effects": False,
                "path_params": {"task_id": {"type": "string"}},
                "request_schema": {
                    "type": "object",
                    "required": ["workspace"],
                    "properties": {"workspace": {"type": "string"}},
                    "additionalProperties": False,
                },
                "response_schema": {
                    "type": "object",
                    "required": ["ok", "task_id", "checks", "warnings", "errors"],
                },
            },
            {
                "method": "GET",
                "path": "/api/task/imports",
                "action": "task_imports_list",
                "side_effects": False,
                "requires_task_source": "control",
                "query_params": {
                    "task_id": {"type": "string", "required": True},
                    "workspace": {"type": "string", "required": True},
                },
                "response_schema": {"type": "object", "required": ["workspace", "imports"]},
            },
            {
                "method": "GET",
                "path": "/api/import/detail",
                "action": "task_import_detail",
                "side_effects": False,
                "requires_task_source": "control",
                "query_params": {
                    "task_id": {"type": "string", "required": True},
                    "import_id": {"type": "string", "required": True},
                    "workspace": {"type": "string", "required": True},
                },
                "response_schema": {"type": "object", "required": ["workspace", "import"]},
            },
            {
                "method": "GET",
                "path": "/api/task/data_lake",
                "action": "task_data_lake_preview",
                "side_effects": False,
                "requires_task_source": "control",
                "query_params": {
                    "task_id": {"type": "string", "required": True},
                    "workspace": {"type": "string", "required": True},
                },
                "response_schema": {
                    "type": "object",
                    "required": ["workspace", "enabled", "data_lake", "preview"],
                },
            },
            {
                "method": "GET",
                "path": "/api/jobs",
                "action": "task_jobs_list",
                "side_effects": False,
                "requires_task_source": "control",
                "query_params": {
                    "task_id": {"type": "string", "required": True},
                    "workspace": {"type": "string", "required": True},
                },
                "response_schema": {"type": "object", "required": ["workspace", "jobs"]},
            },
            {
                "method": "GET",
                "path": "/api/task/annotation_jobs",
                "action": "annotation_jobs_list",
                "side_effects": False,
                "query_params": {"task_id": {"type": "string", "required": True}},
                "response_schema": {"type": "object", "required": ["annotation_jobs"]},
            },
            {
                "method": "GET",
                "path": "/api/annotation_job/detail",
                "action": "annotation_job_detail",
                "side_effects": False,
                "query_params": {
                    "task_id": {"type": "string", "required": True},
                    "annotation_id": {"type": "string", "required": True},
                },
                "response_schema": {"type": "object", "required": ["annotation_job"]},
            },
            {
                "method": "GET",
                "path": "/api/task/decision_artifacts",
                "action": "decision_artifacts_list",
                "side_effects": False,
                "query_params": {"task_id": {"type": "string", "required": True}},
                "response_schema": {"type": "object", "required": ["decision_artifacts"]},
            },
            {
                "method": "GET",
                "path": "/api/decision_artifact/detail",
                "action": "decision_artifact_detail",
                "side_effects": False,
                "query_params": {
                    "task_id": {"type": "string", "required": True},
                    "decision_id": {"type": "string", "required": True},
                },
                "response_schema": {"type": "object", "required": ["decision_artifact"]},
            },
            {
                "method": "GET",
                "path": "/api/task/gold_versions",
                "action": "gold_versions_list",
                "side_effects": False,
                "query_params": {"task_id": {"type": "string", "required": True}},
                "response_schema": {"type": "object", "required": ["gold_versions"]},
            },
            {
                "method": "GET",
                "path": "/api/gold_version/detail",
                "action": "gold_version_detail",
                "side_effects": False,
                "query_params": {
                    "task_id": {"type": "string", "required": True},
                    "version": {"type": "string", "required": True},
                },
                "response_schema": {"type": "object", "required": ["gold_version"]},
            },
            {
                "method": "POST",
                "path": "/api/action",
                "action": "operator_action",
                "side_effects": True,
                "operator_gated": True,
                "allowed_actions": [
                    "argilla_push",
                    "argilla_pull",
                    "gold",
                    "prelabel_export",
                    "prelabel_publish",
                ],
                "request_schema": {
                    "type": "object",
                    "required": ["task", "action"],
                    "properties": {
                        "task": {"type": "string"},
                        "action": {
                            "type": "string",
                            "enum": [
                                "argilla_push",
                                "argilla_pull",
                                "gold",
                                "prelabel_export",
                                "prelabel_publish",
                            ],
                        },
                        "params": {"type": "object"},
                    },
                },
            },
            {
                "method": "POST",
                "path": "/api/allocation/preview",
                "action": "allocation_preview",
                "side_effects": False,
                "requires_task_source": "control",
                "required_permission": Permission.ANNOTATION_REVIEW.value,
                "request_schema": {
                    "type": "object",
                    "required": ["scope", "request"],
                    "properties": {
                        "scope": {
                            "type": "object",
                            "required": ["workspace", "task_id", "revision_id", "revision_hash"],
                        },
                        "request": {"type": "object"},
                    },
                    "additionalProperties": False,
                },
                "response_schema": {
                    "type": "object",
                    "required": ["schema_version", "scope", "ready", "blocking_errors", "warnings"],
                },
            },
            {
                "method": "POST",
                "path": "/api/allocation/plans",
                "action": "allocation_plan_create",
                "side_effects": True,
                "requires_task_source": "control",
                "required_permission": Permission.ANNOTATION_REVIEW.value,
                "required_headers": {"Idempotency-Key": {"type": "string"}},
                "request_schema": {
                    "type": "object",
                    "required": ["scope", "request"],
                    "properties": {
                        "scope": {
                            "type": "object",
                            "required": [
                                "workspace",
                                "task_id",
                                "revision_id",
                                "revision_hash",
                                "cohort_revision_id",
                            ],
                        },
                        "request": {"type": "object"},
                        "idempotency_key": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
                "response_schema": {
                    "type": "object",
                    "required": ["kind", "plan_id", "fingerprint", "lifecycle_state", "replayed"],
                },
            },
            {
                "method": "POST",
                "path": "/api/allocation/plans/{plan_id}/confirm",
                "action": "allocation_plan_confirm",
                "side_effects": True,
                "requires_task_source": "control",
                "required_permission": Permission.ANNOTATION_REVIEW.value,
                "path_params": {"plan_id": {"type": "string", "format": "uuid"}},
                "required_headers": {"Idempotency-Key": {"type": "string"}},
                "request_schema": {
                    "type": "object",
                    "required": ["plan_fingerprint"],
                    "properties": {
                        "workspace": {"type": "string"},
                        "plan_fingerprint": {"type": "string", "format": "sha256"},
                        "idempotency_key": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
                "response_schema": {
                    "type": "object",
                    "required": ["kind", "plan_id", "lifecycle_state", "replayed"],
                },
            },
            {
                "method": "GET",
                "path": "/api/allocation/plans",
                "action": "allocation_plan_list",
                "side_effects": False,
                "requires_task_source": "control",
                "required_permission": Permission.ANNOTATION_REVIEW.value,
                "query_params": {
                    "workspace": {"type": "string", "required": True},
                    "task_id": {"type": "string", "required": False},
                    "limit": {"type": "integer", "required": False},
                    "after": {"type": "string", "required": False},
                },
                "response_schema": {"type": "object", "required": ["workspace", "plans"]},
            },
            {
                "method": "GET",
                "path": "/api/allocation/plans/{plan_id}",
                "action": "allocation_plan_detail",
                "side_effects": False,
                "requires_task_source": "control",
                "required_permission": Permission.ANNOTATION_REVIEW.value,
                "path_params": {"plan_id": {"type": "string", "format": "uuid"}},
                "query_params": {"workspace": {"type": "string", "required": True}},
                "response_schema": {"type": "object", "required": ["plan"]},
            },
            {
                "method": "GET",
                "path": "/api/allocation/plans/{plan_id}/progress",
                "action": "allocation_plan_progress",
                "side_effects": False,
                "requires_task_source": "control",
                "required_permission": Permission.ANNOTATION_REVIEW.value,
                "path_params": {"plan_id": {"type": "string", "format": "uuid"}},
                "query_params": {"workspace": {"type": "string", "required": True}},
                "response_schema": {"type": "object", "required": ["kind", "plan_id", "progress"]},
            },
            {
                "method": "GET",
                "path": "/api/allocation/plans/{plan_id}/assignments",
                "action": "allocation_assignment_list",
                "side_effects": False,
                "requires_task_source": "control",
                "required_permission": Permission.ANNOTATION_REVIEW.value,
                "path_params": {"plan_id": {"type": "string", "format": "uuid"}},
                "query_params": {
                    "workspace": {"type": "string", "required": True},
                    "limit": {"type": "integer", "required": False},
                    "after": {"type": "string", "required": False},
                },
                "response_schema": {"type": "object", "required": ["workspace", "plan_id", "assignments"]},
            },
            {
                "method": "GET",
                "path": "/api/allocation/assignments/{assignment_id}",
                "action": "allocation_assignment_detail",
                "side_effects": False,
                "requires_task_source": "control",
                "required_permission": Permission.ANNOTATION_REVIEW.value,
                "path_params": {"assignment_id": {"type": "string", "format": "uuid"}},
                "query_params": {
                    "workspace": {"type": "string", "required": True},
                    "plan_id": {"type": "string", "required": False},
                },
                "response_schema": {"type": "object", "required": ["assignment"]},
            },
            {
                "method": "POST",
                "path": "/api/suggestions/import",
                "action": "suggestions_import",
                "side_effects": True,
                "operator_gated": True,
                "query_params": {
                    "task_id": {"type": "string", "required": True},
                    "annotation_id": {"type": "string", "required": True},
                    "suggestion_id": {"type": "string", "required": True},
                    "provider": {"type": "string", "required": False},
                    "prompt_version": {"type": "string", "required": False},
                    "publish": {"type": "boolean", "required": False},
                },
                "request_schema": {"content_type": "application/x-ndjson"},
                "response_schema": {"type": "object", "required": ["ok", "suggestions"]},
            },
            {
                "method": "GET",
                "path": "/api/suggestions/download",
                "action": "suggestions_download",
                "side_effects": False,
                "query_params": {
                    "task_id": {"type": "string", "required": True},
                    "annotation_id": {"type": "string", "required": True},
                    "suggestion_id": {"type": "string", "required": True},
                    "kind": {"type": "string", "required": False, "enum": ["template", "suggestions", "schema"]},
                },
            },
            {
                "method": "POST",
                "path": "/api/import/data_lake",
                "action": "data_lake_import_dry_run",
                "side_effects": False,
                "request_schema": {
                    "type": "object",
                    "required": ["task_id", "dry_run", "workspace"],
                    "properties": {
                        "task_id": {"type": "string"},
                        "import_id": {"type": "string"},
                        "dry_run": {"const": True},
                        "workspace": {"type": "string"},
                    },
                    "additionalProperties": True,
                },
                "response_schema": {
                    "type": "object",
                    "required": ["ok", "dry_run", "result"],
                    "properties": {
                        "ok": {"type": "boolean"},
                        "dry_run": {"const": True},
                        "result": {"type": "object"},
                    },
                },
            },
            {
                "method": "POST",
                "path": "/api/import/data_lake",
                "action": "data_lake_import_submit",
                "side_effects": True,
                "request_schema": {
                    "type": "object",
                    "required": ["task_id", "confirm", "idempotency_key", "workspace"],
                    "properties": {
                        "task_id": {"type": "string"},
                        "import_id": {"type": "string"},
                        "confirm": {"const": True},
                        "idempotency_key": {"type": "string"},
                        "workspace": {"type": "string"},
                    },
                    "additionalProperties": True,
                },
                "response_schema": {
                    "type": "object",
                    "required": ["ok", "job"],
                },
            },
        ],
    }
    if auth_mode == "cloudflare_access":
        public_routes = {
            ("GET", "/api/health"),
            ("GET", "/api/version"),
            ("GET", "/api/capabilities"),
            ("GET", "/api/settings/public"),
        }
        payload["endpoints"] = [
            endpoint
            for endpoint in payload["endpoints"]
            if (endpoint["method"], endpoint["path"]) in public_routes
            or _database_authorized_route(endpoint["method"], endpoint["path"])
        ]
    return payload


def _contract_task_path(path: str, *, suffix: str = "") -> str | None:
    prefix = "/api/tasks/"
    if not path.startswith(prefix):
        return None
    rest = path[len(prefix):]
    if suffix:
        marker = f"/{suffix}"
        if not rest.endswith(marker):
            return None
        rest = rest[: -len(marker)]
    elif "/" in rest:
        return None
    task_id = unquote(rest).strip()
    return task_id if _safe_segment(task_id) else None


def _contract_task_lifecycle_path(path: str) -> tuple[str, str] | None:
    for action in ("disable", "archive", "restore"):
        task_id = _contract_task_path(path, suffix=action)
        if task_id is not None:
            return task_id, action
    return None


def _allocation_plan_path(path: str, *, suffix: str | None = None) -> str | None:
    prefix = "/api/allocation/plans/"
    if not path.startswith(prefix):
        return None
    rest = path[len(prefix):]
    if suffix is not None:
        marker = f"/{suffix}"
        if not rest.endswith(marker):
            return None
        rest = rest[: -len(marker)]
    elif "/" in rest:
        return None
    return _allocation_path_segment(rest)


def _allocation_assignment_path(path: str) -> str | None:
    prefix = "/api/allocation/assignments/"
    if not path.startswith(prefix) or "/" in path[len(prefix):]:
        return None
    return _allocation_path_segment(path[len(prefix):])


def _allocation_route_allowed(method: str, path: str) -> bool:
    if method == "GET":
        return (
            path
            in {
                "/api/allocation/plans",
                "/api/allocation/plan/detail",
                "/api/allocation/assignments",
                "/api/allocation/assignment/detail",
            }
            or _allocation_plan_path(path) is not None
            or _allocation_plan_path(path, suffix="progress") is not None
            or _allocation_plan_path(path, suffix="assignments") is not None
            or _allocation_assignment_path(path) is not None
        )
    if method == "POST":
        return (
            path in {"/api/allocation/plans", "/api/allocation/plan"}
            or _allocation_plan_path(path, suffix="confirm") is not None
        )
    return False


def _panel_path_segment(value: str) -> str | None:
    value = unquote(str(value or "")).strip()
    if not _safe_segment(value) or "/" in value or "\\" in value:
        return None
    return value


def _annotator_route_kind(method: str, path: str) -> tuple[str, str | None] | None:
    if method == "GET":
        if path == "/api/annotators":
            return "annotators_list", None
        if path == "/api/cohorts":
            return "cohorts_list", None
        prefix = "/api/annotators/"
        if path.startswith(prefix):
            value = _panel_path_segment(path[len(prefix):])
            if value is not None and "/" not in value:
                return "annotator_detail", value
        prefix = "/api/cohorts/"
        if path.startswith(prefix):
            value = _panel_path_segment(path[len(prefix):])
            if value is not None and "/" not in value:
                return "cohort_detail", value
        return None
    if method == "POST":
        if path == "/api/annotators/provision":
            return "annotator_provision", None
        if path == "/api/annotators/bind":
            return "annotator_bind", None
        prefix = "/api/annotators/"
        if path.startswith(prefix) and path.endswith("/verify"):
            value = _panel_path_segment(path[len(prefix):-len("/verify")])
            if value is not None and "/" not in value:
                return "annotator_verify", value
        if path == "/api/cohorts":
            return "cohort_create", None
        prefix = "/api/cohorts/"
        if path.startswith(prefix) and path.endswith("/revisions"):
            value = _panel_path_segment(path[len(prefix):-len("/revisions")])
            if value is not None and "/" not in value:
                return "cohort_revision_create", value
        return None
    if method == "PUT":
        prefix = "/api/cohorts/"
        if not path.startswith(prefix):
            return None
        rest = path[len(prefix):]
        if rest.endswith("/members"):
            value = _panel_path_segment(rest[:-len("/members")])
            if value is not None and "/" not in value:
                return "cohort_members_replace", value
        value = _panel_path_segment(rest)
        if value is not None and "/" not in value:
            return "cohort_update", value
    return None


def _annotator_route_allowed(method: str, path: str) -> bool:
    return _annotator_route_kind(method, path) is not None


def _mcp_route_allowed(method: str, path: str) -> bool:
    if method == "GET":
        if path in {
            "/api/health",
            "/api/version",
            "/api/settings/public",
            "/api/tasks",
            "/api/task/control",
            "/api/task/imports",
            "/api/import/detail",
            "/api/task/data_lake",
            "/api/jobs",
            "/api/data_lake/catalog",
        }:
            return True
        if _allocation_route_allowed(method, path):
            return True
        return _contract_task_path(path) is not None
    if method == "POST":
        if path == "/api/allocation/preview":
            return True
        if _allocation_route_allowed(method, path):
            return _mcp_writes_enabled()
        if path == "/api/import/data_lake" or _contract_task_path(path, suffix="check") is not None:
            return True
        if not _mcp_writes_enabled():
            return False
        return path == "/api/tasks" or _contract_task_path(path, suffix="publish") is not None
    if method == "PUT" and _mcp_writes_enabled():
        return _contract_task_path(path) is not None
    return False


def _database_authorized_route(method: str, path: str) -> bool:
    if method == "GET" and path == "/api/session":
        return True
    if not _control_task_source_enabled():
        return False
    if _annotator_route_allowed(method, path):
        return True
    if method == "GET":
        if path in {
            "/api/tasks",
            "/api/task/control",
            "/api/task/imports",
            "/api/import/detail",
            "/api/task/data_lake",
            "/api/jobs",
            "/api/data_lake/catalog",
        }:
            return True
        if _allocation_route_allowed(method, path):
            return True
        return _contract_task_path(path) is not None
    if method == "POST":
        return (
            path in {"/api/tasks", "/api/import/data_lake", "/api/allocation/preview"}
            or _allocation_route_allowed(method, path)
            or _contract_task_path(path, suffix="publish") is not None
            or _contract_task_path(path, suffix="check") is not None
            or _contract_task_lifecycle_path(path) is not None
        )
    return method == "PUT" and _contract_task_path(path) is not None


def _cloudflare_route_allowed(method: str, path: str) -> bool:
    if not path.startswith("/api/"):
        return method == "GET"
    if method == "GET" and path in {
        "/api/health",
        "/api/version",
        "/api/capabilities",
        "/api/settings/public",
    }:
        return True
    return _database_authorized_route(method, path)


def _safe_data_lake_summary(data_lake: dict[str, Any]) -> dict[str, Any]:
    return {
        "configured": bool(data_lake),
        "source_dataset_id": data_lake.get("source_dataset_id"),
        "source_object_path": data_lake.get("source_object_path"),
        "lake_registry_uri_configured": bool(data_lake.get("lake_registry_uri")),
        "source_manifest_uri_configured": bool(data_lake.get("source_manifest_uri")),
        "output_base_uri_configured": bool(data_lake.get("output_base_uri")),
    }


def _data_lake_catalog_payload(*, detail_dataset_id: str | None = None) -> dict[str, Any]:
    from .data_lake import default_registry_uri, read_json_uri, read_yaml_uri

    registry_uri = default_registry_uri()
    registry = read_yaml_uri(registry_uri)
    datasets = registry.get("datasets")
    if not isinstance(datasets, dict):
        raise ValueError("数据湖登记表缺少 datasets")

    items: list[dict[str, Any]] = []
    for dataset_id, raw in sorted(datasets.items()):
        if not isinstance(raw, dict):
            continue
        item = {
            "dataset_id": str(dataset_id),
            "name": str(raw.get("name") or dataset_id),
            "description": str(raw.get("description") or ""),
            "layer": str(raw.get("layer") or ""),
            "domain": str(raw.get("domain") or ""),
            "status": str(raw.get("status") or "active"),
            "manifest_uri": str(raw.get("manifest") or ""),
            "canonical_uri": str(raw.get("canonical_uri") or ""),
            "asset_type": str(raw.get("asset_type") or ""),
        }
        if detail_dataset_id and str(dataset_id) == detail_dataset_id:
            manifest_uri = item["manifest_uri"]
            if not manifest_uri:
                raise ValueError(f"数据集缺少 manifest URI: {dataset_id}")
            manifest = read_json_uri(manifest_uri)
            objects = manifest.get("objects")
            if not isinstance(objects, list):
                raise ValueError(f"数据集 manifest 缺少 objects: {dataset_id}")
            item["manifest"] = {
                "dataset_id": str(manifest.get("dataset_id") or ""),
                "version": str(manifest.get("version") or manifest.get("revision") or ""),
                "created_at": str(manifest.get("created_at") or ""),
                "object_count": len(objects),
                "objects": [
                    {
                        "path": str(obj.get("path") or ""),
                        "storage_uri": str(obj.get("storage_uri") or ""),
                        "asset_type": str(obj.get("asset_type") or ""),
                        "rows": obj.get("rows"),
                        "bytes": obj.get("bytes"),
                        "sha256": str(obj.get("sha256") or obj.get("content_sha256") or ""),
                        "id_field": str(obj.get("id_field") or ""),
                    }
                    for obj in objects
                    if isinstance(obj, dict)
                ],
            }
        items.append(item)
    if detail_dataset_id and not any(item["dataset_id"] == detail_dataset_id for item in items):
        raise ValueError(f"数据湖登记表中没有数据集: {detail_dataset_id}")
    return {
        "backend": "r2",
        "registry_uri": registry_uri,
        "registry_version": str(registry.get("version") or registry.get("revision") or ""),
        "datasets": items,
    }


def _task_detail_payload(task) -> dict[str, Any]:
    try:
        from .profiles import profile_definition

        profile = profile_definition(task.profile)
        profile_summary = {
            "id": task.profile,
            "valid": True,
            "name": profile.get("name"),
            "stage_count": len(profile.get("stages", [])),
        }
    except Exception as exc:  # noqa: BLE001
        profile_summary = {"id": task.profile, "valid": False, "error": str(exc)}
    return {
        "task": {
            "task_id": task.task_id,
            "path": str(task.path),
            "profile": profile_summary,
            "id_field": task.id_field,
            "text_fields": task.text_fields,
            "metadata_fields": task.metadata_fields,
            "primary_label": task.primary_label,
            "auxiliary_labels": task.auxiliary_labels,
            "data_lake": _safe_data_lake_summary(task.data_lake),
        }
    }


def _active_task_raw(active: ActiveTaskRevision) -> dict[str, Any]:
    if not isinstance(active.definition, dict):
        raise ActiveTaskLoaderUnavailable("active revision definition is invalid")
    try:
        return pipeline.build_task_raw_from_spec(active.definition, revision=active.revision_number)
    except ValueError as exc:
        raise ActiveTaskLoaderUnavailable("active revision definition is invalid") from exc


def _active_task_summary(workspace_slug: str, access, active: ActiveTaskRevision | None) -> dict[str, Any]:
    lifecycle_state = access.task.lifecycle_state
    if not isinstance(lifecycle_state, TaskLifecycleState):
        lifecycle_state = TaskLifecycleState(lifecycle_state)
    summary = {
        "task_id": access.task.task_key,
        "workspace": workspace_slug,
        "source": "scaffold 控制面",
        "source_type": "control",
        "lifecycle_state": lifecycle_state.value,
        "deletable": False,
        "editable": Permission.TASK_EDIT in access.capabilities,
        "capabilities": [permission.value for permission in access.capabilities],
        "roles": [role.value for role in access.roles],
    }
    if lifecycle_state != TaskLifecycleState.ACTIVE:
        summary.update({"status": lifecycle_state.value, "active": False, "revision": 0})
        return summary
    summary["active"] = True
    draft_fingerprint = getattr(access.task, "draft_fingerprint", None)
    draft_version = getattr(access.task, "draft_version", None)
    if active is None:
        summary.update({"status": "draft" if draft_fingerprint else "unpublished", "revision": 0})
        if draft_fingerprint:
            summary.update({"draft_version": draft_version, "draft_fingerprint": draft_fingerprint})
        return summary
    raw = _active_task_raw(active)
    active_fingerprint = task_definition_fingerprint(active.definition, active.rendered_task)
    status = "published_with_draft" if draft_fingerprint and draft_fingerprint != active_fingerprint else "published"
    summary.update(
        {
            "status": status,
            "revision": active.revision_number,
            "revision_id": active.revision_id,
            "path": str(active.task_config.path) if active.task_config is not None else None,
            "profile": raw["profile"],
            "id_field": raw["id_field"],
            "data_lake": _safe_data_lake_summary(raw.get("data_lake") or {}),
        },
    )
    if status == "published_with_draft":
        summary.update({"draft_version": draft_version, "draft_fingerprint": draft_fingerprint})
    primary = raw.get("labels", {}).get("primary")
    if isinstance(primary, dict):
        summary["primary_label"] = primary
    return summary


def _active_task_detail(workspace_slug: str, task_key: str, active: ActiveTaskRevision) -> dict[str, Any]:
    raw = _active_task_raw(active)
    return {
        "task": {
            "task_id": task_key,
            "workspace": workspace_slug,
            "revision": active.revision_number,
            "revision_id": active.revision_id,
            "path": str(active.task_config.path) if active.task_config is not None else None,
            "profile": raw["profile"],
            "id_field": raw["id_field"],
            "text_fields": raw.get("input", {}).get("text_fields", []),
            "metadata_fields": raw.get("input", {}).get("metadata_fields", []),
            "primary_label": raw.get("labels", {}).get("primary"),
            "auxiliary_labels": raw.get("labels", {}).get("auxiliary", []),
            "data_lake": _safe_data_lake_summary(raw.get("data_lake", {})),
        },
    }


def _draft_response(task_key: str, draft, *, action: str | None = None) -> dict[str, Any]:
    record = {
        "task_id": task_key,
        "draft_spec": draft.definition,
        "draft_version": draft.version,
        "draft_fingerprint": draft.fingerprint,
        "draft_etag": draft.etag,
    }
    payload = {
        "task": {
            "task_id": task_key,
            "status": "draft",
            "source": "scaffold 控制面",
            "source_type": "control",
            "editable": True,
        },
        "record": record,
    }
    if action is not None:
        payload.update({"ok": True, "action": action})
    return payload


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def discover_runs(runs_root: Path) -> list[dict]:
    out: list[dict] = []
    if not runs_root.exists():
        return out
    for task_dir in sorted(p for p in runs_root.iterdir() if p.is_dir()):
        for run_dir in sorted(p for p in task_dir.iterdir() if p.is_dir()):
            audit = run_dir / "audit" / "run_summary.json"
            merged = run_dir / "merged" / "merge_summary.json"
            if not audit.exists() and not merged.exists():
                continue
            out.append({
                "task_id": task_dir.name,
                "run_id": run_dir.name,
                "path": str(run_dir),
                "audit": read_json(audit) if audit.exists() else None,
                "merge": read_json(merged) if merged.exists() else None,
            })
    return out


def run_detail(run_dir: Path) -> dict:
    audit_dir = run_dir / "audit"
    batches = []
    if audit_dir.is_dir():
        for sp in sorted(audit_dir.glob("batch_*.summary.json")):
            batches.append(read_json(sp))
    merge = run_dir / "merged" / "merge_summary.json"
    return {
        "batches": batches,
        "merge": read_json(merge) if merge.exists() else None,
        "pools": {k: _count_lines(run_dir / d / f) for k, (d, f) in POOL_FILES.items()},
        "has_decisions": (run_dir / "adjudication" / "decisions.jsonl").exists(),
    }


def pool_rows(run_dir: Path, kind: str, limit: int = 200) -> list[dict]:
    if kind not in POOL_FILES:
        return []
    d, f = POOL_FILES[kind]
    path = run_dir / d / f
    if not path.exists():
        return []
    return read_jsonl(path)[:limit]


def append_decision(run_dir: Path, decision: dict) -> int:
    out = run_dir / "adjudication" / "decisions.jsonl"
    existing = read_jsonl(out) if out.exists() else []
    existing.append(decision)
    write_jsonl(existing, out)
    return len(existing)


def parse_import_rows(text: str) -> list[dict]:
    stripped = text.strip()
    if not stripped:
        raise ValueError("上传内容为空")
    if stripped[0] in "[{":
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            return [data]
        if isinstance(data, list):
            if not all(isinstance(row, dict) for row in data):
                raise ValueError("JSON 数组中的每一项都必须是对象")
            if not data:
                raise ValueError("上传内容为空")
            return data

    rows: list[dict] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"第 {line_no} 行不是合法 JSON") from exc
        if not isinstance(row, dict):
            raise ValueError(f"第 {line_no} 行必须是 JSON 对象")
        rows.append(row)
    if not rows:
        raise ValueError("上传内容为空")
    return rows


def list_gold(runs_root: Path, task: str) -> list[dict]:
    gold_dir = runs_root / task / "gold"
    out = []
    if not gold_dir.exists():
        return out
    for mp in sorted(gold_dir.glob("gold_*.manifest.json")):
        out.append(read_json(mp))
    return out


# Backwards-compatible aliases used by tests.
_discover_runs = discover_runs


def _sample_rows(run_dir: Path, limit: int = 50) -> list[dict]:
    return pool_rows(run_dir, "merged", limit)


class _Handler(BaseHTTPRequestHandler):
    server_version = "LLSPanel/0.2"
    runs_root: Path = Path("runs")
    tasks_root: Path = Path("tasks")
    static_dir: Path | None = None
    authenticator: PanelAuthenticator | None = None
    authorization_service: DatabaseService | None = None
    allocation_repository: TrustedAllocationRepository | None = None
    annotator_repository: AnnotatorControlRepository | None = None
    annotator_adapter: ArgillaAdminAdapter | None = None
    authorization_ready: bool = False
    active_task_loader: ActiveTaskLoader | None = None

    def log_message(self, *args) -> None:
        pass

    def _authenticate(self) -> ActorContext:
        if self.authenticator is None:
            raise PanelAuthenticationError(
                "authentication_not_configured",
                "Panel 认证未配置",
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
        return self.authenticator.authenticate(self.headers)

    def _actor(self) -> str:
        context = getattr(self, "_request_context", None)
        return context.actor.audit_id if isinstance(context, ActorContext) else "system"

    def _is_mcp_request(self) -> bool:
        context = getattr(self, "_request_context", None)
        return isinstance(context, ActorContext) and context.caller.is_static_mcp_service

    def _require_auth(self) -> bool:
        try:
            context = self._authenticate()
        except PanelAuthenticationError as exc:
            headers = {}
            request_path = urlparse(self.path).path
            if (
                self.authenticator is not None
                and self.authenticator.challenge
                and not request_path.startswith("/api/")
            ):
                headers["WWW-Authenticate"] = self.authenticator.challenge
            self._json({"error": exc.message, "code": exc.code}, status=exc.status, headers=headers)
            return False
        path = urlparse(self.path).path
        if context.caller.is_static_mcp_service and not _mcp_route_allowed(self.command, path):
            self._json({"error": "MCP 服务身份无权访问该接口"}, status=HTTPStatus.FORBIDDEN)
            return False
        service_public_route = self.command == "GET" and path in {
            "/api/health",
            "/api/version",
            "/api/settings/public",
        }
        if context.actor.kind != "user" and not service_public_route:
            self._json(
                {"error": "MCP 请求缺少经过验证的用户 actor", "code": "delegated_actor_required"},
                status=HTTPStatus.FORBIDDEN,
            )
            return False
        if (
            context.actor.authentication_method == "cloudflare_access"
            and not _cloudflare_route_allowed(self.command, path)
        ):
            self._json(
                {"error": "该路由尚未接入数据库授权", "code": "route_authorization_unavailable"},
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return False
        self._request_context = context
        return True

    def _authorization_context(
        self,
    ) -> tuple[DatabaseService, ExternalIdentity, ExternalIdentity, AuditChannel]:
        context = getattr(self, "_request_context", None)
        if not isinstance(context, ActorContext) or context.actor.kind != "user":
            raise _PanelRouteError(
                HTTPStatus.FORBIDDEN,
                "delegated_actor_required",
                "请求缺少经过验证的用户 actor",
            )
        if context.caller != context.actor and not context.caller.is_static_mcp_service:
            raise _PanelRouteError(
                HTTPStatus.FORBIDDEN,
                "invalid_caller",
                "caller 不能代表该 actor 调用 Panel API",
            )
        service = self.authorization_service
        if not self.authorization_ready or service is None:
            raise _PanelRouteError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "authorization_unavailable",
                "数据库授权暂时不可用",
            )
        channel = AuditChannel.MCP if context.caller.is_static_mcp_service else AuditChannel.PANEL
        return service, _external_identity(context.actor), _external_identity(context.caller), channel

    def _annotator_workspace(self, params, body: dict[str, Any] | None = None) -> str:
        workspace = self._workspace_selector(params, body)
        if workspace is None:
            raise PanelAnnotatorError(
                "workspace_required",
                "标注人员管理必须显式提供 workspace",
                HTTPStatus.UNPROCESSABLE_ENTITY,
                field="workspace",
            )
        return workspace

    def _annotator_idempotency_key(self) -> str:
        value = str(self.headers.get("Idempotency-Key") or "").strip()
        if not _valid_idempotency_key(value):
            raise PanelAnnotatorError(
                "missing_idempotency_key",
                "写入请求必须提供有效的 Idempotency-Key header",
                HTTPStatus.UNPROCESSABLE_ENTITY,
                field="Idempotency-Key",
            )
        return value

    def _annotator_request_id(self) -> str | None:
        value = str(self.headers.get("X-Request-ID") or "").strip()
        if not value:
            return None
        if len(value) > 255 or any(ord(char) < 32 for char in value):
            raise PanelAnnotatorError(
                "invalid_request_id",
                "X-Request-ID 不是有效请求标识",
                HTTPStatus.UNPROCESSABLE_ENTITY,
                field="X-Request-ID",
            )
        return value

    def _annotator_runtime(
        self,
        *,
        idempotency_key: str = "",
        request_id: str | None = None,
        require_adapter: bool = False,
    ) -> tuple[PanelAnnotatorService, _PanelAnnotatorRepositoryFacade, ExternalIdentity]:
        if not _control_task_source_enabled():
            raise PanelAnnotatorError(
                "authorization_unavailable",
                "标注人员管理仅在 Scaffold control-plane 中可用",
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
        service, actor_identity, caller_identity, channel = self._authorization_context()
        repository = self.annotator_repository
        if repository is None:
            raise PanelAnnotatorError(
                "repository_unavailable",
                "标注人员资料库暂时不可用",
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
        repository_facade = _PanelAnnotatorRepositoryFacade(
            repository,
            actor_identity=actor_identity,
            caller_identity=caller_identity,
            channel=channel,
            idempotency_key=idempotency_key,
            request_id=request_id,
        )
        adapter_facade = None
        if require_adapter:
            adapter = self.annotator_adapter
            if adapter is None:
                raise PanelAnnotatorError(
                    "external_service_unavailable",
                    "外部标注服务暂时不可用",
                    HTTPStatus.SERVICE_UNAVAILABLE,
                )
            adapter_facade = _PanelArgillaAdminFacade(
                adapter,
                principal_issuer=actor_identity.issuer,
                repository=repository_facade,
            )
        return (
            PanelAnnotatorService(
                repository_facade,
                adapter_facade,
                _PanelAnnotatorAuthorizer(service),
            ),
            repository_facade,
            actor_identity,
        )

    @staticmethod
    def _annotator_body_id(
        body: dict[str, Any],
        *,
        field: str,
        path_value: str,
    ) -> None:
        body_value = body.get(field)
        if body_value is None:
            body[field] = path_value
            return
        if str(body_value).strip() != path_value:
            raise PanelAnnotatorError(
                "path_body_conflict",
                f"路径中的 {field} 与请求体不一致",
                HTTPStatus.UNPROCESSABLE_ENTITY,
                field=field,
            )

    @staticmethod
    def _annotator_dto_payload(value: Any) -> dict[str, Any]:
        if isinstance(value, (AnnotatorResponse,)):
            return value.to_dict()
        to_dict = getattr(value, "to_dict", None)
        if callable(to_dict):
            return to_dict()
        if isinstance(value, dict):
            return dict(value)
        raise PanelAnnotatorError(
            "service_unavailable",
            "标注人员服务暂时不可用",
            HTTPStatus.SERVICE_UNAVAILABLE,
        )

    def _annotator_list(self, params) -> None:
        try:
            workspace = self._annotator_workspace(params)
            service, _, actor = self._annotator_runtime()
            records = service.list_annotators(workspace, actor=actor)
            self._json(
                {
                    "workspace": workspace,
                    "annotators": [self._annotator_dto_payload(item) for item in records],
                },
                sort_keys=True,
            )
        except Exception as exc:
            self._route_error(exc)

    def _annotator_detail(self, annotator_id: str, params) -> None:
        try:
            workspace = self._annotator_workspace(params)
            service, _, actor = self._annotator_runtime()
            record = service.get_annotator(workspace, annotator_id, actor=actor)
            self._json(
                {"workspace": workspace, "annotator": self._annotator_dto_payload(record)},
                sort_keys=True,
            )
        except Exception as exc:
            self._route_error(exc)

    def _annotator_provision(self, params) -> None:
        body = self._read_body()
        try:
            if not isinstance(body, dict):
                raise PanelAnnotatorError(
                    "invalid_request", "请求体必须是对象", HTTPStatus.UNPROCESSABLE_ENTITY
                )
            self._annotator_workspace(params, body)
            idempotency_key = self._annotator_idempotency_key()
            service, repository, actor = self._annotator_runtime(
                idempotency_key=idempotency_key,
                request_id=self._annotator_request_id(),
                require_adapter=True,
            )
            record = service.create_annotator(body, actor=actor)
            self._json(
                {
                    "workspace": body.get("workspace"),
                    "annotator": self._annotator_dto_payload(record),
                    "replayed": repository.last_replayed,
                },
                status=repository.last_status,
                sort_keys=True,
            )
        except Exception as exc:
            self._route_error(exc)
        finally:
            if isinstance(body, dict):
                body.pop("initial_password", None)

    def _annotator_bind(self, params) -> None:
        body = self._read_body()
        try:
            if not isinstance(body, dict):
                raise PanelAnnotatorError(
                    "invalid_request", "请求体必须是对象", HTTPStatus.UNPROCESSABLE_ENTITY
                )
            self._annotator_workspace(params, body)
            idempotency_key = self._annotator_idempotency_key()
            service, repository, actor = self._annotator_runtime(
                idempotency_key=idempotency_key,
                request_id=self._annotator_request_id(),
                require_adapter=True,
            )
            record = service.bind_annotator(body, actor=actor)
            self._json(
                {
                    "workspace": body.get("workspace"),
                    "annotator": self._annotator_dto_payload(record),
                    "replayed": repository.last_replayed,
                },
                status=repository.last_status,
                sort_keys=True,
            )
        except Exception as exc:
            self._route_error(exc)

    def _annotator_verify(self, annotator_id: str, params) -> None:
        body = self._read_body()
        try:
            if not isinstance(body, dict):
                raise PanelAnnotatorError(
                    "invalid_request",
                    "请求体必须是对象",
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                )
            self._annotator_body_id(body, field="annotator_id", path_value=annotator_id)
            self._annotator_workspace(params, body)
            idempotency_key = self._annotator_idempotency_key()
            service, repository, actor = self._annotator_runtime(
                idempotency_key=idempotency_key,
                request_id=self._annotator_request_id(),
                require_adapter=True,
            )
            record = service.verify_annotator(body, actor=actor)
            self._json(
                {
                    "workspace": body.get("workspace"),
                    "annotator": self._annotator_dto_payload(record),
                    "replayed": repository.last_replayed,
                },
                status=repository.last_status,
                sort_keys=True,
            )
        except Exception as exc:
            self._route_error(exc)

    def _cohort_list(self, params) -> None:
        try:
            workspace = self._annotator_workspace(params)
            service, _, actor = self._annotator_runtime()
            records = service.list_cohorts(workspace, actor=actor)
            self._json(
                {
                    "workspace": workspace,
                    "cohorts": [self._annotator_dto_payload(item) for item in records],
                },
                sort_keys=True,
            )
        except Exception as exc:
            self._route_error(exc)

    def _cohort_detail(self, cohort_id: str, params) -> None:
        try:
            workspace = self._annotator_workspace(params)
            service, _, actor = self._annotator_runtime()
            record = service.get_cohort(workspace, cohort_id, actor=actor)
            self._json(
                {"workspace": workspace, "cohort": self._annotator_dto_payload(record)},
                sort_keys=True,
            )
        except Exception as exc:
            self._route_error(exc)

    def _cohort_create(self, params) -> None:
        body = self._read_body()
        try:
            if not isinstance(body, dict):
                raise PanelAnnotatorError(
                    "invalid_request", "请求体必须是对象", HTTPStatus.UNPROCESSABLE_ENTITY
                )
            self._annotator_workspace(params, body)
            idempotency_key = self._annotator_idempotency_key()
            service, repository, actor = self._annotator_runtime(
                idempotency_key=idempotency_key,
                request_id=self._annotator_request_id(),
            )
            record = service.create_cohort(body, actor=actor)
            self._json(
                {
                    "workspace": body.get("workspace"),
                    "cohort": self._annotator_dto_payload(record),
                    "replayed": repository.last_replayed,
                },
                status=repository.last_status,
                sort_keys=True,
            )
        except Exception as exc:
            self._route_error(exc)

    def _cohort_revision(self, cohort_id: str, params, *, update_basics: bool = False) -> None:
        body = self._read_body()
        try:
            if not isinstance(body, dict):
                raise PanelAnnotatorError(
                    "invalid_request",
                    "请求体必须是对象",
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                )
            self._annotator_body_id(body, field="cohort_id", path_value=cohort_id)
            self._annotator_workspace(params, body)
            idempotency_key = self._annotator_idempotency_key()
            service, repository, actor = self._annotator_runtime(
                idempotency_key=idempotency_key,
                request_id=self._annotator_request_id(),
            )
            if update_basics:
                record = service.update_cohort(body, actor=actor)
            else:
                record = service.replace_cohort_members(body, actor=actor)
            self._json(
                {
                    "workspace": body.get("workspace"),
                    "cohort": self._annotator_dto_payload(record),
                    "replayed": repository.last_replayed,
                },
                status=repository.last_status,
                sort_keys=True,
            )
        except Exception as exc:
            self._route_error(exc)

    def _workspace_selector(self, params, body: dict[str, Any] | None = None) -> str | None:
        values: list[str] = []
        for name in ("workspace", "workspace_slug"):
            query_value = str(params.get(name, [""])[0] or "").strip()
            if query_value:
                values.append(query_value)
            if body is not None:
                body_value = str(body.get(name) or "").strip()
                if body_value:
                    values.append(body_value)
        if len(set(values)) > 1:
            raise _PanelRouteError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "workspace_selector_conflict",
                "workspace 选择参数不一致",
            )
        value = values[0] if values else None
        if value is not None and not _safe_segment(value):
            raise _PanelRouteError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "invalid_workspace",
                "workspace 必须是单段安全标识符",
            )
        return value

    def _resolve_workspace(
        self,
        service: DatabaseService,
        actor_identity: ExternalIdentity,
        selected: str | None,
    ) -> str:
        if selected is not None:
            return selected
        session = service.get_session(actor_identity)
        if len(session.workspaces) == 1:
            return session.workspaces[0].workspace.slug
        if not session.workspaces:
            raise _PanelRouteError(
                HTTPStatus.NOT_FOUND,
                "resource_not_found",
                "workspace 不可见",
            )
        raise _PanelRouteError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "workspace_required",
            "当前用户有多个 workspace，必须显式选择 workspace",
        )

    def _load_active_revision(self, workspace_slug: str, task_key: str) -> ActiveTaskRevision | None:
        loader = self.active_task_loader
        if loader is None:
            raise _PanelRouteError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "active_task_loader_unavailable",
                "active task loader 尚未就绪",
            )
        try:
            loaded = loader.load_active_revision(workspace_slug, task_key)
            if loaded is None or isinstance(loaded, ActiveTaskRevision):
                return loaded
            return ActiveTaskRevision(
                revision_id=str(loaded.revision_id),
                revision_number=int(loaded.revision_number),
                definition=dict(loaded.definition),
                rendered_task=str(loaded.rendered_task),
                task_config=loaded.task_config,
            )
        except ActiveTaskLoaderUnavailable as exc:
            raise _PanelRouteError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "active_task_loader_unavailable",
                "active task loader 暂时不可用",
            ) from exc
        except Exception as exc:
            raise _PanelRouteError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "active_task_loader_unavailable",
                "active task loader 返回失败",
            ) from exc

    def _workspace_runs_root(self, workspace_slug: str) -> Path:
        if not _safe_segment(workspace_slug):
            raise _PanelRouteError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "invalid_workspace",
                "workspace 必须是单段安全标识符",
            )
        return self.runs_root / workspace_slug

    def _authorized_active_task(
        self,
        task_id: str,
        params,
        *,
        body: dict[str, Any] | None = None,
        permission: Permission = Permission.TASK_READ,
        require_task_config: bool = False,
        require_materialized: bool = True,
    ) -> tuple[str, ActiveTaskRevision, Path]:
        if not _safe_segment(task_id):
            raise ValueError("task_id 必须是单段安全标识符")
        service, actor_identity, _, _ = self._authorization_context()
        workspace_slug = self._resolve_workspace(
            service,
            actor_identity,
            self._workspace_selector(params, body),
        )
        decision = service.require_task(actor_identity, workspace_slug, task_id, permission)
        if decision.task is None:
            raise _PanelRouteError(HTTPStatus.NOT_FOUND, "resource_not_found", "任务不可见")
        if decision.task.lifecycle_state != TaskLifecycleState.ACTIVE:
            raise TaskLifecycleNotActive(decision.task.lifecycle_state)
        active = self._load_active_revision(workspace_slug, task_id)
        if active is None:
            raise _PanelRouteError(
                HTTPStatus.NOT_FOUND,
                "resource_not_found",
                "active published revision 不存在",
            )
        if require_materialized and active.task_config is None:
            raise _PanelRouteError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "active_task_materialization_unavailable",
                "active task 尚未完成物化",
            )
        return workspace_slug, active, self._workspace_runs_root(workspace_slug)

    def _task_context(
        self,
        task_id: str,
        params,
        *,
        body: dict[str, Any] | None = None,
        permission: Permission = Permission.TASK_READ,
        require_task_config: bool = False,
    ) -> tuple[Any, Path, str | None]:
        if _control_task_source_enabled():
            workspace_slug, active, workspace_runs_root = self._authorized_active_task(
                task_id,
                params,
                body=body,
                permission=permission,
            )
            return active.task_config, workspace_runs_root, workspace_slug
        if require_task_config:
            return self._load_task_by_id(task_id), self.runs_root, None
        return None, self.runs_root, None

    def _route_error(self, exc: Exception) -> None:
        if isinstance(exc, _PanelRouteError):
            self._json(
                {"error": exc.message, "code": exc.code},
                status=exc.status,
                headers=exc.headers,
            )
            return
        if isinstance(exc, PanelAnnotatorError):
            self._json(
                exc.to_payload(),
                status=exc.status,
            )
            return
        if isinstance(exc, AllocationPreviewDTOError):
            payload = {"error": exc.message, "code": exc.code}
            if exc.field is not None:
                payload["field"] = exc.field
            self._json(payload, status=HTTPStatus.UNPROCESSABLE_ENTITY, sort_keys=True)
            return
        if isinstance(exc, AllocationResourceNotFound):
            self._json(
                {"error": "allocation 资源不存在", "code": "resource_not_found"},
                status=HTTPStatus.NOT_FOUND,
            )
            return
        if isinstance(exc, AllocationRepositoryValidationError):
            self._json(
                {"error": "allocation 请求无效", "code": "invalid_allocation_request"},
                status=HTTPStatus.UNPROCESSABLE_ENTITY,
            )
            return
        if isinstance(exc, AllocationNotReady):
            self._json(
                {"error": "allocation 计划尚未满足执行条件", "code": "allocation_not_ready"},
                status=HTTPStatus.CONFLICT,
            )
            return
        if isinstance(exc, AllocationOperationInProgress):
            self._json(
                {"error": "allocation 操作正在处理中，请稍后重试", "code": "operation_in_progress"},
                status=HTTPStatus.CONFLICT,
            )
            return
        if isinstance(exc, AllocationNaturalKeyConflict):
            self._json(
                {"error": "allocation 远端响应已记录且内容不一致", "code": "natural_key_conflict"},
                status=HTTPStatus.CONFLICT,
            )
            return
        if isinstance(exc, AllocationRepositoryError):
            self._json(
                {"error": "allocation 控制服务暂时不可用", "code": "allocation_api_unavailable"},
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        if isinstance(exc, AuthorizationDenied):
            reason = exc.decision.reason
            if reason in {
                AuthorizationReason.UNKNOWN_PRINCIPAL,
                AuthorizationReason.INACTIVE_PRINCIPAL,
                AuthorizationReason.RESOURCE_NOT_VISIBLE,
                AuthorizationReason.INACTIVE_WORKSPACE,
            }:
                self._json(
                    {"error": "资源不存在", "code": "resource_not_found"},
                    status=HTTPStatus.NOT_FOUND,
                )
                return
            if reason in {AuthorizationReason.ROLE_DENIED, AuthorizationReason.INVALID_AUDIT_CONTEXT}:
                self._json(
                    {"error": "权限不足", "code": "permission_denied"},
                    status=HTTPStatus.FORBIDDEN,
                )
                return
            self._json(
                {"error": "授权服务暂时不可用", "code": "authorization_unavailable"},
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        if isinstance(exc, AuthorizationUnavailable):
            self._json(
                {"error": "数据库授权暂时不可用", "code": "authorization_unavailable"},
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        if isinstance(exc, ActiveTaskLoaderUnavailable):
            self._json(
                {"error": "active task loader 返回无效数据", "code": "active_task_loader_unavailable"},
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        if isinstance(exc, TaskPreconditionRequired):
            self._json(
                {"error": "请求必须提供 If-Match", "code": exc.code},
                status=HTTPStatus.PRECONDITION_REQUIRED,
            )
            return
        if isinstance(exc, TaskPublishReasonRequired):
            self._json(
                {"error": "任务发布必须提供 reason", "code": exc.code},
                status=HTTPStatus.UNPROCESSABLE_ENTITY,
            )
            return
        if isinstance(exc, TaskLifecycleReasonRequired):
            self._json(
                {"error": "任务生命周期变更必须提供 reason", "code": exc.code},
                status=HTTPStatus.UNPROCESSABLE_ENTITY,
            )
            return
        if isinstance(exc, TaskLifecycleStateInvalid):
            self._json(
                {"error": str(exc), "code": exc.code},
                status=HTTPStatus.UNPROCESSABLE_ENTITY,
            )
            return
        if isinstance(exc, TaskLifecycleTransitionInvalid):
            self._json(
                {
                    "error": "任务生命周期状态转移非法",
                    "code": exc.code,
                    "previous_state": exc.previous_state.value,
                    "target_state": exc.target_state.value,
                },
                status=HTTPStatus.CONFLICT,
            )
            return
        if isinstance(exc, TaskLifecycleNotActive):
            self._json(
                {"error": "任务当前未启用", "code": exc.code, "lifecycle_state": exc.state.value},
                status=HTTPStatus.CONFLICT,
            )
            return
        if isinstance(exc, TaskDraftConflict):
            headers = {"ETag": exc.current_etag} if exc.current_etag is not None else None
            self._json(
                {"error": "任务草稿已变化，请重新读取", "code": exc.code},
                status=HTTPStatus.PRECONDITION_FAILED,
                headers=headers,
            )
            return
        if isinstance(exc, TaskDraftNotFound):
            self._json(
                {"error": "任务草稿不存在", "code": exc.code},
                status=HTTPStatus.NOT_FOUND,
            )
            return
        if isinstance(exc, TaskCreateConflict):
            self._json(
                {"error": "任务编号已存在，不能重复创建", "code": exc.code},
                status=HTTPStatus.CONFLICT,
            )
            return
        if isinstance(exc, TaskCreateInProgress):
            self._json(
                {"error": "任务创建正在处理中，请稍后重试", "code": exc.code},
                status=HTTPStatus.CONFLICT,
            )
            return
        if isinstance(exc, (IdempotencyConflict, TaskPublishInProgress, TaskLifecycleInProgress)):
            self._json(
                {"error": "请求与现有幂等操作冲突", "code": exc.code},
                status=HTTPStatus.CONFLICT,
            )
            return
        if isinstance(exc, ValueError):
            self._json(
                {"error": str(exc), "code": "invalid_definition"},
                status=HTTPStatus.UNPROCESSABLE_ENTITY,
            )
            return
        self._json(
            {"error": "Panel control API 暂时不可用", "code": "control_api_unavailable"},
            status=HTTPStatus.SERVICE_UNAVAILABLE,
        )

    def _json(
        self,
        obj,
        status: int = 200,
        headers: dict[str, str] | None = None,
        *,
        sort_keys: bool = False,
    ) -> None:
        body = json.dumps(obj, ensure_ascii=False, sort_keys=sort_keys).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _read_allocation_preview_body(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            raise AllocationPreviewDTOError("invalid_json", "请求体不能为空")
        if length > 10 * 1024 * 1024:
            raise AllocationPreviewDTOError("request_too_large", "allocation preview 请求体超过 10 MiB")
        return parse_allocation_preview_json(self.rfile.read(length))

    def _allocation_preview(self) -> None:
        try:
            if not _control_task_source_enabled():
                raise _PanelRouteError(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "allocation_preview_unavailable",
                    "allocation preview 需要 scaffold 控制面",
                )
            service, actor_identity, _, _ = self._authorization_context()
            request = self._read_allocation_preview_body()
            decision = service.require_task(
                actor_identity,
                request.workspace,
                request.task_id,
                Permission.ANNOTATION_REVIEW,
            )
            if decision.task is None:
                raise _PanelRouteError(
                    HTTPStatus.NOT_FOUND,
                    "resource_not_found",
                    "任务不可见",
                )
            try:
                payload = preview_allocation_dto(request)
            except Exception as exc:
                raise _PanelRouteError(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "allocation_preview_unavailable",
                    "allocation preview 暂时不可用",
                ) from exc
            self._json(
                payload,
                status=HTTPStatus.OK if payload["ready"] else HTTPStatus.UNPROCESSABLE_ENTITY,
                sort_keys=True,
            )
        except Exception as exc:
            self._route_error(exc)

    def _read_allocation_control_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            raise AllocationPreviewDTOError("invalid_json", "请求体不能为空")
        if length > 10 * 1024 * 1024:
            raise AllocationPreviewDTOError("request_too_large", "allocation 请求体超过 10 MiB")
        return self.rfile.read(length)

    def _allocation_repository_ready(self) -> TrustedAllocationRepository:
        if not _control_task_source_enabled():
            raise _PanelRouteError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "allocation_api_unavailable",
                "allocation 控制 API 需要 scaffold 控制面",
            )
        repository = self.allocation_repository
        if not self.authorization_ready or repository is None:
            raise _PanelRouteError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "allocation_api_unavailable",
                "allocation 控制服务暂时不可用",
            )
        return repository

    def _allocation_idempotency_key(self, body_value: str | None) -> str:
        body_key = str(body_value or "").strip()
        header_key = str(self.headers.get("Idempotency-Key") or "").strip()
        if not header_key:
            raise AllocationPreviewDTOError(
                "missing_idempotency_key",
                "create 和 confirm 必须提供 Idempotency-Key header",
                field="Idempotency-Key",
            )
        if body_key and header_key and body_key != header_key:
            raise AllocationPreviewDTOError(
                "idempotency_key_conflict",
                "JSON 幂等键与 Idempotency-Key header 不一致",
                field="idempotency_key",
            )
        value = header_key
        if not _valid_idempotency_key(value):
            raise AllocationPreviewDTOError(
                "invalid_idempotency_key",
                "必须提供有效的 idempotency_key 或 Idempotency-Key header",
                field="idempotency_key",
            )
        return value

    def _allocation_request_id(self) -> str | None:
        value = str(self.headers.get("X-Request-ID") or "").strip()
        if not value:
            return None
        if len(value) > 255 or any(ord(char) < 32 for char in value):
            raise AllocationPreviewDTOError("invalid_request_id", "X-Request-ID 不是有效请求标识", field="X-Request-ID")
        return value

    def _allocation_workspace(self, params, body_workspace: str | None = None) -> str:
        service, actor_identity, _, _ = self._authorization_context()
        body = {"workspace": body_workspace} if body_workspace else None
        return self._resolve_workspace(
            service,
            actor_identity,
            self._workspace_selector(params, body),
        )

    def _allocation_limit(self, params) -> int:
        raw = str(params.get("limit", ["100"])[0] or "100")
        try:
            value = int(raw)
        except ValueError as exc:
            raise AllocationPreviewDTOError("invalid_limit", "limit 必须是 1 到 100 的整数", field="limit") from exc
        if value < 1 or value > 100:
            raise AllocationPreviewDTOError("invalid_limit", "limit 必须是 1 到 100 的整数", field="limit")
        return value

    def _allocation_plan_create(self) -> None:
        try:
            repository = self._allocation_repository_ready()
            _, actor_identity, caller_identity, channel = self._authorization_context()
            request = parse_allocation_plan_create_json(self._read_allocation_control_body())
            workspace_slug = self._workspace_selector(
                {},
                {"workspace": request.workspace},
            )
            if workspace_slug is None:
                raise _PanelRouteError(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    "workspace_required",
                    "allocation 请求必须显式选择 workspace",
                )
            idempotency_key = self._allocation_idempotency_key(request.idempotency_key)
            result = repository.create(
                request.allocation_request,
                actor_identity=actor_identity,
                caller_identity=caller_identity,
                workspace_slug=workspace_slug,
                task_key=request.task_id,
                cohort_revision_id=request.cohort_revision_id,
                idempotency_key=idempotency_key,
                channel=channel,
                request_id=self._allocation_request_id(),
            )
            self._json(serialize_allocation_plan_result(result), status=result.response_status, sort_keys=True)
        except Exception as exc:
            self._route_error(exc)

    def _allocation_plan_confirm(self, plan_id: str, params) -> None:
        try:
            repository = self._allocation_repository_ready()
            _, actor_identity, caller_identity, channel = self._authorization_context()
            plan_uuid = _allocation_uuid(plan_id, "plan_id")
            request = parse_allocation_plan_confirm_json(self._read_allocation_control_body())
            workspace_slug = self._allocation_workspace(params, request.workspace)
            idempotency_key = self._allocation_idempotency_key(request.idempotency_key)
            result = repository.confirm(
                plan_uuid,
                plan_fingerprint=request.plan_fingerprint,
                actor_identity=actor_identity,
                caller_identity=caller_identity,
                workspace_slug=workspace_slug,
                idempotency_key=idempotency_key,
                channel=channel,
                request_id=self._allocation_request_id(),
            )
            self._json(serialize_allocation_confirmation_result(result), status=result.response_status, sort_keys=True)
        except Exception as exc:
            self._route_error(exc)

    def _allocation_plan_list(self, params) -> None:
        try:
            repository = self._allocation_repository_ready()
            _, actor_identity, caller_identity, _ = self._authorization_context()
            workspace_slug = self._allocation_workspace(params)
            task_key = str(params.get("task_id", [""])[0] or "").strip() or None
            if task_key is not None and not _safe_segment(task_key):
                raise AllocationPreviewDTOError("invalid_identifier", "task_id 必须是单段安全标识符", field="task_id")
            after_value = str(params.get("after", [""])[0] or "").strip() or None
            result = repository.list_plans(
                actor_identity=actor_identity,
                caller_identity=caller_identity,
                workspace_slug=workspace_slug,
                task_key=task_key,
                limit=self._allocation_limit(params),
                after_plan_id=_allocation_uuid(after_value, "after") if after_value else None,
            )
            self._json(result, sort_keys=True)
        except Exception as exc:
            self._route_error(exc)

    def _allocation_plan_detail(self, plan_id: str | None, params) -> None:
        try:
            repository = self._allocation_repository_ready()
            _, actor_identity, caller_identity, _ = self._authorization_context()
            resolved_plan_id = plan_id or str(params.get("plan_id", [""])[0] or "").strip()
            plan_uuid = _allocation_uuid(resolved_plan_id, "plan_id")
            result = repository.get_plan(
                plan_uuid,
                actor_identity=actor_identity,
                caller_identity=caller_identity,
                workspace_slug=self._allocation_workspace(params),
            )
            self._json({"plan": result}, sort_keys=True)
        except Exception as exc:
            self._route_error(exc)

    def _allocation_plan_progress(self, plan_id: str, params) -> None:
        try:
            repository = self._allocation_repository_ready()
            _, actor_identity, _, _ = self._authorization_context()
            plan_uuid = _allocation_uuid(plan_id, "plan_id")
            progress = repository.progress(
                plan_uuid,
                actor_identity=actor_identity,
                workspace_slug=self._allocation_workspace(params),
            )
            self._json(serialize_allocation_progress(progress), sort_keys=True)
        except Exception as exc:
            self._route_error(exc)

    def _allocation_assignment_list(self, plan_id: str | None, params) -> None:
        try:
            repository = self._allocation_repository_ready()
            _, actor_identity, caller_identity, _ = self._authorization_context()
            resolved_plan_id = plan_id or str(params.get("plan_id", [""])[0] or "").strip()
            plan_uuid = _allocation_uuid(resolved_plan_id, "plan_id")
            after_value = str(params.get("after", [""])[0] or "").strip() or None
            result = repository.list_assignments(
                plan_uuid,
                actor_identity=actor_identity,
                caller_identity=caller_identity,
                workspace_slug=self._allocation_workspace(params),
                limit=self._allocation_limit(params),
                after_assignment_id=_allocation_uuid(after_value, "after") if after_value else None,
            )
            self._json(result, sort_keys=True)
        except Exception as exc:
            self._route_error(exc)

    def _allocation_assignment_detail(self, assignment_id: str | None, params) -> None:
        try:
            repository = self._allocation_repository_ready()
            _, actor_identity, caller_identity, _ = self._authorization_context()
            resolved_assignment_id = assignment_id or str(params.get("assignment_id", [""])[0] or "").strip()
            assignment_uuid = _allocation_uuid(resolved_assignment_id, "assignment_id")
            plan_id = str(params.get("plan_id", [""])[0] or "").strip() or None
            plan_uuid = _allocation_uuid(plan_id, "plan_id") if plan_id is not None else None
            result = repository.get_assignment(
                assignment_uuid,
                actor_identity=actor_identity,
                caller_identity=caller_identity,
                workspace_slug=self._allocation_workspace(params),
                plan_id=plan_uuid,
            )
            self._json({"assignment": result}, sort_keys=True)
        except Exception as exc:
            self._route_error(exc)

    def _resolve_run(self, params) -> Path | None:
        task = params.get("task", params.get("task_id", [""]))[0]
        run = params.get("run", [""])[0]
        if not _safe_segment(task) or not _safe_segment(run):
            return None
        if _control_task_source_enabled():
            try:
                _, task_runs_root, _ = self._task_context(task, params)
            except Exception:
                return None
        else:
            task_runs_root = self.runs_root
        run_dir = task_runs_root / task / run
        return run_dir if run_dir.is_dir() else None

    def _sync_tasks_if_needed(self, *, force: bool = False):
        if not _r2_task_source_enabled():
            return None
        from .task_registry import sync_tasks_from_registry

        settings = _apply_runtime_settings(self.runs_root)
        ttl_seconds = _task_registry_sync_ttl_seconds()
        cache_key = _task_registry_sync_cache_key(self.tasks_root, settings)
        now = time.monotonic()

        with _TASK_REGISTRY_SYNC_CACHE_LOCK:
            cached = _TASK_REGISTRY_SYNC_CACHE.get(cache_key)
            if not force and cached is not None and ttl_seconds > 0 and now - cached[0] < ttl_seconds:
                return cached[1]

            synced = sync_tasks_from_registry(self.tasks_root, registry_uri=settings["task_registry_uri"])
            if ttl_seconds > 0:
                _TASK_REGISTRY_SYNC_CACHE[cache_key] = (time.monotonic(), synced)
            return synced

    def _session_payload(self) -> tuple[dict[str, Any], dict[str, str]]:
        service, actor_identity, _, _ = self._authorization_context()
        context = self._request_context
        session = service.get_session(actor_identity)
        return (
            {
                "authenticated": True,
                "user": context.actor.identity.display_snapshot(),
                "authentication": {"method": context.actor.authentication_method},
                "authorization": {
                    "state": AUTHORIZATION_READY,
                    "workspaces": [_workspace_access_payload(access) for access in session.workspaces],
                },
            },
            {
                "Cache-Control": "no-store, private",
                "Pragma": "no-cache",
            },
        )

    def _control_task_list(self, params) -> None:
        try:
            service, actor_identity, _, _ = self._authorization_context()
            workspace_slug = self._resolve_workspace(
                service,
                actor_identity,
                self._workspace_selector(params),
            )
            try:
                limit = int(params.get("limit", ["100"])[0])
            except ValueError as exc:
                raise ValueError("limit 必须是整数") from exc
            after_task_key = str(params.get("after", [""])[0] or "").strip() or None
            include_archived = _truthy_value(params.get("include_archived", [""])[0])
            page = service.list_authorized_tasks(
                actor_identity,
                workspace_slug,
                limit=limit,
                after_task_key=after_task_key,
                include_archived=include_archived,
            )
            if page.workspace is None:
                raise _PanelRouteError(
                    HTTPStatus.NOT_FOUND,
                    "resource_not_found",
                    "workspace 不可见",
                )
            tasks: list[dict[str, Any]] = []
            for access in page.items:
                if Permission.TASK_READ not in access.capabilities:
                    continue
                active = None
                if access.task.lifecycle_state == TaskLifecycleState.ACTIVE:
                    active = self._load_active_revision(workspace_slug, access.task.task_key)
                if active is None and Permission.TASK_EDIT not in access.capabilities:
                    continue
                tasks.append(_active_task_summary(workspace_slug, access, active))
            self._json(
                {
                    "workspace": workspace_slug,
                    "tasks": tasks,
                    "next_cursor": page.next_cursor,
                },
            )
        except Exception as exc:
            self._route_error(exc)

    def _control_task_detail(self, task_id: str, params) -> None:
        try:
            if not _safe_segment(task_id):
                raise ValueError("task_id 必须是单段安全标识符")
            service, actor_identity, _, _ = self._authorization_context()
            workspace_slug = self._resolve_workspace(
                service,
                actor_identity,
                self._workspace_selector(params),
            )
            decision = service.require_task(actor_identity, workspace_slug, task_id, Permission.TASK_READ)
            if decision.task is None:
                raise _PanelRouteError(HTTPStatus.NOT_FOUND, "resource_not_found", "任务不可见")
            if decision.task.lifecycle_state != TaskLifecycleState.ACTIVE:
                raise TaskLifecycleNotActive(decision.task.lifecycle_state)
            active = self._load_active_revision(workspace_slug, task_id)
            if active is None:
                raise _PanelRouteError(
                    HTTPStatus.NOT_FOUND,
                    "resource_not_found",
                    "active published revision 不存在",
                )
            self._json(_active_task_detail(workspace_slug, task_id, active))
        except Exception as exc:
            self._route_error(exc)

    def _control_draft_detail(self, params) -> None:
        try:
            task_id = str(params.get("task_id", [""])[0] or "").strip()
            if not _safe_segment(task_id):
                raise ValueError("task_id 必须是单段安全标识符")
            service, actor_identity, _, _ = self._authorization_context()
            workspace_slug = self._resolve_workspace(
                service,
                actor_identity,
                self._workspace_selector(params),
            )
            draft = service.get_task_draft(
                identity=actor_identity,
                workspace_slug=workspace_slug,
                task_key=task_id,
            )
            if draft is None:
                raise _PanelRouteError(
                    HTTPStatus.NOT_FOUND,
                    "task_draft_not_found",
                    "任务草稿不存在",
                )
            self._json(
                _draft_response(task_id, draft),
                headers={"ETag": draft.etag, "Cache-Control": "no-store, private"},
            )
        except Exception as exc:
            self._route_error(exc)

    def _validated_draft_definition(
        self,
        body: dict[str, Any],
        *,
        task_id: str,
    ) -> tuple[dict[str, Any], str]:
        if not isinstance(body, dict):
            raise ValueError("任务草稿必须是 JSON 对象")
        forbidden = {"actor", "actor_id", "caller", "caller_id", "channel"}
        supplied = sorted(forbidden.intersection(body))
        if supplied:
            raise ValueError(f"身份与 channel 由服务端推导，不能提交: {', '.join(supplied)}")
        definition = dict(body)
        definition.pop("workspace", None)
        definition.pop("workspace_slug", None)
        definition.pop("idempotency_key", None)
        submitted_task_id = str(definition.get("task_id") or "").strip()
        if submitted_task_id != task_id:
            raise ValueError("body.task_id 必须与路由 task_id 一致")
        raw = pipeline.build_task_raw_from_spec(definition)
        rendered_task = yaml.safe_dump(raw, allow_unicode=True, sort_keys=False)
        return definition, rendered_task

    def _control_draft_save(
        self,
        task_id: str,
        params,
        body: dict[str, Any],
        *,
        if_match: str | None,
        create: bool,
    ) -> None:
        try:
            if not _safe_segment(task_id):
                raise ValueError("task_id 必须是单段安全标识符")
            service, actor_identity, caller_identity, channel = self._authorization_context()
            workspace_slug = self._resolve_workspace(
                service,
                actor_identity,
                self._workspace_selector(params, body),
            )
            definition, rendered_task = self._validated_draft_definition(body, task_id=task_id)
            if create:
                idempotency_key = str(
                    body.get("idempotency_key") or self.headers.get("Idempotency-Key") or "",
                ).strip()
                if not idempotency_key:
                    idempotency_key = (
                        f"task-create:{workspace_slug}:{task_id}:"
                        f"{task_definition_fingerprint(definition, rendered_task)}"
                    )
                if not _valid_idempotency_key(idempotency_key):
                    raise ValueError("任务创建必须提供有效的 idempotency_key 或 Idempotency-Key header")
                result = service.create_task(
                    actor_identity=actor_identity,
                    caller_identity=caller_identity,
                    workspace_slug=workspace_slug,
                    task_key=task_id,
                    definition=definition,
                    rendered_task=rendered_task,
                    idempotency_key=idempotency_key,
                    channel=channel,
                )
                action = "replayed" if result.replayed else "created"
                status = result.response_status
            else:
                result = service.save_task_draft(
                    actor_identity=actor_identity,
                    caller_identity=caller_identity,
                    workspace_slug=workspace_slug,
                    task_key=task_id,
                    definition=definition,
                    rendered_task=rendered_task,
                    if_match=if_match,
                    channel=channel,
                )
                action = "updated" if result.changed else "unchanged"
                status = 200
            self._json(
                _draft_response(task_id, result.draft, action=action),
                headers={"ETag": result.draft.etag, "Cache-Control": "no-store, private"},
                status=status,
            )
        except Exception as exc:
            self._route_error(exc)

    def _control_publish(self, task_id: str, params, body: dict[str, Any]) -> None:
        try:
            if not _safe_segment(task_id):
                raise ValueError("task_id 必须是单段安全标识符")
            if not isinstance(body, dict):
                raise ValueError("发布请求必须是 JSON 对象")
            forbidden = {"actor", "actor_id", "caller", "caller_id", "channel"}
            supplied = sorted(forbidden.intersection(body))
            if supplied:
                raise ValueError(f"身份与 channel 由服务端推导，不能提交: {', '.join(supplied)}")
            if not _truthy_value(body.get("confirm")):
                raise ValueError("任务发布必须显式设置 confirm=true")
            reason = str(body.get("reason") or "").strip()
            if not reason:
                raise TaskPublishReasonRequired()
            idempotency_key = str(
                body.get("idempotency_key") or self.headers.get("Idempotency-Key") or "",
            ).strip()
            if not _valid_idempotency_key(idempotency_key):
                raise ValueError("任务发布必须提供有效的 idempotency_key 或 Idempotency-Key header")
            if_match = _strong_if_match(self.headers.get("If-Match"))
            if if_match is None:
                raise TaskPreconditionRequired()
            service, actor_identity, caller_identity, channel = self._authorization_context()
            workspace_slug = self._resolve_workspace(
                service,
                actor_identity,
                self._workspace_selector(params, body),
            )
            result = service.publish_task_draft(
                actor_identity=actor_identity,
                caller_identity=caller_identity,
                workspace_slug=workspace_slug,
                task_key=task_id,
                if_match=if_match,
                reason=reason,
                idempotency_key=idempotency_key,
                channel=channel,
            )
            self._json(
                {
                    "ok": True,
                    "task": {
                        "task_id": task_id,
                        "workspace": workspace_slug,
                        "status": "publishing",
                        "revision": result.revision_number,
                    },
                    "published": {
                        "revision": result.revision_number,
                        "revision_id": str(result.revision_id),
                        "previous_revision_id": (
                            str(result.previous_revision_id) if result.previous_revision_id is not None else None
                        ),
                        "materialization_id": str(result.materialization_id),
                        "materialization_state": result.materialization_state.value,
                    },
                    "replayed": result.replayed,
                },
                status=result.response_status,
            )
        except Exception as exc:
            self._route_error(exc)

    def _control_task_lifecycle(
        self,
        task_id: str,
        action: str,
        params,
        body: dict[str, Any],
    ) -> None:
        try:
            if not _safe_segment(task_id):
                raise ValueError("task_id 必须是单段安全标识符")
            if not isinstance(body, dict):
                raise ValueError("任务生命周期请求必须是 JSON 对象")
            forbidden = {"actor", "actor_id", "caller", "caller_id", "channel"}
            supplied = sorted(forbidden.intersection(body))
            if supplied:
                raise ValueError(f"身份与 channel 由服务端推导，不能提交: {', '.join(supplied)}")
            target_state = {
                "disable": TaskLifecycleState.DISABLED,
                "archive": TaskLifecycleState.ARCHIVED,
                "restore": TaskLifecycleState.ACTIVE,
            }.get(action)
            if target_state is None:
                raise TaskLifecycleStateInvalid(action)
            reason = body.get("reason")
            idempotency_key = str(
                body.get("idempotency_key") or self.headers.get("Idempotency-Key") or "",
            ).strip()
            if not _valid_idempotency_key(idempotency_key):
                raise ValueError("任务生命周期变更必须提供有效的 idempotency_key 或 Idempotency-Key header")
            service, actor_identity, caller_identity, channel = self._authorization_context()
            workspace_slug = self._resolve_workspace(
                service,
                actor_identity,
                self._workspace_selector(params, body),
            )
            result = service.transition_task_lifecycle(
                actor_identity=actor_identity,
                caller_identity=caller_identity,
                workspace_slug=workspace_slug,
                task_key=task_id,
                target_state=target_state,
                reason=reason,
                idempotency_key=idempotency_key,
                channel=channel,
            )
            self._json(
                {
                    "ok": True,
                    "task": {
                        "task_id": result.task_key,
                        "workspace": workspace_slug,
                        "previous_state": result.previous_state.value,
                        "lifecycle_state": result.lifecycle_state.value,
                        "changed": result.changed,
                    },
                    "reason": result.reason,
                    "audit_event_id": str(result.audit_event_id) if result.audit_event_id else None,
                    "replayed": result.replayed,
                },
                status=result.response_status,
            )
        except Exception as exc:
            self._route_error(exc)

    def _control_data_lake_import(self, params) -> None:
        try:
            body = self._read_body()
            if not isinstance(body, dict):
                raise ValueError("数据湖导入请求必须是 JSON 对象")
            forbidden = {"actor", "actor_id", "caller", "caller_id", "channel"}
            supplied = sorted(forbidden.intersection(body))
            if supplied:
                raise ValueError(f"身份与 channel 由服务端推导，不能提交: {', '.join(supplied)}")
            task_id = str(body.get("task_id") or params.get("task_id", [""])[0] or "").strip()
            if not _safe_segment(task_id):
                raise ValueError("task_id 必须是单段安全标识符")
            import_id = str(body.get("import_id") or params.get("import_id", [""])[0] or "").strip()
            dry_run_value = body.get("dry_run", body.get("dryRun", params.get("dry_run", [""])[0]))
            dry_run = _truthy_value(dry_run_value)
            if self._is_mcp_request() and not dry_run and not _mcp_writes_enabled():
                raise _PanelRouteError(
                    HTTPStatus.FORBIDDEN,
                    "mcp_writes_disabled",
                    "当前部署未启用 MCP 写操作",
                )
            override_keys = (
                "lake_registry_uri",
                "source_dataset_id",
                "source_manifest_uri",
                "source_object_path",
            )
            provided_overrides = {
                key: body.get(key)
                for key in override_keys
                if body.get(key) not in (None, "")
            }
            if provided_overrides and not _allow_data_lake_overrides():
                raise ValueError("生产模式不允许覆盖数据湖来源；请在 active task 定义中配置")
            overrides = provided_overrides if _allow_data_lake_overrides() else {}
            idempotency_key = str(
                body.get("idempotency_key") or self.headers.get("Idempotency-Key") or "",
            ).strip()
            if not dry_run:
                if not _truthy_value(body.get("confirm")):
                    raise ValueError("数据湖导入提交必须显式设置 confirm=true")
                if not _valid_idempotency_key(idempotency_key):
                    raise ValueError("数据湖导入提交必须提供有效的 idempotency_key 或 Idempotency-Key header")

            service, actor_identity, caller_identity, channel = self._authorization_context()
            workspace_slug = self._resolve_workspace(
                service,
                actor_identity,
                self._workspace_selector(params, body),
            )
            decision = service.require_task(actor_identity, workspace_slug, task_id, Permission.IMPORT_SUBMIT)
            if decision.task is None:
                raise _PanelRouteError(HTTPStatus.NOT_FOUND, "resource_not_found", "任务不可见")
            if decision.task.lifecycle_state != TaskLifecycleState.ACTIVE:
                raise TaskLifecycleNotActive(decision.task.lifecycle_state)
            active = self._load_active_revision(workspace_slug, task_id)
            if active is None:
                raise _PanelRouteError(
                    HTTPStatus.NOT_FOUND,
                    "resource_not_found",
                    "active published revision 不存在",
                )
            if active.task_config is None:
                raise _PanelRouteError(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "active_task_materialization_unavailable",
                    "active task 尚未完成物化",
                )

            max_bytes = int(os.environ.get("LLS_MAX_IMPORT_BYTES", str(100 * 1024 * 1024)))
            _apply_runtime_settings(self.runs_root)
            workspace_runs_root = self._workspace_runs_root(workspace_slug)
            if dry_run:
                result = pipeline.dry_run_data_lake_import(
                    workspace_runs_root,
                    active.task_config,
                    import_id=import_id or None,
                    overrides=overrides,
                    max_bytes=max_bytes,
                )
                self._json({"ok": bool(result["validation"]["ok"]), "dry_run": True, "result": result})
                return

            service.append_audit(
                workspace_slug=workspace_slug,
                event_type="task.import_requested",
                actor_identity=actor_identity,
                caller_identity=caller_identity,
                channel=channel,
                required_permission=Permission.IMPORT_SUBMIT,
                task_key=task_id,
                details={"import_id": import_id or None},
            )
            job = pipeline.start_data_lake_import(
                workspace_runs_root,
                active.task_config,
                import_id=import_id or None,
                overrides=overrides,
                max_bytes=max_bytes,
                idempotency_key=idempotency_key,
            )
            self._json({"ok": True, "job": job})
        except Exception as exc:
            self._route_error(exc)

    def _control_import_list(self, params) -> None:
        try:
            task_id = str(params.get("task_id", [""])[0] or "").strip()
            workspace_slug, active, workspace_runs_root = self._authorized_active_task(task_id, params)
            self._json({
                "workspace": workspace_slug,
                "imports": pipeline.list_imports(
                    workspace_runs_root,
                    task_id,
                    id_field=active.task_config.id_field,
                ),
            })
        except Exception as exc:
            self._route_error(exc)

    def _control_import_detail(self, params) -> None:
        try:
            task_id = str(params.get("task_id", [""])[0] or "").strip()
            import_id = str(params.get("import_id", [""])[0] or "").strip()
            if not _safe_segment(import_id):
                raise ValueError("import_id 必须是单段安全标识符")
            workspace_slug, active, workspace_runs_root = self._authorized_active_task(task_id, params)
            try:
                item = pipeline.import_detail(
                    workspace_runs_root,
                    task_id,
                    import_id,
                    id_field=active.task_config.id_field,
                )
            except ValueError as exc:
                raise _PanelRouteError(
                    HTTPStatus.NOT_FOUND,
                    "resource_not_found",
                    "导入资产不存在",
                ) from exc
            self._json({"workspace": workspace_slug, "import": item})
        except Exception as exc:
            self._route_error(exc)

    def _control_jobs(self, params) -> None:
        try:
            task_id = str(params.get("task_id", [""])[0] or "").strip()
            workspace_slug, _, workspace_runs_root = self._authorized_active_task(task_id, params)
            self._json({
                "workspace": workspace_slug,
                "jobs": pipeline.jobs_for_task(workspace_runs_root, task_id),
            })
        except Exception as exc:
            self._route_error(exc)

    def _control_data_lake_preview(self, params) -> None:
        try:
            from .data_lake import preview_source

            task_id = str(params.get("task_id", [""])[0] or "").strip()
            workspace_slug, active, _ = self._authorized_active_task(task_id, params)
            _apply_runtime_settings(self.runs_root)
            task_config = active.task_config
            self._json({
                "workspace": workspace_slug,
                "enabled": bool(task_config.data_lake),
                "data_lake": task_config.data_lake,
                "preview": preview_source(task_config) if task_config.data_lake else None,
            })
        except Exception as exc:
            self._route_error(exc)

    def _list_tasks(self) -> list[dict]:
        if _control_task_source_enabled():
            raise RuntimeError("control 模式任务列表必须通过数据库授权 resolver")
        tasks = pipeline.list_tasks(self.tasks_root)
        if not _r2_task_source_enabled():
            return tasks
        out: list[dict] = []
        for task in tasks:
            task_id = task.get("task_id")
            if not task_id:
                continue
            item = dict(task)
            item["source"] = "R2 数据湖"
            item["deletable"] = False
            out.append(item)
        return out

    def _load_task_by_id(self, task_id: str):
        if _control_task_source_enabled():
            service, actor_identity, _, _ = self._authorization_context()
            workspace_slug = self._resolve_workspace(service, actor_identity, None)
            decision = service.require_task(actor_identity, workspace_slug, task_id, Permission.TASK_READ)
            if decision.task is None:
                raise ValueError(f"任务不可见: {task_id}")
            if decision.task.lifecycle_state != TaskLifecycleState.ACTIVE:
                raise TaskLifecycleNotActive(decision.task.lifecycle_state)
            active = self._load_active_revision(workspace_slug, task_id)
            if active is None:
                raise ValueError(f"任务尚未发布或不存在: {task_id}")
            if active.task_config is None:
                raise ActiveTaskLoaderUnavailable(f"任务尚未完成物化: {task_id}")
            return active.task_config
        return pipeline.load_task_by_id(self.tasks_root, task_id)

    def _resolve_action_task_path(
        self,
        task_path: str,
        body: dict[str, Any] | None = None,
    ) -> tuple[Any, Path]:
        if _control_task_source_enabled():
            reference = str(task_path or "").strip()
            reference_path = Path(reference)
            if reference_path.name == "task.yaml":
                task_id = reference_path.parent.name
            else:
                task_id = reference
            if not _safe_segment(task_id):
                raise ValueError("control 模式 action 必须提交 task_id 或 task.yaml 逻辑路径")
            _, active, workspace_runs_root = self._authorized_active_task(
                task_id,
                {},
                body=body,
            )
            return active.task_config, workspace_runs_root
        if not _r2_task_source_enabled():
            return task_path, self.runs_root
        try:
            submitted = Path(task_path).expanduser().resolve()
        except Exception as exc:
            raise ValueError(f"任务路径无效: {task_path}") from exc
        for task in pipeline.list_tasks(self.tasks_root):
            local_path = task.get("path")
            if not local_path:
                continue
            try:
                resolved = Path(local_path).expanduser().resolve()
            except Exception:
                continue
            if submitted == resolved:
                return str(resolved), self.runs_root
        raise ValueError("生产模式只能执行已同步到本地缓存的 R2 任务配置；请先同步任务配置")

    def _sync_tasks_from_registry(self) -> None:
        if not _r2_task_source_enabled():
            self._json({"error": "当前不是 R2 任务来源模式，不需要同步任务配置"}, status=400)
            return
        try:
            synced = self._sync_tasks_if_needed(force=True)
            tasks = self._list_tasks()
            self._json({
                "ok": True,
                "sync": {
                    "registry_uri": synced.registry_uri if synced is not None else None,
                    "tasks": synced.tasks if synced is not None else {},
                },
                "tasks": tasks,
            })
        except Exception as exc:
            self._json({"error": str(exc)}, status=400)

    def _task_detail(self, task_id: str) -> None:
        if not _safe_segment(task_id):
            self._json({"error": "bad task"}, status=400)
            return
        try:
            self._json(_task_detail_payload(self._load_task_by_id(task_id)))
        except Exception as exc:
            self._json({"error": str(exc)}, status=404)

    def _task_check(self, task_id: str) -> None:
        if not _safe_segment(task_id):
            self._json({"ok": False, "task_id": task_id, "checks": [], "warnings": [], "errors": [
                {"check": "task_id", "message": "bad task"}
            ]}, status=400)
            return
        try:
            task = self._load_task_by_id(task_id)
        except Exception as exc:
            self._json({
                "ok": False,
                "task_id": task_id,
                "checks": [{
                    "name": "task_load",
                    "status": "error",
                    "message": str(exc),
                    "details": {"error_class": exc.__class__.__name__},
                }],
                "warnings": [],
                "errors": [{
                    "check": "task_load",
                    "message": str(exc),
                    "details": {"error_class": exc.__class__.__name__},
                }],
            }, status=404)
            return
        self._task_check_loaded(task_id, task)

    def _control_task_check(self, task_id: str, params, body: dict[str, Any]) -> None:
        try:
            _, active, _ = self._authorized_active_task(task_id, params, body=body)
            self._task_check_loaded(task_id, active.task_config)
        except Exception as exc:
            self._route_error(exc)

    def _task_check_loaded(self, task_id: str, task) -> None:
        checks: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []

        def add_check(name: str, status: str, message: str, details: dict[str, Any] | None = None) -> None:
            item = {"name": name, "status": status, "message": message}
            if details:
                item["details"] = details
            checks.append(item)
            if status == "warning":
                warnings.append({"check": name, "message": message, "details": details or {}})
            elif status == "error":
                errors.append({"check": name, "message": message, "details": details or {}})

        add_check("task_load", "ok", "task loaded", {"path": str(task.path)})

        try:
            from .profiles import profile_definition

            profile = profile_definition(task.profile)
            add_check("profile", "ok", "profile is valid", {
                "profile_id": task.profile,
                "stage_count": len(profile.get("stages", [])),
            })
        except Exception as exc:  # noqa: BLE001
            add_check("profile", "error", str(exc), {
                "profile_id": task.profile,
                "error_class": exc.__class__.__name__,
            })

        if not task.data_lake:
            add_check("data_lake_config", "error", "task has no data_lake configuration")
        else:
            add_check("data_lake_config", "ok", "task has data_lake configuration", _safe_data_lake_summary(task.data_lake))
            try:
                from .data_lake import preview_source

                _apply_runtime_settings(self.runs_root)
                preview = preview_source(task)
                add_check("data_lake_preview", "ok", "data_lake source can be previewed", {"preview": preview})
            except Exception as exc:  # noqa: BLE001
                add_check("data_lake_preview", "error", str(exc), {"error_class": exc.__class__.__name__})

        self._json({
            "ok": not errors,
            "task_id": task_id,
            "checks": checks,
            "warnings": warnings,
            "errors": errors,
        })

    def _serve_static(self, path: str) -> bool:
        if not self.static_dir:
            return False
        rel = path.lstrip("/") or "index.html"
        target = (self.static_dir / rel).resolve()
        try:
            target.relative_to(self.static_dir.resolve())
        except ValueError:
            return False
        if not target.is_file():
            target = self.static_dir / "index.html"
            if not target.is_file():
                return False
        ctype = "text/html; charset=utf-8"
        if target.suffix == ".js":
            ctype = "text/javascript; charset=utf-8"
        elif target.suffix == ".css":
            ctype = "text/css; charset=utf-8"
        elif target.suffix == ".json":
            ctype = "application/json; charset=utf-8"
        elif target.suffix == ".svg":
            ctype = "image/svg+xml"
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return True

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if not path.startswith("/api/") and self._serve_static(path):
            return
        if not self._require_auth():
            return
        params = parse_qs(parsed.query)
        annotator_route = _annotator_route_kind(self.command, path)
        if annotator_route is not None:
            kind, resource_id = annotator_route
            if kind == "annotators_list":
                self._annotator_list(params)
            elif kind == "annotator_detail":
                self._annotator_detail(resource_id, params)
            elif kind == "cohorts_list":
                self._cohort_list(params)
            elif kind == "cohort_detail":
                self._cohort_detail(resource_id, params)
            return
        contract_task_id = _contract_task_path(path)
        if path == "/api/health":
            self._json({"ok": True, "status": "ok", "service": "llm-labeling-scaffold"})
        elif path == "/api/version":
            self._json({
                "service": "llm-labeling-scaffold",
                "version": __version__,
                "api_contract_version": API_CONTRACT_VERSION,
            })
        elif path == "/api/session":
            try:
                payload, headers = self._session_payload()
                self._json(payload, headers=headers)
            except Exception as exc:
                self._route_error(exc)
        elif path == "/api/capabilities":
            auth_mode = self.authenticator.mode if self.authenticator is not None else "unconfigured"
            authorization_state = AUTHORIZATION_READY if self.authorization_ready else AUTHORIZATION_UNAVAILABLE
            self._json(_contract_capabilities(auth_mode, authorization_state))
        elif path == "/api/settings/public":
            try:
                settings = _apply_runtime_settings(self.runs_root)
                self._json(_public_settings_response(settings))
            except Exception as exc:
                self._json({"error": str(exc)}, status=400)
        elif path == "/api/data_lake/catalog":
            try:
                _apply_runtime_settings(self.runs_root)
                dataset_id = str(params.get("dataset_id", [""])[0] or "").strip()
                if dataset_id and not _safe_segment(dataset_id):
                    raise ValueError("dataset_id 必须是单段安全标识符")
                self._json(_data_lake_catalog_payload(detail_dataset_id=dataset_id or None))
            except Exception as exc:
                self._json({"error": str(exc)}, status=400)
        elif path == "/api/allocation/plans":
            self._allocation_plan_list(params)
        elif path == "/api/allocation/plan/detail":
            self._allocation_plan_detail(None, params)
        elif path == "/api/allocation/assignments":
            self._allocation_assignment_list(None, params)
        elif path == "/api/allocation/assignment/detail":
            self._allocation_assignment_detail(None, params)
        elif _allocation_plan_path(path, suffix="progress") is not None:
            self._allocation_plan_progress(_allocation_plan_path(path, suffix="progress"), params)
        elif _allocation_plan_path(path, suffix="assignments") is not None:
            self._allocation_assignment_list(_allocation_plan_path(path, suffix="assignments"), params)
        elif _allocation_assignment_path(path) is not None:
            self._allocation_assignment_detail(_allocation_assignment_path(path), params)
        elif _allocation_plan_path(path) is not None:
            self._allocation_plan_detail(_allocation_plan_path(path), params)
        elif contract_task_id is not None:
            if _control_task_source_enabled():
                self._control_task_detail(contract_task_id, params)
            else:
                self._task_detail(contract_task_id)
        elif path.startswith("/api/tasks/"):
            self._json({"error": "not found"}, status=404)
        elif path == "/api/runs":
            if _control_task_source_enabled():
                self._json({"error": "control 模式必须使用带 task_id 的任务范围接口"}, status=400)
                return
            self._json({"runs": discover_runs(self.runs_root)})
        elif path == "/api/run":
            run_dir = self._resolve_run(params)
            if not run_dir:
                self._json({"error": "unknown run"}, status=404)
                return
            self._json(run_detail(run_dir))
        elif path == "/api/rows":
            run_dir = self._resolve_run(params)
            if not run_dir:
                self._json({"error": "unknown run"}, status=404)
                return
            kind = params.get("kind", ["merged"])[0]
            self._json({"kind": kind, "rows": pool_rows(run_dir, kind)})
        elif path == "/api/gold":
            task = params.get("task", [""])[0]
            if not _safe_segment(task):
                self._json({"error": "bad task"}, status=400)
                return
            try:
                _, task_runs_root, _ = self._task_context(task, params)
                self._json({"gold": list_gold(task_runs_root, task)})
            except Exception as exc:
                self._route_error(exc)
        elif path == "/api/tasks":
            if _control_task_source_enabled():
                self._control_task_list(params)
            else:
                try:
                    self._json({"tasks": self._list_tasks()})
                except Exception as exc:
                    self._json({"error": str(exc)}, status=400)
        elif path == "/api/task/control":
            if not _control_task_source_enabled():
                self._json({"error": "当前不是 scaffold 控制面任务来源模式"}, status=400)
                return
            self._control_draft_detail(params)
        elif path == "/api/task/profile":
            task = params.get("task_id", [""])[0]
            preset = params.get("preset", [""])[0].strip() or None
            if not _safe_segment(task):
                self._json({"error": "bad task"}, status=400)
                return
            try:
                task_cfg, task_runs_root, _ = self._task_context(
                    task,
                    params,
                    require_task_config=True,
                )
                self._json(pipeline.task_profile_status(task_runs_root, task_cfg, profile_id=preset))
            except Exception as exc:
                self._route_error(exc)
        elif path == "/api/task/graph":
            task = params.get("task_id", [""])[0]
            preset = params.get("preset", [""])[0].strip() or None
            if not _safe_segment(task):
                self._json({"error": "bad task"}, status=400)
                return
            try:
                task_cfg, task_runs_root, _ = self._task_context(
                    task,
                    params,
                    require_task_config=True,
                )
                self._json(pipeline.task_asset_graph(task_runs_root, task_cfg, profile_id=preset))
            except Exception as exc:
                self._route_error(exc)
        elif path == "/api/task/archive_plan":
            task = params.get("task_id", [""])[0]
            if not _safe_segment(task):
                self._json({"error": "bad task"}, status=400)
                return
            try:
                task_cfg, task_runs_root, _ = self._task_context(
                    task,
                    params,
                    require_task_config=True,
                )
                self._json(pipeline.task_archive_plan(
                    self.tasks_root,
                    task_runs_root,
                    task_cfg,
                    r2_task_source=_r2_task_source_enabled(),
                    control_task_source=_control_task_source_enabled(),
                ))
            except Exception as exc:
                self._route_error(exc)
        elif path == "/api/profile/presets":
            try:
                from .profiles import DEFAULT_PROFILE, list_profile_presets

                self._json({"default_profile_id": DEFAULT_PROFILE, "presets": list_profile_presets()})
            except Exception as exc:
                self._json({"error": str(exc)}, status=400)
        elif path == "/api/settings":
            try:
                settings = _apply_runtime_settings(self.runs_root)
                self._json(_settings_response(settings))
            except Exception as exc:
                self._json({"error": str(exc)}, status=400)
        elif path == "/api/config":
            try:
                settings = _apply_runtime_settings(self.runs_root)
                self._json(_settings_response(settings, include_legacy=True))
            except Exception as exc:
                self._json({"error": str(exc)}, status=400)
        elif path == "/api/task/runs":
            task = params.get("task_id", [""])[0]
            if not _safe_segment(task):
                self._json({"error": "bad task"}, status=400)
                return
            try:
                _, task_runs_root, _ = self._task_context(task, params)
                self._json({"runs": pipeline.list_runs(task_runs_root, task)})
            except Exception as exc:
                self._route_error(exc)
        elif path == "/api/task/samples":
            task = params.get("task_id", [""])[0]
            if not _safe_segment(task):
                self._json({"error": "bad task"}, status=400)
                return
            try:
                _, task_runs_root, _ = self._task_context(task, params)
                self._json({"samples": pipeline.list_samples(task_runs_root, task)})
            except Exception as exc:
                self._route_error(exc)
        elif path == "/api/task/imports":
            if _control_task_source_enabled():
                self._control_import_list(params)
                return
            task = params.get("task_id", [""])[0]
            if not _safe_segment(task):
                self._json({"error": "bad task"}, status=400)
                return
            try:
                task_cfg, task_runs_root, _ = self._task_context(
                    task,
                    params,
                    require_task_config=True,
                )
                self._json({"imports": pipeline.list_imports(task_runs_root, task, id_field=task_cfg.id_field)})
            except Exception as exc:
                self._route_error(exc)
        elif path == "/api/import/detail":
            if _control_task_source_enabled():
                self._control_import_detail(params)
                return
            task = params.get("task_id", [""])[0]
            import_id = params.get("import_id", [""])[0]
            try:
                task_cfg, task_runs_root, _ = self._task_context(
                    task,
                    params,
                    require_task_config=True,
                )
                self._json({"import": pipeline.import_detail(task_runs_root, task, import_id, id_field=task_cfg.id_field)})
            except Exception as exc:
                self._route_error(exc)
        elif path == "/api/import/rows":
            task = params.get("task_id", [""])[0]
            import_id = params.get("import_id", [""])[0]
            try:
                _, task_runs_root, _ = self._task_context(task, params)
                self._json(pipeline.import_rows(
                    task_runs_root,
                    task,
                    import_id,
                    offset=int(params.get("offset", ["0"])[0] or 0),
                    limit=int(params.get("limit", ["50"])[0] or 50),
                    query=params.get("q", [""])[0],
                ))
            except Exception as exc:
                self._json({"error": str(exc)}, status=400)
        elif path == "/api/import/download":
            self._download_import(params)
        elif path == "/api/suggestions/download":
            self._download_suggestions(params)
        elif path == "/api/task/annotation_jobs":
            task = params.get("task_id", [""])[0]
            if not _safe_segment(task):
                self._json({"error": "bad task"}, status=400)
                return
            try:
                _, task_runs_root, _ = self._task_context(task, params)
                self._json({"annotation_jobs": pipeline.list_annotation_jobs(task_runs_root, task)})
            except Exception as exc:
                self._route_error(exc)
        elif path == "/api/annotation_job/detail":
            task = params.get("task_id", [""])[0]
            annotation_id = params.get("annotation_id", [""])[0]
            if not _safe_segment(task) or not _safe_segment(annotation_id):
                self._json({"error": "bad task/annotation_id"}, status=400)
                return
            try:
                _, task_runs_root, _ = self._task_context(task, params)
                self._json({"annotation_job": pipeline.annotation_job_detail(task_runs_root, task, annotation_id)})
            except ValueError as exc:
                self._json({"error": str(exc), "found": False}, status=404)
            except Exception as exc:
                self._route_error(exc)
        elif path == "/api/task/agreement_audits":
            task = params.get("task_id", [""])[0]
            if not _safe_segment(task):
                self._json({"error": "bad task"}, status=400)
                return
            try:
                _, task_runs_root, _ = self._task_context(task, params)
                self._json({"agreement_audits": pipeline.list_agreement_audits(task_runs_root, task)})
            except Exception as exc:
                self._route_error(exc)
        elif path == "/api/task/models":
            task = params.get("task_id", [""])[0]
            if not _safe_segment(task):
                self._json({"error": "bad task"}, status=400)
                return
            try:
                _, task_runs_root, _ = self._task_context(task, params)
                self._json({"models": pipeline.list_models(task_runs_root, task)})
            except Exception as exc:
                self._route_error(exc)
        elif path == "/api/task/gold_versions":
            task = params.get("task_id", [""])[0]
            if not _safe_segment(task):
                self._json({"error": "bad task"}, status=400)
                return
            try:
                _, task_runs_root, _ = self._task_context(task, params)
                self._json({"gold_versions": pipeline.list_gold_versions(task_runs_root, task)})
            except Exception as exc:
                self._route_error(exc)
        elif path == "/api/gold_version/detail":
            task = params.get("task_id", [""])[0]
            version = params.get("version", [""])[0]
            if not _safe_segment(task) or not _safe_segment(version):
                self._json({"error": "bad task/version"}, status=400)
                return
            try:
                _, task_runs_root, _ = self._task_context(task, params)
                self._json({"gold_version": pipeline.gold_version_detail(task_runs_root, task, version)})
            except ValueError as exc:
                self._json({"error": str(exc), "found": False}, status=404)
            except Exception as exc:
                self._route_error(exc)
        elif path == "/api/task/decision_artifacts":
            task = params.get("task_id", [""])[0]
            if not _safe_segment(task):
                self._json({"error": "bad task"}, status=400)
                return
            try:
                _, task_runs_root, _ = self._task_context(task, params)
                self._json({"decision_artifacts": pipeline.list_decision_artifacts(task_runs_root, task)})
            except Exception as exc:
                self._route_error(exc)
        elif path == "/api/decision_artifact/detail":
            task = params.get("task_id", [""])[0]
            decision_id = params.get("decision_id", [""])[0]
            if not _safe_segment(task) or not _safe_segment(decision_id):
                self._json({"error": "bad task/decision_id"}, status=400)
                return
            try:
                _, task_runs_root, _ = self._task_context(task, params)
                self._json({"decision_artifact": pipeline.decision_artifact_detail(task_runs_root, task, decision_id)})
            except ValueError as exc:
                self._json({"error": str(exc), "found": False}, status=404)
            except Exception as exc:
                self._route_error(exc)
        elif path == "/api/task/decisions":
            task = params.get("task_id", [""])[0]
            run = params.get("run", [""])[0]
            if not _safe_segment(task) or not _safe_segment(run):
                self._json({"error": "bad task/run"}, status=400)
                return
            try:
                _, task_runs_root, _ = self._task_context(task, params)
                self._json({"decisions": pipeline.list_decisions(task_runs_root, task, run)})
            except Exception as exc:
                self._route_error(exc)
        elif path == "/api/jobs":
            if _control_task_source_enabled():
                self._control_jobs(params)
                return
            task = params.get("task_id", [""])[0]
            if not _safe_segment(task):
                self._json({"error": "bad task"}, status=400)
                return
            try:
                _, task_runs_root, _ = self._task_context(task, params)
                self._json({"jobs": pipeline.jobs_for_task(task_runs_root, task)})
            except Exception as exc:
                self._route_error(exc)
        elif path == "/api/task/audit":
            task = params.get("task_id", [""])[0]
            if not _safe_segment(task):
                self._json({"error": "bad task"}, status=400)
                return
            try:
                _, task_runs_root, _ = self._task_context(task, params)
                self._json({"events": pipeline.list_audit_events(task_runs_root, task)})
            except Exception as exc:
                self._route_error(exc)
        elif path == "/api/task/data_lake":
            if _control_task_source_enabled():
                self._control_data_lake_preview(params)
                return
            task = params.get("task_id", [""])[0]
            if not _safe_segment(task):
                self._json({"error": "bad task"}, status=400)
                return
            try:
                from .data_lake import preview_source

                _apply_runtime_settings(self.runs_root)
                task_cfg, _, _ = self._task_context(
                    task,
                    params,
                    require_task_config=True,
                )
                self._json({
                    "enabled": bool(task_cfg.data_lake),
                    "data_lake": task_cfg.data_lake,
                    "preview": preview_source(task_cfg) if task_cfg.data_lake else None,
                })
            except Exception as exc:
                self._json({"error": str(exc)}, status=400)
        elif path == "/api/argilla/status":
            try:
                from .integrations.argilla import test_connection

                self._json(test_connection({
                    "workspace": params.get("workspace", [""])[0],
                }))
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, status=400)
        elif path == "/api/export":
            self._export(params)
        elif path.startswith("/api/"):
            self._json({"error": "not found"}, status=404)
        else:
            if not self._serve_static(path):
                self._json({"error": "not found"}, status=404)

    def _export(self, params) -> None:
        run_dir = self._resolve_run(params)
        kind = params.get("kind", ["merged"])[0]
        if not run_dir or kind not in POOL_FILES:
            self._json({"error": "unknown export"}, status=404)
            return
        d, f = POOL_FILES[kind]
        path = run_dir / d / f
        if not path.exists():
            self._json({"error": "no data"}, status=404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{kind}.jsonl"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if not self._require_auth():
            return
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)
        annotator_route = _annotator_route_kind(self.command, path)
        if annotator_route is not None:
            kind, resource_id = annotator_route
            if kind == "annotator_provision":
                self._annotator_provision(params)
            elif kind == "annotator_bind":
                self._annotator_bind(params)
            elif kind == "annotator_verify":
                self._annotator_verify(resource_id, params)
            elif kind == "cohort_create":
                self._cohort_create(params)
            elif kind == "cohort_revision_create":
                self._cohort_revision(resource_id, params)
            return
        contract_check_task_id = _contract_task_path(path, suffix="check")
        publish_task_id = _contract_task_path(path, suffix="publish")
        lifecycle_path = _contract_task_lifecycle_path(path)
        if contract_check_task_id is not None:
            if _control_task_source_enabled():
                self._control_task_check(contract_check_task_id, params, self._read_body())
            else:
                self._task_check(contract_check_task_id)
        elif publish_task_id is not None:
            if not _control_task_source_enabled():
                self._json({"error": "只有 scaffold 控制面任务来源模式支持发布任务 revision"}, status=400)
                return
            self._control_publish(publish_task_id, params, self._read_body())
        elif lifecycle_path is not None:
            if not _control_task_source_enabled():
                self._json({"error": "只有 scaffold 控制面任务来源模式支持任务生命周期变更"}, status=400)
                return
            task_id, action = lifecycle_path
            self._control_task_lifecycle(task_id, action, params, self._read_body())
        elif path == "/api/tasks/sync":
            self._sync_tasks_from_registry()
        elif path.startswith("/api/tasks/"):
            self._json({"error": "not found"}, status=404)
        elif path == "/api/adjudicate":
            run_dir = self._resolve_run(params)
            if not run_dir:
                self._json({"error": "unknown run"}, status=404)
                return
            body = self._read_body()
            rid = body.get("id")
            human_label = body.get("human_label")
            if not rid or not isinstance(human_label, dict):
                self._json({"error": "id and human_label required"}, status=400)
                return
            id_field = body.get("id_field", "record_id")
            count = append_decision(run_dir, {
                id_field: rid,
                "human_label": human_label,
                "note": body.get("note", ""),
            })
            self._json({"ok": True, "decisions": count})
        elif path == "/api/allocation/preview":
            self._allocation_preview()
        elif path in {"/api/allocation/plans", "/api/allocation/plan"}:
            self._allocation_plan_create()
        elif _allocation_plan_path(path, suffix="confirm") is not None:
            self._allocation_plan_confirm(_allocation_plan_path(path, suffix="confirm"), params)
        elif path == "/api/action":
            body = self._read_body()
            task_path = body.get("task")
            action = body.get("action")
            if not task_path or not action:
                self._json({"error": "task and action required"}, status=400)
                return
            try:
                resolved_task, resolved_runs_root = self._resolve_action_task_path(task_path, body)
                job = pipeline.start_action(
                    resolved_runs_root,
                    resolved_task,
                    action,
                    body.get("params", {}),
                )
                self._json({"ok": True, "job": job})
            except Exception as exc:
                self._json({"error": redact_text(exc)}, status=400)
        elif path == "/api/suggestions/import":
            self._import_suggestions(params)
        elif path == "/api/tasks":
            body = self._read_body()
            if _control_task_source_enabled():
                task_id = str(body.get("task_id") or "").strip() if isinstance(body, dict) else ""
                self._control_draft_save(task_id, params, body, if_match=None, create=True)
                return
            try:
                if _r2_task_source_enabled():
                    self._json({"error": "生产模式任务必须从 R2 数据湖登记表拉取，不能在面板中新建本地任务"}, status=400)
                    return
                if not _allow_data_lake_overrides() and "data_lake" in body:
                    body = dict(body)
                    body.pop("data_lake", None)
                task = pipeline.create_task(self.tasks_root, body)
                self._json({"ok": True, "task": task})
            except Exception as exc:
                self._json({"error": str(exc)}, status=400)
        elif path == "/api/import":
            self._import(params)
        elif path == "/api/import/data_lake":
            if _control_task_source_enabled():
                self._control_data_lake_import(params)
            else:
                self._import_data_lake(params)
        elif path == "/api/task/archive":
            if _control_task_source_enabled():
                self._json({"error": "control 模式不能通过文件归档任务；请在控制面停用或归档任务"}, status=400)
                return
            body = self._read_body()
            task_id = str(body.get("task_id") or params.get("task_id", [""])[0])
            reason = str(body.get("reason") or "")
            if not _safe_segment(task_id):
                self._json({"error": "bad task"}, status=400)
                return
            try:
                result = pipeline.execute_task_archive(
                    self.tasks_root,
                    self.runs_root,
                    task_id,
                    reason=reason,
                    actor=self._actor(),
                    r2_task_source=_r2_task_source_enabled(),
                )
                self._json({"ok": True, "archive": result})
            except Exception as exc:
                self._json({"error": str(exc)}, status=400)
        elif path == "/api/task/cache_cleanup":
            if _control_task_source_enabled():
                self._json({"error": "control 模式不能清理任务配置缓存"}, status=400)
                return
            body = self._read_body()
            task_id = str(body.get("task_id") or params.get("task_id", [""])[0])
            if not _safe_segment(task_id):
                self._json({"error": "bad task"}, status=400)
                return
            try:
                result = pipeline.execute_task_cache_cleanup(self.runs_root, task_id, actor=self._actor())
                self._json({"ok": result.get("ok", False), "cleanup": result})
            except Exception as exc:
                self._json({"error": str(exc)}, status=400)
        elif path == "/api/settings":
            body = self._read_body()
            if not isinstance(body, dict):
                self._json({"error": "settings payload must be a JSON object"}, status=400)
                return
            try:
                settings = panel_settings.update_settings(self.runs_root, body)
                _invalidate_task_registry_sync_cache()
                _apply_runtime_settings(self.runs_root, settings)
                self._json({"ok": True, **_settings_response(settings)})
            except Exception as exc:
                self._json({"error": str(exc)}, status=400)
        else:
            self._json({"error": "not found"}, status=404)

    def do_PUT(self) -> None:
        if not self._require_auth():
            return
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)
        annotator_route = _annotator_route_kind(self.command, path)
        if annotator_route is not None:
            kind, resource_id = annotator_route
            if kind == "cohort_update":
                self._cohort_revision(resource_id, params, update_basics=True)
            elif kind == "cohort_members_replace":
                self._cohort_revision(resource_id, params)
            return
        task_id = _contract_task_path(path)
        if task_id is None:
            self._json({"error": "not found"}, status=404)
            return
        if not _control_task_source_enabled():
            self._json({"error": "只有 scaffold 控制面任务来源模式支持编辑任务草稿"}, status=400)
            return
        try:
            if_match = _strong_if_match(self.headers.get("If-Match"))
            if if_match is None:
                raise TaskPreconditionRequired()
            self._control_draft_save(
                task_id,
                params,
                self._read_body(),
                if_match=if_match,
                create=False,
            )
        except Exception as exc:
            self._route_error(exc)

    def do_DELETE(self) -> None:
        if not self._require_auth():
            return
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)
        if path == "/api/tasks":
            task_id = params.get("task_id", [""])[0]
            delete_runs = params.get("delete_runs", ["0"])[0] in {"1", "true", "yes"}
            try:
                if _control_task_source_enabled():
                    self._json({"error": "控制面任务不能直接删除本地 task.yaml；请通过任务状态停用或归档"}, status=400)
                    return
                if _r2_task_source_enabled():
                    self._json({"error": "生产模式任务来自 R2 数据湖登记表，不能在面板中归档本地缓存；请在登记表中标记为非启用状态"}, status=400)
                    return
                self._json({
                    "ok": True,
                    "task": pipeline.delete_task(
                        self.tasks_root,
                        task_id,
                        runs_root=self.runs_root,
                        delete_runs=delete_runs,
                    ),
                })
            except Exception as exc:
                self._json({"error": str(exc)}, status=400)
        elif path == "/api/import":
            if _control_task_source_enabled():
                self._json({"error": "control 模式暂不提供文件资产归档入口"}, status=400)
                return
            task_id = params.get("task_id", [""])[0]
            import_id = params.get("import_id", [""])[0]
            reason = params.get("reason", [""])[0]
            try:
                self._json({
                    "ok": True,
                    "import": pipeline.archive_import(self.runs_root, task_id, import_id, reason=reason),
                })
            except Exception as exc:
                self._json({"error": str(exc)}, status=400)
        elif path == "/api/sample":
            if _control_task_source_enabled():
                self._json({"error": "control 模式暂不提供文件资产归档入口"}, status=400)
                return
            task_id = params.get("task_id", [""])[0]
            sample_id = params.get("sample_id", [""])[0]
            reason = params.get("reason", [""])[0]
            try:
                self._json({
                    "ok": True,
                    "sample": pipeline.archive_sample(self.runs_root, task_id, sample_id, reason=reason),
                })
            except Exception as exc:
                self._json({"error": str(exc)}, status=400)
        elif path == "/api/annotation_job":
            if _control_task_source_enabled():
                self._json({"error": "control 模式暂不提供文件资产归档入口"}, status=400)
                return
            task_id = params.get("task_id", [""])[0]
            annotation_id = params.get("annotation_id", [""])[0]
            reason = params.get("reason", [""])[0]
            try:
                self._json({
                    "ok": True,
                    "annotation_job": pipeline.archive_annotation_job(self.runs_root, task_id, annotation_id, reason=reason),
                })
            except Exception as exc:
                self._json({"error": str(exc)}, status=400)
        else:
            self._json({"error": "not found"}, status=404)

    def _import(self, params) -> None:
        task = params.get("task_id", params.get("task", [""]))[0]
        name = params.get("name", ["imported"])[0]
        if not _safe_segment(task) or not _safe_segment(name):
            self._json({"error": "bad task/name"}, status=400)
            return
        if not _allow_manual_imports():
            self._json({"error": "生产模式不允许手动上传或粘贴导入；请使用 task.yaml 中的 data_lake 配置从数据湖导入"}, status=400)
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        max_bytes = int(os.environ.get("LLS_MAX_IMPORT_BYTES", str(100 * 1024 * 1024)))
        if length <= 0:
            self._json({"error": "上传内容为空"}, status=400)
            return
        if length > max_bytes:
            self._json({"error": f"上传内容过大，当前上限为 {max_bytes} bytes"}, status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            rows = parse_import_rows(raw.decode("utf-8", "replace"))
        except ValueError as exc:
            self._json({"error": str(exc)}, status=400)
            return
        try:
            task_cfg, task_runs_root, _ = self._task_context(
                task,
                params,
                require_task_config=True,
            )
            result = pipeline.save_import(task_runs_root, task_cfg, name, rows, source="upload")
            self._json({"ok": True, "import": result})
        except Exception as exc:
            self._route_error(exc)

    def _import_data_lake(self, params) -> None:
        body = self._read_body()
        if not isinstance(body, dict):
            body = {}
        task_id = str(body.get("task_id") or params.get("task_id", [""])[0])
        if not _safe_segment(task_id):
            self._json({"error": "bad task"}, status=400)
            return
        import_id = str(body.get("import_id") or params.get("import_id", [""])[0] or "").strip()
        dry_run_value = body.get("dry_run", body.get("dryRun", params.get("dry_run", [""])[0]))
        dry_run = _truthy_value(dry_run_value)
        if self._is_mcp_request() and not dry_run and not _mcp_writes_enabled():
            self._json({"error": "当前部署未启用 MCP 写操作"}, status=HTTPStatus.FORBIDDEN)
            return
        override_keys = (
            "lake_registry_uri",
            "source_dataset_id",
            "source_manifest_uri",
            "source_object_path",
        )
        provided_overrides = {key: body.get(key) for key in override_keys if body.get(key) not in (None, "")}
        if provided_overrides and not _allow_data_lake_overrides():
            self._json({"error": "生产模式不允许覆盖数据湖来源；请在 task.yaml 中使用治理登记表配置"}, status=400)
            return
        overrides = provided_overrides if _allow_data_lake_overrides() else {}
        idempotency_key = str(body.get("idempotency_key") or self.headers.get("Idempotency-Key") or "").strip()
        if not dry_run:
            if not _truthy_value(body.get("confirm")):
                self._json({"error": "数据湖导入提交必须显式设置 confirm=true"}, status=400)
                return
            if not _valid_idempotency_key(idempotency_key):
                self._json({"error": "数据湖导入提交必须提供有效的 idempotency_key 或 Idempotency-Key header"}, status=400)
                return
        max_bytes = int(os.environ.get("LLS_MAX_IMPORT_BYTES", str(100 * 1024 * 1024)))
        try:
            _apply_runtime_settings(self.runs_root)
            task_cfg = self._load_task_by_id(task_id)
            if dry_run:
                dry_run_result = pipeline.dry_run_data_lake_import(
                    self.runs_root,
                    task_cfg,
                    import_id=import_id or None,
                    overrides=overrides,
                    max_bytes=max_bytes,
                )
                self._json({"ok": bool(dry_run_result["validation"]["ok"]), "dry_run": True, "result": dry_run_result})
                return
            job = pipeline.start_data_lake_import(
                self.runs_root,
                task_cfg,
                import_id=import_id or None,
                overrides=overrides,
                max_bytes=max_bytes,
                idempotency_key=idempotency_key,
            )
            self._json({"ok": True, "job": job})
        except Exception as exc:
            self._json({"error": str(exc)}, status=400)

    def _import_suggestions(self, params) -> None:
        task_id = params.get("task_id", [""])[0]
        annotation_id = params.get("annotation_id", [""])[0]
        suggestion_id = params.get("suggestion_id", [""])[0]
        provider = params.get("provider", ["external"])[0] or "external"
        prompt_version = params.get("prompt_version", ["v001"])[0] or "v001"
        publish = _truthy_value(params.get("publish", [""])[0])
        if not _safe_segment(task_id) or not _safe_segment(annotation_id) or not _safe_segment(suggestion_id):
            self._json({"error": "bad task/annotation/suggestion"}, status=400)
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        max_bytes = int(os.environ.get("LLS_MAX_SUGGESTIONS_BYTES", str(50 * 1024 * 1024)))
        if length <= 0:
            self._json({"error": "上传内容为空"}, status=400)
            return
        if length > max_bytes:
            self._json({"error": f"上传内容过大，当前上限为 {max_bytes} bytes"}, status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        raw = self.rfile.read(length)
        try:
            rows = parse_import_rows(raw.decode("utf-8", "replace"))
        except ValueError as exc:
            self._json({"error": str(exc)}, status=400)
            return
        try:
            from .suggestions import import_external_suggestions

            task_cfg, task_runs_root, _ = self._task_context(
                task_id,
                params,
                require_task_config=True,
            )
            result = import_external_suggestions(
                task_runs_root,
                task_cfg,
                annotation_id,
                suggestion_id,
                rows,
                provider=provider,
                prompt_version=prompt_version,
                publish=publish,
            )
            self._json({"ok": True, "suggestions": result})
        except Exception as exc:
            self._json({"error": str(exc)}, status=400)

    def _download_import(self, params) -> None:
        task = params.get("task_id", [""])[0]
        import_id = params.get("import_id", [""])[0]
        if not _safe_segment(task) or not _safe_segment(import_id):
            self._json({"error": "bad task/import"}, status=400)
            return
        try:
            _, task_runs_root, _ = self._task_context(task, params)
        except Exception as exc:
            self._route_error(exc)
            return
        path = task_runs_root / task / "imports" / import_id / "raw.jsonl"
        if not path.exists():
            self._json({"error": "no data"}, status=404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{import_id}.jsonl"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _download_suggestions(self, params) -> None:
        task = params.get("task_id", [""])[0]
        annotation_id = params.get("annotation_id", [""])[0]
        suggestion_id = params.get("suggestion_id", [""])[0]
        kind = params.get("kind", ["template"])[0] or "template"
        if not _safe_segment(task) or not _safe_segment(annotation_id) or not _safe_segment(suggestion_id):
            self._json({"error": "bad task/annotation/suggestion"}, status=400)
            return
        files = {
            "template": ("suggestions_template.jsonl", "application/x-ndjson; charset=utf-8"),
            "suggestions": ("suggestions.jsonl", "application/x-ndjson; charset=utf-8"),
            "schema": ("schema.json", "application/json; charset=utf-8"),
        }
        if kind not in files:
            self._json({"error": "unknown suggestions download kind"}, status=400)
            return
        filename, content_type = files[kind]
        try:
            _, task_runs_root, _ = self._task_context(task, params)
        except Exception as exc:
            self._route_error(exc)
            return
        path = task_runs_root / task / "suggestions" / annotation_id / suggestion_id / filename
        if not path.is_file():
            self._json({"error": "no data"}, status=404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{suggestion_id}_{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve_panel(
    runs_root: str | Path = "runs",
    host: str = "127.0.0.1",
    port: int = 8765,
    user: str = "admin",
    password: str | None = None,
    static_dir: str | Path | None = None,
    tasks_root: str | Path = "tasks",
    auth_mode: str | None = None,
    active_task_loader: ActiveTaskLoader | None = None,
    deployment_mode: str | None = None,
) -> None:
    host = _normalize_bind_host(host)
    resolved_auth_mode = resolve_panel_auth_mode(auth_mode)
    resolved_deployment_mode = _resolve_panel_deployment_mode(deployment_mode)
    _validate_panel_startup(
        host=host,
        auth_mode=resolved_auth_mode,
        deployment_mode=resolved_deployment_mode,
    )
    authenticator = build_panel_authenticator(
        mode=resolved_auth_mode,
        basic_user=user,
        basic_password=password,
    )
    _Handler.runs_root = Path(runs_root)
    _Handler.tasks_root = Path(tasks_root)
    _Handler.authenticator = authenticator
    owned_active_task_loader = False
    if _control_task_source_enabled() and active_task_loader is None:
        try:
            active_task_loader = ControlTaskSnapshotLoader.from_url(
                None,
                runs_root,
                tasks_root,
            )
            owned_active_task_loader = True
        except Exception:
            active_task_loader = None
    try:
        authorization_service = _build_authorization_service()
    except Exception:
        authorization_service = None
    allocation_repository = None
    if authorization_service is not None and _control_task_source_enabled():
        try:
            allocation_repository = TrustedAllocationRepository.from_url()
        except Exception:
            allocation_repository = None
    annotator_repository = None
    if authorization_service is not None and _control_task_source_enabled():
        try:
            annotator_repository = AnnotatorControlRepository.from_url()
        except Exception:
            annotator_repository = None
    annotator_adapter = None
    if _control_task_source_enabled():
        try:
            annotator_adapter = ArgillaAdminAdapter()
        except Exception:
            annotator_adapter = None
    _Handler.authorization_service = authorization_service
    _Handler.allocation_repository = allocation_repository
    _Handler.annotator_repository = annotator_repository
    _Handler.annotator_adapter = annotator_adapter
    _Handler.authorization_ready = _authorization_runtime_ready(authorization_service, active_task_loader)
    _Handler.active_task_loader = active_task_loader
    if static_dir is None:
        guess = Path("frontend/dist")
        static_dir = guess if guess.is_dir() else None
    _Handler.static_dir = Path(static_dir) if static_dir else None
    httpd = _panel_server_type(host)((host, port), _Handler)
    display_host = _display_bind_host(host)
    print(
        f"[lls panel] serving on http://{display_host}:{port} "
        f"(auth mode='{authenticator.mode}', deployment mode='{resolved_deployment_mode}')"
    )
    if _Handler.static_dir:
        print(f"[lls panel] serving frontend from {_Handler.static_dir}")
    else:
        print("[lls panel] no frontend build found; run 'npm run build' in frontend/ or use the Vite dev server")
    if not _Handler.authorization_ready:
        print("[lls panel] authorization runtime unavailable; protected routes fail closed")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        if allocation_repository is not None:
            allocation_repository.close()
        if annotator_repository is not None:
            annotator_repository.close()
        if authorization_service is not None:
            authorization_service.close()
        if owned_active_task_loader and active_task_loader is not None:
            active_task_loader.close()
