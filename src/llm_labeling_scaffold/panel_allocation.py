from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .allocation import (
    AllocationPreview,
    AllocationRequest,
    AllocationStrategy,
    AnnotatorSpec,
    CalibrationRule,
    ManifestKind,
    OverlapRule,
    RecordSpec,
    SourceManifestRef,
    TaskRevisionRef,
    preview_allocation as _preview_allocation,
)


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_IDENTIFIER_LENGTH = 255
_MAX_ALGORITHM_VERSION_LENGTH = 64
_TOP_LEVEL_FIELDS = frozenset({"scope", "workspace", "task_id", "revision_id", "revision_hash", "request"})
_SCOPE_FIELDS = frozenset({"workspace", "task_id", "revision_id", "revision_hash"})
_REQUEST_FIELDS = frozenset(
    {
        "strategy",
        "task_revision",
        "source_manifest",
        "records",
        "annotators",
        "seed",
        "algorithm_version",
        "overlap_rules",
        "calibration",
    }
)
_FORBIDDEN_IDENTITY_FIELDS = frozenset(
    {
        "actor",
        "actor_id",
        "caller",
        "caller_id",
        "channel",
        "audit_channel",
        "permission",
        "permissions",
        "permission_context",
        "authorization",
    }
)
_FORBIDDEN_SECRET_FIELDS = frozenset(
    {
        "password",
        "passwd",
        "api_key",
        "apikey",
        "secret",
        "token",
        "access_token",
        "owner_token",
        "credential",
        "credentials",
    }
)
_FORBIDDEN_PATH_FIELDS = frozenset(
    {
        "file",
        "filename",
        "file_path",
        "filepath",
        "manifest_path",
        "path",
        "source_path",
    }
)


