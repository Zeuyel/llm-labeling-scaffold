from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from typing import Any, Mapping, Sequence


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class AllocationStrategy(str, Enum):
    SHARED_QUEUE = "shared_queue"
    FIXED_PARTITION = "fixed_partition"
    CALIBRATION_THEN_PARTITION = "calibration_then_partition"


class ManifestKind(str, Enum):
    SAMPLE = "sample"
    BATCH = "batch"


class AssignmentPhase(str, Enum):
    CALIBRATION = "calibration"
    PRODUCTION = "production"


class AssignmentRole(str, Enum):
    CALIBRATION = "calibration"
    PRIMARY = "primary"
    OVERLAP = "overlap"
    SHARED = "shared"


class WorkspaceMode(str, Enum):
    CALIBRATION = "calibration"
    PERSONAL = "personal"
    SHARED = "shared"


class OverlapMatrixMode(str, Enum):
    ASSIGNEE = "assignee"
    POOL = "pool"


@dataclass(frozen=True, slots=True)
class TaskRevisionRef:
    task_id: str
    revision_id: str
    revision_hash: str


@dataclass(frozen=True, slots=True)
class SourceManifestRef:
    manifest_id: str
    manifest_hash: str
    kind: ManifestKind = ManifestKind.SAMPLE


@dataclass(frozen=True, slots=True)
class RecordSpec:
    record_id: str
    content_hash: str
    batch_id: str = "default"


@dataclass(frozen=True, slots=True)
class AnnotatorSpec:
    annotator_id: str
    cohort_id: str
    capacity: int


@dataclass(frozen=True, slots=True)
class OverlapRule:
    record_ids: tuple[str, ...] = ()
    count: int | None = None
    rate: float | None = None
    assignees_per_record: int = 2


@dataclass(frozen=True, slots=True)
class CalibrationRule:
    record_ids: tuple[str, ...] = ()
    count: int | None = None
    rate: float | None = None
    expected_annotator_ids: tuple[str, ...] | None = None
    exclude_from_production: bool = True


@dataclass(frozen=True, slots=True)
class AllocationRequest:
    strategy: AllocationStrategy
    task_revision: TaskRevisionRef
    source_manifest: SourceManifestRef
    records: tuple[RecordSpec, ...]
    annotators: tuple[AnnotatorSpec, ...]
    seed: int
    algorithm_version: str
    overlap: OverlapRule = OverlapRule()
    calibration: CalibrationRule | None = None


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    code: str
    field: str
    message: str
    context: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class ValidationReport:
    issues: tuple[ValidationIssue, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.issues


class AllocationValidationError(ValueError):
    def __init__(self, report: ValidationReport):
        self.report = report
        first = report.issues[0] if report.issues else None
        message = "allocation request is invalid"
        if first is not None:
            message = f"{message}: {first.code} at {first.field}"
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class Assignment:
    assignment_id: str
    record_id: str
    record_content_hash: str
    phase: AssignmentPhase
    batch_id: str
    role: AssignmentRole
    workspace_group_id: str
    dataset_group_id: str
    assignee_id: str | None
    pool_id: str | None
    required_submissions: int
    gate_id: str | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceGroup:
    workspace_group_id: str
    phase: AssignmentPhase
    mode: WorkspaceMode
    cohort_id: str
    assignee_id: str | None
    member_annotator_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DatasetGroup:
    dataset_group_id: str
    workspace_group_id: str
    phase: AssignmentPhase
    min_submitted: int
    assignee_id: str | None
    pool_id: str | None
    assignment_ids: tuple[str, ...]
    record_ids: tuple[str, ...]
    batch_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AnnotatorLoad:
    annotator_id: str
    cohort_id: str
    capacity: int
    calibration_rows: int
    production_rows: int
    assigned_rows: int
    reserved_shared_rows: int
    eligible_shared_rows: int
    remaining_capacity: int


@dataclass(frozen=True, slots=True)
class OverlapCell:
    phase: AssignmentPhase
    annotator_a: str
    annotator_b: str
    record_ids: tuple[str, ...]
    count: int


@dataclass(frozen=True, slots=True)
class PoolOverlapRequirement:
    phase: AssignmentPhase
    pool_id: str
    record_id: str
    required_submissions: int


@dataclass(frozen=True, slots=True)
class OverlapMatrix:
    mode: OverlapMatrixMode
    cells: tuple[OverlapCell, ...]
    pool_requirements: tuple[PoolOverlapRequirement, ...]


@dataclass(frozen=True, slots=True)
class CalibrationExpectation:
    record_id: str
    expected_annotator_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AllocationGate:
    gate_id: str
    gate_kind: str
    blocks_phase: AssignmentPhase
    task_revision_hash: str
    source_manifest_hash: str
    cohort_id: str
    expectations: tuple[CalibrationExpectation, ...]
    plan_fingerprint: str


@dataclass(frozen=True, slots=True)
class AllocationPlan:
    schema_version: str
    strategy: AllocationStrategy
    task_revision: TaskRevisionRef
    source_manifest: SourceManifestRef
    seed: int
    algorithm_version: str
    input_fingerprint: str
    fingerprint: str
    assignments: tuple[Assignment, ...]
    workspace_groups: tuple[WorkspaceGroup, ...]
    dataset_groups: tuple[DatasetGroup, ...]
    annotator_loads: tuple[AnnotatorLoad, ...]
    overlap_matrix: OverlapMatrix
    gates: tuple[AllocationGate, ...]
    unsatisfied_constraints: tuple[ValidationIssue, ...]

    def fingerprint_payload(self) -> dict[str, Any]:
        payload = canonical_data(self)
        payload["fingerprint"] = ""
        for gate in payload["gates"]:
            gate["plan_fingerprint"] = ""
        return payload

    def to_dict(self) -> dict[str, Any]:
        return canonical_data(self)


@dataclass(frozen=True, slots=True)
class _ResolvedRequest:
    request: AllocationRequest
    cohort_id: str
    calibration_record_ids: tuple[str, ...]
    production_record_ids: tuple[str, ...]
    overlap_record_ids: tuple[str, ...]
    expected_calibration_annotator_ids: tuple[str, ...]
    input_fingerprint: str


def canonical_data(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: canonical_data(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        data: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("canonical mappings require string keys")
            data[key] = canonical_data(item)
        return data
    if isinstance(value, (tuple, list)):
        return [canonical_data(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError(f"unsupported canonical value: {type(value).__name__}")


def canonical_hash(value: Any) -> str:
    try:
        encoded = json.dumps(
            canonical_data(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("value must contain canonical JSON data") from exc
    normalized = unicodedata.normalize("NFC", encoded)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def validate_allocation_request(request: AllocationRequest) -> ValidationReport:
    _, issues = _resolve_request(request)
    return ValidationReport(issues=issues)


def _resolve_request(
    request: AllocationRequest,
) -> tuple[_ResolvedRequest | None, tuple[ValidationIssue, ...]]:
    issues: list[ValidationIssue] = []
    if not isinstance(request, AllocationRequest):
        return None, (_issue("invalid_request", "request", "request must be an AllocationRequest"),)

    if not isinstance(request.strategy, AllocationStrategy):
        issues.append(_issue("invalid_strategy", "strategy", "strategy is not supported"))
    if not isinstance(request.task_revision, TaskRevisionRef):
        issues.append(
            _issue("invalid_task_revision", "task_revision", "task_revision must be a TaskRevisionRef")
        )
    if not isinstance(request.source_manifest, SourceManifestRef):
        issues.append(
            _issue("invalid_source_manifest", "source_manifest", "source_manifest must be a SourceManifestRef")
        )
    if not isinstance(request.records, tuple):
        issues.append(_issue("mutable_collection", "records", "records must be a tuple"))
    if not isinstance(request.annotators, tuple):
        issues.append(_issue("mutable_collection", "annotators", "annotators must be a tuple"))
    if isinstance(request.seed, bool) or not isinstance(request.seed, int):
        issues.append(_issue("invalid_seed", "seed", "seed must be an integer"))
    if not _clean(request.algorithm_version):
        issues.append(
            _issue("invalid_algorithm_version", "algorithm_version", "algorithm_version must not be blank")
        )
    if not isinstance(request.overlap, OverlapRule):
        issues.append(_issue("invalid_overlap_rule", "overlap", "overlap must be an OverlapRule"))
    if request.calibration is not None and not isinstance(request.calibration, CalibrationRule):
        issues.append(
            _issue("invalid_calibration_rule", "calibration", "calibration must be a CalibrationRule or None")
        )
    if issues:
        return None, tuple(issues)

    task_revision = request.task_revision
    source_manifest = request.source_manifest
    if not _clean(task_revision.task_id):
        issues.append(_issue("blank_identifier", "task_revision.task_id", "task_id must not be blank"))
    if not _clean(task_revision.revision_id):
        issues.append(
            _issue("blank_identifier", "task_revision.revision_id", "revision_id must not be blank")
        )
    if not _is_sha256(task_revision.revision_hash):
        issues.append(
            _issue(
                "invalid_hash",
                "task_revision.revision_hash",
                "revision_hash must be a lowercase SHA-256 digest",
            )
        )
    if not _clean(source_manifest.manifest_id):
        issues.append(
            _issue("blank_identifier", "source_manifest.manifest_id", "manifest_id must not be blank")
        )
    if not _is_sha256(source_manifest.manifest_hash):
        issues.append(
            _issue(
                "invalid_hash",
                "source_manifest.manifest_hash",
                "manifest_hash must be a lowercase SHA-256 digest",
            )
        )
    if not isinstance(source_manifest.kind, ManifestKind):
        issues.append(
            _issue("invalid_manifest_kind", "source_manifest.kind", "manifest kind is not supported")
        )

    normalized_records: list[RecordSpec] = []
    record_ids: dict[str, int] = {}
    content_hashes: dict[str, int] = {}
    for index, record in enumerate(request.records):
        field = f"records[{index}]"
        if not isinstance(record, RecordSpec):
            issues.append(_issue("invalid_record", field, "record must be a RecordSpec"))
            continue
        record_id = _clean(record.record_id)
        batch_id = _clean(record.batch_id)
        if not record_id:
            issues.append(_issue("blank_identifier", f"{field}.record_id", "record_id must not be blank"))
        elif record_id in record_ids:
            issues.append(
                _issue(
                    "duplicate_record_id",
                    f"{field}.record_id",
                    "record_id must be unique",
                    record_id=record_id,
                    first_index=record_ids[record_id],
                )
            )
        else:
            record_ids[record_id] = index
        if not _is_sha256(record.content_hash):
            issues.append(
                _issue(
                    "invalid_hash",
                    f"{field}.content_hash",
                    "content_hash must be a lowercase SHA-256 digest",
                )
            )
        elif record.content_hash in content_hashes:
            issues.append(
                _issue(
                    "duplicate_record_content_hash",
                    f"{field}.content_hash",
                    "record content_hash must be unique",
                    content_hash=record.content_hash,
                    first_index=content_hashes[record.content_hash],
                )
            )
        else:
            content_hashes[record.content_hash] = index
        if not batch_id:
            issues.append(_issue("blank_identifier", f"{field}.batch_id", "batch_id must not be blank"))
        if record_id and batch_id and _is_sha256(record.content_hash):
            normalized_records.append(
                RecordSpec(record_id=record_id, content_hash=record.content_hash, batch_id=batch_id)
            )

    normalized_annotators: list[AnnotatorSpec] = []
    annotator_ids: dict[str, int] = {}
    cohort_ids: set[str] = set()
    for index, annotator in enumerate(request.annotators):
        field = f"annotators[{index}]"
        if not isinstance(annotator, AnnotatorSpec):
            issues.append(_issue("invalid_annotator", field, "annotator must be an AnnotatorSpec"))
            continue
        annotator_id = _clean(annotator.annotator_id)
        cohort_id = _clean(annotator.cohort_id)
        if not annotator_id:
            issues.append(
                _issue("blank_identifier", f"{field}.annotator_id", "annotator_id must not be blank")
            )
        elif annotator_id in annotator_ids:
            issues.append(
                _issue(
                    "duplicate_annotator_id",
                    f"{field}.annotator_id",
                    "annotator_id must be unique",
                    annotator_id=annotator_id,
                    first_index=annotator_ids[annotator_id],
                )
            )
        else:
            annotator_ids[annotator_id] = index
        if not cohort_id:
            issues.append(_issue("blank_identifier", f"{field}.cohort_id", "cohort_id must not be blank"))
        else:
            cohort_ids.add(cohort_id)
        if isinstance(annotator.capacity, bool) or not isinstance(annotator.capacity, int):
            issues.append(
                _issue("invalid_capacity", f"{field}.capacity", "capacity must be a non-negative integer")
            )
        elif annotator.capacity < 0:
            issues.append(
                _issue("invalid_capacity", f"{field}.capacity", "capacity must be a non-negative integer")
            )
        if annotator_id and cohort_id and isinstance(annotator.capacity, int) and not isinstance(
            annotator.capacity, bool
        ) and annotator.capacity >= 0:
            normalized_annotators.append(
                AnnotatorSpec(
                    annotator_id=annotator_id,
                    cohort_id=cohort_id,
                    capacity=annotator.capacity,
                )
            )

    if not request.annotators:
        issues.append(_issue("empty_cohort", "annotators", "at least one annotator is required"))
    if len(cohort_ids) > 1:
        issues.append(
            _issue(
                "mixed_cohort",
                "annotators",
                "all annotators in one allocation request must belong to the same cohort",
                cohort_ids=",".join(sorted(cohort_ids)),
            )
        )

    issues.extend(_selector_shape_issues(request.overlap, "overlap"))
    if request.strategy == AllocationStrategy.CALIBRATION_THEN_PARTITION:
        if request.calibration is None:
            issues.append(
                _issue(
                    "calibration_required",
                    "calibration",
                    "calibration_then_partition requires a calibration rule",
                )
            )
        else:
            issues.extend(_selector_shape_issues(request.calibration, "calibration"))
            if not isinstance(request.calibration.exclude_from_production, bool):
                issues.append(
                    _issue(
                        "invalid_calibration_rule",
                        "calibration.exclude_from_production",
                        "exclude_from_production must be boolean",
                    )
                )
            if request.calibration.expected_annotator_ids is not None and not isinstance(
                request.calibration.expected_annotator_ids, tuple
            ):
                issues.append(
                    _issue(
                        "mutable_collection",
                        "calibration.expected_annotator_ids",
                        "expected_annotator_ids must be a tuple or None",
                    )
                )
    elif request.calibration is not None:
        issues.append(
            _issue(
                "calibration_not_supported",
                "calibration",
                "calibration rules are only valid for calibration_then_partition",
            )
        )

    if issues:
        return None, tuple(issues)

    normalized_records.sort(key=lambda item: item.record_id)
    normalized_annotators.sort(key=lambda item: item.annotator_id)
    records_by_id = {item.record_id: item for item in normalized_records}
    cohort_id = normalized_annotators[0].cohort_id

    calibration_ids: tuple[str, ...] = ()
    expected_annotator_ids: tuple[str, ...] = ()
    calibration = request.calibration
    if request.strategy == AllocationStrategy.CALIBRATION_THEN_PARTITION and calibration is not None:
        calibration_ids, selector_issues = _resolve_selector(
            normalized_records,
            record_ids=calibration.record_ids,
            count=calibration.count,
            rate=calibration.rate,
            seed=request.seed,
            algorithm_version=request.algorithm_version,
            namespace="calibration",
            field="calibration",
        )
        issues.extend(selector_issues)
        if not calibration_ids:
            issues.append(
                _issue(
                    "empty_calibration",
                    "calibration",
                    "calibration_then_partition requires at least one calibration record",
                )
            )
        expected_annotator_ids, expected_issues = _resolve_expected_annotators(
            calibration.expected_annotator_ids,
            normalized_annotators,
        )
        issues.extend(expected_issues)

    excluded = set(calibration_ids) if calibration and calibration.exclude_from_production else set()
    production_records = tuple(item for item in normalized_records if item.record_id not in excluded)
    overlap_ids, overlap_issues = _resolve_selector(
        production_records,
        record_ids=request.overlap.record_ids,
        count=request.overlap.count,
        rate=request.overlap.rate,
        seed=request.seed,
        algorithm_version=request.algorithm_version,
        namespace="overlap",
        field="overlap",
    )
    issues.extend(overlap_issues)
    if overlap_ids and (
        isinstance(request.overlap.assignees_per_record, bool)
        or not isinstance(request.overlap.assignees_per_record, int)
        or request.overlap.assignees_per_record < 2
    ):
        issues.append(
            _issue(
                "invalid_overlap_width",
                "overlap.assignees_per_record",
                "overlap assignees_per_record must be at least 2",
            )
        )

    if issues:
        return None, tuple(issues)

    capacity_by_annotator = {item.annotator_id: item.capacity for item in normalized_annotators}
    calibration_count = len(calibration_ids)
    for annotator_id in expected_annotator_ids:
        if capacity_by_annotator[annotator_id] < calibration_count:
            issues.append(
                _issue(
                    "annotator_capacity_insufficient",
                    "annotators",
                    "annotator capacity cannot cover required calibration rows",
                    annotator_id=annotator_id,
                    capacity=capacity_by_annotator[annotator_id],
                    required=calibration_count,
                )
            )

    remaining_capacities = {
        annotator.annotator_id: annotator.capacity
        - (calibration_count if annotator.annotator_id in expected_annotator_ids else 0)
        for annotator in normalized_annotators
    }
    overlap_count = len(overlap_ids)
    overlap_width = request.overlap.assignees_per_record if overlap_count else 1
    required_production_rows = len(production_records) + overlap_count * (overlap_width - 1)
    available_production_capacity = sum(max(0, value) for value in remaining_capacities.values())
    if available_production_capacity < required_production_rows:
        issues.append(
            _issue(
                "insufficient_capacity",
                "annotators",
                "cohort capacity cannot cover all planned rows",
                available=available_production_capacity,
                required=required_production_rows,
            )
        )
    if overlap_count:
        if overlap_width > len(normalized_annotators):
            issues.append(
                _issue(
                    "cohort_too_small",
                    "overlap.assignees_per_record",
                    "overlap width exceeds cohort size",
                    cohort_size=len(normalized_annotators),
                    required=overlap_width,
                )
            )
        else:
            for record_prefix_count in range(1, overlap_count + 1):
                available = sum(
                    min(max(0, capacity), record_prefix_count)
                    for capacity in remaining_capacities.values()
                )
                required = record_prefix_count * overlap_width
                if available < required:
                    issues.append(
                        _issue(
                            "overlap_unassignable",
                            "overlap",
                            "capacities cannot place every overlap copy on a distinct annotator",
                            overlap_records=overlap_count,
                            assignees_per_record=overlap_width,
                            failing_prefix=record_prefix_count,
                            available=available,
                            required=required,
                        )
                    )
                    break

    if issues:
        return None, tuple(issues)

    normalized_overlap = OverlapRule(
        record_ids=tuple(sorted(_clean(item) for item in request.overlap.record_ids)),
        count=request.overlap.count,
        rate=request.overlap.rate,
        assignees_per_record=request.overlap.assignees_per_record,
    )
    normalized_calibration = None
    if calibration is not None:
        normalized_expected = calibration.expected_annotator_ids
        if normalized_expected is not None:
            normalized_expected = tuple(sorted(_clean(item) for item in normalized_expected))
        normalized_calibration = CalibrationRule(
            record_ids=tuple(sorted(_clean(item) for item in calibration.record_ids)),
            count=calibration.count,
            rate=calibration.rate,
            expected_annotator_ids=normalized_expected,
            exclude_from_production=calibration.exclude_from_production,
        )
    normalized_request = AllocationRequest(
        strategy=request.strategy,
        task_revision=TaskRevisionRef(
            task_id=_clean(task_revision.task_id),
            revision_id=_clean(task_revision.revision_id),
            revision_hash=task_revision.revision_hash,
        ),
        source_manifest=SourceManifestRef(
            manifest_id=_clean(source_manifest.manifest_id),
            manifest_hash=source_manifest.manifest_hash,
            kind=source_manifest.kind,
        ),
        records=tuple(normalized_records),
        annotators=tuple(normalized_annotators),
        seed=request.seed,
        algorithm_version=_clean(request.algorithm_version),
        overlap=normalized_overlap,
        calibration=normalized_calibration,
    )
    resolved = _ResolvedRequest(
        request=normalized_request,
        cohort_id=cohort_id,
        calibration_record_ids=calibration_ids,
        production_record_ids=tuple(item.record_id for item in production_records),
        overlap_record_ids=overlap_ids,
        expected_calibration_annotator_ids=expected_annotator_ids,
        input_fingerprint=canonical_hash(normalized_request),
    )
    return resolved, ()


def _selector_shape_issues(rule: OverlapRule | CalibrationRule, field: str) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if not isinstance(rule.record_ids, tuple):
        issues.append(_issue("mutable_collection", f"{field}.record_ids", "record_ids must be a tuple"))
    active = int(bool(rule.record_ids)) + int(rule.count is not None) + int(rule.rate is not None)
    if active > 1:
        issues.append(
            _issue(
                "selector_conflict",
                field,
                "record_ids, count, and rate are mutually exclusive selectors",
            )
        )
    if rule.count is not None and (
        isinstance(rule.count, bool) or not isinstance(rule.count, int) or rule.count < 0
    ):
        issues.append(
            _issue("invalid_selector_count", f"{field}.count", "selector count must be non-negative")
        )
    if rule.rate is not None and (
        isinstance(rule.rate, bool)
        or not isinstance(rule.rate, (int, float))
        or not math.isfinite(float(rule.rate))
        or float(rule.rate) < 0
        or float(rule.rate) > 1
    ):
        issues.append(_issue("invalid_selector_rate", f"{field}.rate", "selector rate must be in [0, 1]"))
    if isinstance(rule, OverlapRule) and (
        isinstance(rule.assignees_per_record, bool)
        or not isinstance(rule.assignees_per_record, int)
        or rule.assignees_per_record < 2
    ):
        issues.append(
            _issue(
                "invalid_overlap_width",
                f"{field}.assignees_per_record",
                "assignees_per_record must be at least 2",
            )
        )
    return issues


def _resolve_selector(
    records: Sequence[RecordSpec],
    *,
    record_ids: tuple[str, ...],
    count: int | None,
    rate: float | None,
    seed: int,
    algorithm_version: str,
    namespace: str,
    field: str,
) -> tuple[tuple[str, ...], tuple[ValidationIssue, ...]]:
    issues: list[ValidationIssue] = []
    available_ids = {item.record_id for item in records}
    if record_ids:
        normalized = tuple(_clean(item) for item in record_ids)
        seen: set[str] = set()
        for index, record_id in enumerate(normalized):
            if not record_id:
                issues.append(
                    _issue("blank_identifier", f"{field}.record_ids[{index}]", "record_id must not be blank")
                )
            elif record_id in seen:
                issues.append(
                    _issue(
                        "duplicate_rule_record_id",
                        f"{field}.record_ids[{index}]",
                        "rule record_ids must be unique",
                        record_id=record_id,
                    )
                )
            elif record_id not in available_ids:
                issues.append(
                    _issue(
                        "unknown_record_id",
                        f"{field}.record_ids[{index}]",
                        "rule references a record outside the eligible source set",
                        record_id=record_id,
                    )
                )
            seen.add(record_id)
        return tuple(sorted(seen & available_ids)), tuple(issues)

    selected_count = 0
    if count is not None:
        if count > len(records):
            issues.append(
                _issue(
                    "selector_exceeds_records",
                    f"{field}.count",
                    "selector count exceeds the eligible record count",
                    available=len(records),
                    requested=count,
                )
            )
        selected_count = min(count, len(records))
    elif rate is not None:
        selected_count = min(len(records), math.ceil(len(records) * float(rate)))
    if selected_count == 0:
        return (), tuple(issues)
    ordered = sorted(
        records,
        key=lambda item: (
            _seed_rank(seed, algorithm_version, namespace, item.record_id, item.content_hash),
            item.record_id,
        ),
    )
    return tuple(sorted(item.record_id for item in ordered[:selected_count])), tuple(issues)


def _resolve_expected_annotators(
    expected: tuple[str, ...] | None,
    annotators: Sequence[AnnotatorSpec],
) -> tuple[tuple[str, ...], tuple[ValidationIssue, ...]]:
    available = {item.annotator_id for item in annotators}
    if expected is None:
        return tuple(sorted(available)), ()
    issues: list[ValidationIssue] = []
    normalized = tuple(_clean(item) for item in expected)
    seen: set[str] = set()
    for index, annotator_id in enumerate(normalized):
        if not annotator_id:
            issues.append(
                _issue(
                    "blank_identifier",
                    f"calibration.expected_annotator_ids[{index}]",
                    "annotator_id must not be blank",
                )
            )
        elif annotator_id in seen:
            issues.append(
                _issue(
                    "duplicate_expected_annotator",
                    f"calibration.expected_annotator_ids[{index}]",
                    "expected annotator IDs must be unique",
                    annotator_id=annotator_id,
                )
            )
        elif annotator_id not in available:
            issues.append(
                _issue(
                    "unknown_expected_annotator",
                    f"calibration.expected_annotator_ids[{index}]",
                    "expected annotator is outside the selected cohort",
                    annotator_id=annotator_id,
                )
            )
        seen.add(annotator_id)
    selected = tuple(sorted(seen & available))
    if not selected:
        issues.append(
            _issue(
                "empty_expected_annotators",
                "calibration.expected_annotator_ids",
                "at least one expected calibration annotator is required",
            )
        )
    return selected, tuple(issues)


def _seed_rank(seed: int, algorithm_version: str, namespace: str, *parts: str) -> str:
    return canonical_hash(
        {
            "algorithm_version": algorithm_version,
            "namespace": namespace,
            "parts": parts,
            "seed": seed,
        }
    )


def _clean(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return unicodedata.normalize("NFC", value.strip())


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _issue(code: str, field: str, message: str, **context: Any) -> ValidationIssue:
    return ValidationIssue(
        code=code,
        field=field,
        message=message,
        context=tuple(sorted((key, str(value)) for key, value in context.items())),
    )