class AllocationPreviewDTOError(ValueError):
    def __init__(self, code: str, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field


@dataclass(frozen=True, slots=True)
class AllocationPreviewRequest:
    workspace: str
    allocation_request: AllocationRequest

    @property
    def task_id(self) -> str:
        return self.allocation_request.task_revision.task_id

    @property
    def revision_id(self) -> str:
        return self.allocation_request.task_revision.revision_id

    @property
    def revision_hash(self) -> str:
        return self.allocation_request.task_revision.revision_hash


def parse_allocation_preview_json(raw: str | bytes) -> AllocationPreviewRequest:
    try:
        payload = json.loads(raw)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AllocationPreviewDTOError("invalid_json", "请求体必须是合法 JSON") from exc
    return parse_allocation_preview_request(payload)


def parse_allocation_preview_request(payload: Any) -> AllocationPreviewRequest:
    root = _mapping(payload, "request")
    _reject_forbidden_fields(root)
    _ensure_fields(root, _TOP_LEVEL_FIELDS, "request")
    workspace, task_id, revision_id, revision_hash = _parse_scope(root)
    request_payload = _mapping(root.get("request"), "request.request")
    _reject_forbidden_fields(request_payload)
    _ensure_fields(request_payload, _REQUEST_FIELDS, "request.request")

    task_revision = TaskRevisionRef(
        task_id=task_id,
        revision_id=revision_id,
        revision_hash=revision_hash,
    )
    submitted_task_revision = request_payload.get("task_revision")
    if submitted_task_revision is not None:
        parsed_task_revision = _parse_task_revision(submitted_task_revision, "request.request.task_revision")
        if parsed_task_revision != task_revision:
            raise AllocationPreviewDTOError(
                "scope_revision_conflict",
                "scope 与 request.task_revision 必须完全一致",
                field="request.task_revision",
            )

    allocation_request = AllocationRequest(
        strategy=_parse_strategy(request_payload.get("strategy"), "request.request.strategy"),
        task_revision=task_revision,
        source_manifest=_parse_source_manifest(
            request_payload.get("source_manifest"),
            "request.request.source_manifest",
        ),
        records=_parse_records(request_payload.get("records"), "request.request.records"),
        annotators=_parse_annotators(
            request_payload.get("annotators"),
            "request.request.annotators",
        ),
        seed=_integer(request_payload.get("seed"), "request.request.seed"),
        algorithm_version=_algorithm_version(
            request_payload.get("algorithm_version"),
            "request.request.algorithm_version",
        ),
        overlap_rules=_parse_overlap_rules(
            request_payload.get("overlap_rules", []),
            "request.request.overlap_rules",
        ),
        calibration=_parse_calibration(
            request_payload.get("calibration"),
            "request.request.calibration",
        ),
    )
    return AllocationPreviewRequest(workspace=workspace, allocation_request=allocation_request)


def preview_allocation_dto(value: AllocationPreviewRequest) -> dict[str, Any]:
    if not isinstance(value, AllocationPreviewRequest):
        raise TypeError("value must be an AllocationPreviewRequest")
    preview = _preview_allocation(value.allocation_request)
    return serialize_allocation_preview(value, preview)


def preview_allocation_payload(payload: Any) -> dict[str, Any]:
    return preview_allocation_dto(parse_allocation_preview_request(payload))


def serialize_allocation_preview(
    request: AllocationPreviewRequest,
    preview: AllocationPreview,
) -> dict[str, Any]:
    if not isinstance(request, AllocationPreviewRequest):
        raise TypeError("request must be an AllocationPreviewRequest")
    if not isinstance(preview, AllocationPreview):
        raise TypeError("preview must be an AllocationPreview")
    return {
        "schema_version": "panel_allocation_preview_v1",
        "scope": {
            "workspace": request.workspace,
            "task_id": request.task_id,
            "revision_id": request.revision_id,
            "revision_hash": request.revision_hash,
        },
        "ready": preview.ready,
        "strategy": _enum_value(preview.strategy),
        "source_manifest": _source_manifest(preview.source_manifest),
        "cohort_id": preview.cohort_id,
        "algorithm_version": preview.algorithm_version,
        "input_fingerprint": preview.input_fingerprint,
        "plan_fingerprint": preview.plan_fingerprint,
        "annotator_loads": [
            {
                "annotator_id": item.annotator_id,
                "mode": _enum_value(item.mode),
                "capacity": item.capacity,
                "assigned_rows": item.assigned_rows,
                "calibration_rows": item.calibration_rows,
                "production_rows": item.production_rows,
                "advisory_reserved_shared_rows": item.advisory_reserved_shared_rows,
                "remaining_capacity": item.remaining_capacity,
            }
            for item in preview.annotator_loads
        ],
        "calibration_set": (
            {
                "record_ids": list(preview.calibration_set.record_ids),
                "expected_annotator_ids": list(preview.calibration_set.expected_annotator_ids),
                "expected_assignment_rows": preview.calibration_set.expected_assignment_rows,
                "gate_id": preview.calibration_set.gate_id,
            }
            if preview.calibration_set is not None
            else None
        ),
        "overlap": {
            "mode": _enum_value(preview.overlap.mode),
            "cells": [
                {
                    "phase": _enum_value(item.phase),
                    "annotator_a": item.annotator_a,
                    "annotator_b": item.annotator_b,
                    "count": item.count,
                }
                for item in preview.overlap.cells
            ],
            "pool_requirements": [
                {
                    "phase": _enum_value(item.phase),
                    "pool_id": item.pool_id,
                    "required_submissions": item.required_submissions,
                    "record_count": item.record_count,
                }
                for item in preview.overlap.pool_requirements
            ],
        },
        "workspace_requirements": [
            {
                "workspace_group_id": item.workspace_group_id,
                "phase": _enum_value(item.phase),
                "mode": _enum_value(item.mode),
                "cohort_id": item.cohort_id,
                "assignee_id": item.assignee_id,
                "member_annotator_ids": list(item.member_annotator_ids),
            }
            for item in preview.workspace_requirements
        ],
        "dataset_requirements": [
            {
                "dataset_group_id": item.dataset_group_id,
                "workspace_group_id": item.workspace_group_id,
                "phase": _enum_value(item.phase),
                "min_submitted": item.min_submitted,
                "assignee_id": item.assignee_id,
                "pool_id": item.pool_id,
                "record_count": item.record_count,
                "batch_count": item.batch_count,
            }
            for item in preview.dataset_requirements
        ],
        "blocking_errors": [_validation_issue(item) for item in preview.blocking_errors],
        "warnings": [_validation_issue(item) for item in preview.warnings],
    }


def serialize_allocation_preview_json(
    request: AllocationPreviewRequest,
    preview: AllocationPreview,
) -> str:
    return json.dumps(
        serialize_allocation_preview(request, preview),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _parse_scope(root: Mapping[str, Any]) -> tuple[str, str, str, str]:
    scope = root.get("scope")
    if scope is not None:
        _ensure_fields(root, {"scope", "request"}, "request")
        scope_payload = _mapping(scope, "request.scope")
        _reject_forbidden_fields(scope_payload)
        _ensure_fields(scope_payload, _SCOPE_FIELDS, "request.scope")
        source = scope_payload
        field_prefix = "request.scope"
    else:
        _ensure_fields(
            root,
            {"workspace", "task_id", "revision_id", "revision_hash", "request"},
            "request",
        )
        source = root
        field_prefix = "request"
    required = ("workspace", "task_id", "revision_id", "revision_hash")
    for name in required:
        if name not in source:
            raise AllocationPreviewDTOError(
                "missing_field",
                f"缺少必填字段: {name}",
                field=f"{field_prefix}.{name}",
            )
    return (
        _identifier(source["workspace"], f"{field_prefix}.workspace"),
        _identifier(source["task_id"], f"{field_prefix}.task_id"),
        _identifier(source["revision_id"], f"{field_prefix}.revision_id"),
        _sha256(source["revision_hash"], f"{field_prefix}.revision_hash"),
    )


def _parse_task_revision(value: Any, field: str) -> TaskRevisionRef:
    payload = _mapping(value, field)
    _reject_forbidden_fields(payload)
    _ensure_fields(payload, {"task_id", "revision_id", "revision_hash"}, field)
    for name in ("task_id", "revision_id", "revision_hash"):
        if name not in payload:
            raise AllocationPreviewDTOError("missing_field", f"缺少必填字段: {name}", field=f"{field}.{name}")
    return TaskRevisionRef(
        task_id=_identifier(payload["task_id"], f"{field}.task_id"),
        revision_id=_identifier(payload["revision_id"], f"{field}.revision_id"),
        revision_hash=_sha256(payload["revision_hash"], f"{field}.revision_hash"),
    )


def _parse_strategy(value: Any, field: str) -> AllocationStrategy:
    text = _text(value, field)
    try:
        return AllocationStrategy(text)
    except ValueError as exc:
        raise AllocationPreviewDTOError("invalid_strategy", "strategy 不是受支持的值", field=field) from exc


def _parse_source_manifest(value: Any, field: str) -> SourceManifestRef:
    payload = _mapping(value, field)
    _reject_forbidden_fields(payload)
    _ensure_fields(payload, {"manifest_id", "manifest_hash", "kind"}, field)
    if "manifest_id" not in payload or "manifest_hash" not in payload:
        raise AllocationPreviewDTOError("missing_field", "source_manifest 缺少必填字段", field=field)
    kind_value = payload.get("kind", ManifestKind.SAMPLE.value)
    try:
        kind = ManifestKind(_text(kind_value, f"{field}.kind"))
    except ValueError as exc:
        raise AllocationPreviewDTOError("invalid_manifest_kind", "manifest kind 不是受支持的值", field=f"{field}.kind") from exc
    return SourceManifestRef(
        manifest_id=_identifier(payload["manifest_id"], f"{field}.manifest_id"),
        manifest_hash=_sha256(payload["manifest_hash"], f"{field}.manifest_hash"),
        kind=kind,
    )


def _parse_records(value: Any, field: str) -> tuple[RecordSpec, ...]:
    values = _list(value, field)
    records: list[RecordSpec] = []
    for index, item in enumerate(values):
        item_field = f"{field}[{index}]"
        payload = _mapping(item, item_field)
        _reject_forbidden_fields(payload)
        _ensure_fields(payload, {"record_id", "content_hash", "batch_id"}, item_field)
        if "record_id" not in payload or "content_hash" not in payload:
            raise AllocationPreviewDTOError("missing_field", "record 缺少必填字段", field=item_field)
        records.append(
            RecordSpec(
                record_id=_identifier(payload["record_id"], f"{item_field}.record_id"),
                content_hash=_sha256(payload["content_hash"], f"{item_field}.content_hash"),
                batch_id=_identifier(payload.get("batch_id", "default"), f"{item_field}.batch_id"),
            )
        )
    return tuple(records)


def _parse_annotators(value: Any, field: str) -> tuple[AnnotatorSpec, ...]:
    values = _list(value, field)
    annotators: list[AnnotatorSpec] = []
    for index, item in enumerate(values):
        item_field = f"{field}[{index}]"
        payload = _mapping(item, item_field)
        _reject_forbidden_fields(payload)
        _ensure_fields(payload, {"annotator_id", "cohort_id", "capacity"}, item_field)
        required = ("annotator_id", "cohort_id", "capacity")
        if any(name not in payload for name in required):
            raise AllocationPreviewDTOError("missing_field", "annotator 缺少必填字段", field=item_field)
        annotators.append(
            AnnotatorSpec(
                annotator_id=_identifier(payload["annotator_id"], f"{item_field}.annotator_id"),
                cohort_id=_identifier(payload["cohort_id"], f"{item_field}.cohort_id"),
                capacity=_integer(payload["capacity"], f"{item_field}.capacity"),
            )
        )
    return tuple(annotators)


def _parse_overlap_rules(value: Any, field: str) -> tuple[OverlapRule, ...]:
    values = _list(value, field)
    rules: list[OverlapRule] = []
    for index, item in enumerate(values):
        item_field = f"{field}[{index}]"
        payload = _mapping(item, item_field)
        _reject_forbidden_fields(payload)
        _ensure_fields(payload, {"record_ids", "count", "rate", "required_submissions"}, item_field)
        rules.append(
            OverlapRule(
                record_ids=_identifier_list(payload.get("record_ids", []), f"{item_field}.record_ids"),
                count=_optional_integer(payload.get("count"), f"{item_field}.count"),
                rate=_optional_rate(payload.get("rate"), f"{item_field}.rate"),
                required_submissions=_integer(
                    payload.get("required_submissions", 2),
                    f"{item_field}.required_submissions",
                ),
            )
        )
    return tuple(rules)


def _parse_calibration(value: Any, field: str) -> CalibrationRule | None:
    if value is None:
        return None
    payload = _mapping(value, field)
    _reject_forbidden_fields(payload)
    _ensure_fields(
        payload,
        {"record_ids", "count", "rate", "expected_annotator_ids", "exclude_from_production"},
        field,
    )
    expected = payload.get("expected_annotator_ids")
    expected_ids = None if expected is None else _identifier_list(expected, f"{field}.expected_annotator_ids")
    exclude = payload.get("exclude_from_production", True)
    if not isinstance(exclude, bool):
        raise AllocationPreviewDTOError(
            "invalid_type",
            "exclude_from_production 必须是布尔值",
            field=f"{field}.exclude_from_production",
        )
    return CalibrationRule(
        record_ids=_identifier_list(payload.get("record_ids", []), f"{field}.record_ids"),
        count=_optional_integer(payload.get("count"), f"{field}.count"),
        rate=_optional_rate(payload.get("rate"), f"{field}.rate"),
        expected_annotator_ids=expected_ids,
        exclude_from_production=exclude,
    )


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AllocationPreviewDTOError("invalid_type", "必须是 JSON 对象", field=field)
    if any(not isinstance(key, str) for key in value):
        raise AllocationPreviewDTOError("invalid_field", "JSON 对象字段名必须是字符串", field=field)
    return value


def _list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise AllocationPreviewDTOError("invalid_type", "必须是 JSON 数组", field=field)
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise AllocationPreviewDTOError("invalid_type", "必须是字符串", field=field)
    value = value.strip()
    if not value:
        raise AllocationPreviewDTOError("invalid_value", "字符串不能为空", field=field)
    if any(ord(char) < 32 for char in value):
        raise AllocationPreviewDTOError("invalid_value", "字符串不能包含控制字符", field=field)
    return value


def _identifier(value: Any, field: str) -> str:
    value = _text(value, field)
    if len(value) > _MAX_IDENTIFIER_LENGTH or value in {".", ".."} or ".." in value:
        raise AllocationPreviewDTOError("invalid_identifier", "标识符不是安全的单段值", field=field)
    if "/" in value or "\\" in value:
        raise AllocationPreviewDTOError("path_forbidden", "不接受文件路径或路径分隔符", field=field)
    return value


def _sha256(value: Any, field: str) -> str:
    value = _text(value, field)
    if _SHA256_RE.fullmatch(value) is None:
        raise AllocationPreviewDTOError("invalid_hash", "必须是小写 SHA-256 摘要", field=field)
    return value


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AllocationPreviewDTOError("invalid_type", "必须是整数", field=field)
    return value


def _optional_integer(value: Any, field: str) -> int | None:
    return None if value is None else _integer(value, field)


def _optional_rate(value: Any, field: str) -> float | int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise AllocationPreviewDTOError("invalid_type", "必须是有限数字", field=field)
    return value


def _identifier_list(value: Any, field: str) -> tuple[str, ...]:
    return tuple(_identifier(item, f"{field}[{index}]") for index, item in enumerate(_list(value, field)))


def _algorithm_version(value: Any, field: str) -> str:
    value = _text(value, field)
    if len(value) > _MAX_ALGORITHM_VERSION_LENGTH or "/" in value or "\\" in value:
        raise AllocationPreviewDTOError("invalid_identifier", "algorithm_version 不是安全标识符", field=field)
    return value


def _ensure_fields(value: Mapping[str, Any], allowed: set[str] | frozenset[str], field: str) -> None:
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise AllocationPreviewDTOError(
            "unknown_field",
            f"字段不在允许的 DTO 白名单中: {', '.join(unknown)}",
            field=field,
        )


def _reject_forbidden_fields(value: Mapping[str, Any], path: str = "request") -> None:
    for key, item in value.items():
        normalized = key.lower().replace("-", "_")
        if normalized in _FORBIDDEN_IDENTITY_FIELDS:
            raise AllocationPreviewDTOError(
                "server_context_forbidden",
                "actor、caller、channel 和权限上下文由服务端确定",
                field=f"{path}.{key}",
            )
        if normalized in _FORBIDDEN_SECRET_FIELDS:
            raise AllocationPreviewDTOError(
                "sensitive_input_forbidden",
                "不接受密码、API key 或其他凭证",
                field=f"{path}.{key}",
            )
        if normalized in _FORBIDDEN_PATH_FIELDS:
            raise AllocationPreviewDTOError(
                "path_forbidden",
                "不接受任意文件路径",
                field=f"{path}.{key}",
            )
        if isinstance(item, Mapping):
            _reject_forbidden_fields(item, f"{path}.{key}")
        elif isinstance(item, list):
            for index, nested in enumerate(item):
                if isinstance(nested, Mapping):
                    _reject_forbidden_fields(nested, f"{path}.{key}[{index}]")


def _enum_value(value: Enum | None) -> str | None:
    return value.value if value is not None else None


def _source_manifest(value: SourceManifestRef | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {
        "manifest_id": value.manifest_id,
        "manifest_hash": value.manifest_hash,
        "kind": _enum_value(value.kind),
    }


def _validation_issue(value) -> dict[str, Any]:
    return {
        "code": value.code,
        "field": value.field,
        "message": value.message,
        "context": {key: item for key, item in value.context},
    }
