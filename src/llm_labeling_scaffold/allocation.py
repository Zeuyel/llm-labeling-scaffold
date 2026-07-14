from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass, fields, is_dataclass, replace
from enum import Enum
from fractions import Fraction
from itertools import combinations
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


class AnnotatorLoadMode(str, Enum):
    DIRECT_ASSIGNMENT = "direct_assignment"
    SHARED_QUEUE_ADVISORY = "shared_queue_advisory"


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
    """Separates enforceable assignments from shared-queue capacity advice.

    Advisory shared rows are not per-user assignments and cannot be enforced by Argilla.
    """

    annotator_id: str
    cohort_id: str
    mode: AnnotatorLoadMode
    capacity: int
    calibration_rows: int
    production_rows: int
    assigned_rows: int
    advisory_reserved_shared_rows: int
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


@dataclass(frozen=True, slots=True)
class _DatasetSpec:
    dataset_group_id: str
    workspace_group_id: str
    phase: AssignmentPhase
    min_submitted: int
    assignee_id: str | None
    pool_id: str | None


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


def plan_allocation(request: AllocationRequest) -> AllocationPlan:
    resolved, issues = _resolve_request(request)
    if resolved is None:
        raise AllocationValidationError(ValidationReport(issues=issues))

    normalized = resolved.request
    records_by_id = {item.record_id: item for item in normalized.records}
    annotators_by_id = {item.annotator_id: item for item in normalized.annotators}
    calibration_loads = {item.annotator_id: 0 for item in normalized.annotators}
    assignments: list[Assignment] = []
    workspace_groups: list[WorkspaceGroup] = []
    dataset_specs: dict[str, _DatasetSpec] = {}
    gates: list[AllocationGate] = []

    gate_id: str | None = None
    if normalized.strategy == AllocationStrategy.CALIBRATION_THEN_PARTITION:
        gate_id = _stable_id("gate", resolved.input_fingerprint, "calibration")
        calibration_workspace_id = _stable_id(
            "workspace", resolved.input_fingerprint, AssignmentPhase.CALIBRATION.value
        )
        calibration_dataset_id = _stable_id(
            "dataset", resolved.input_fingerprint, AssignmentPhase.CALIBRATION.value
        )
        workspace_groups.append(
            WorkspaceGroup(
                workspace_group_id=calibration_workspace_id,
                phase=AssignmentPhase.CALIBRATION,
                mode=WorkspaceMode.CALIBRATION,
                cohort_id=resolved.cohort_id,
                assignee_id=None,
                member_annotator_ids=resolved.expected_calibration_annotator_ids,
            )
        )
        dataset_specs[calibration_dataset_id] = _DatasetSpec(
            dataset_group_id=calibration_dataset_id,
            workspace_group_id=calibration_workspace_id,
            phase=AssignmentPhase.CALIBRATION,
            min_submitted=len(resolved.expected_calibration_annotator_ids),
            assignee_id=None,
            pool_id=resolved.cohort_id,
        )
        for record_id in resolved.calibration_record_ids:
            record = records_by_id[record_id]
            for annotator_id in resolved.expected_calibration_annotator_ids:
                assignments.append(
                    _assignment(
                        resolved,
                        record,
                        phase=AssignmentPhase.CALIBRATION,
                        role=AssignmentRole.CALIBRATION,
                        workspace_group_id=calibration_workspace_id,
                        dataset_group_id=calibration_dataset_id,
                        assignee_id=annotator_id,
                        pool_id=None,
                        required_submissions=1,
                        gate_id=None,
                    )
                )
                calibration_loads[annotator_id] += 1
        expectations = tuple(
            CalibrationExpectation(
                record_id=record_id,
                expected_annotator_ids=resolved.expected_calibration_annotator_ids,
            )
            for record_id in resolved.calibration_record_ids
        )
        gates.append(
            AllocationGate(
                gate_id=gate_id,
                gate_kind="calibration_expected_responder_set_v1",
                blocks_phase=AssignmentPhase.PRODUCTION,
                task_revision_hash=normalized.task_revision.revision_hash,
                source_manifest_hash=normalized.source_manifest.manifest_hash,
                cohort_id=resolved.cohort_id,
                expectations=expectations,
                plan_fingerprint="",
            )
        )

    production_targets, final_loads = _allocate_production_targets(resolved, calibration_loads)
    if normalized.strategy == AllocationStrategy.SHARED_QUEUE:
        shared_workspace_id = _stable_id("workspace", "shared", resolved.cohort_id)
        workspace_groups.append(
            WorkspaceGroup(
                workspace_group_id=shared_workspace_id,
                phase=AssignmentPhase.PRODUCTION,
                mode=WorkspaceMode.SHARED,
                cohort_id=resolved.cohort_id,
                assignee_id=None,
                member_annotator_ids=tuple(annotators_by_id),
            )
        )
        overlap_ids = set(resolved.overlap_record_ids)
        for record_id in resolved.production_record_ids:
            record = records_by_id[record_id]
            required_submissions = (
                normalized.overlap.assignees_per_record if record_id in overlap_ids else 1
            )
            dataset_group_id = _stable_id(
                "dataset",
                resolved.input_fingerprint,
                AssignmentPhase.PRODUCTION.value,
                "shared",
                str(required_submissions),
            )
            dataset_specs.setdefault(
                dataset_group_id,
                _DatasetSpec(
                    dataset_group_id=dataset_group_id,
                    workspace_group_id=shared_workspace_id,
                    phase=AssignmentPhase.PRODUCTION,
                    min_submitted=required_submissions,
                    assignee_id=None,
                    pool_id=resolved.cohort_id,
                ),
            )
            assignments.append(
                _assignment(
                    resolved,
                    record,
                    phase=AssignmentPhase.PRODUCTION,
                    role=(
                        AssignmentRole.OVERLAP
                        if required_submissions > 1
                        else AssignmentRole.SHARED
                    ),
                    workspace_group_id=shared_workspace_id,
                    dataset_group_id=dataset_group_id,
                    assignee_id=None,
                    pool_id=resolved.cohort_id,
                    required_submissions=required_submissions,
                    gate_id=None,
                )
            )
    else:
        personal_workspaces: dict[str, str] = {}
        for annotator in normalized.annotators:
            workspace_group_id = _stable_id(
                "workspace", "personal", resolved.cohort_id, annotator.annotator_id
            )
            personal_workspaces[annotator.annotator_id] = workspace_group_id
            workspace_groups.append(
                WorkspaceGroup(
                    workspace_group_id=workspace_group_id,
                    phase=AssignmentPhase.PRODUCTION,
                    mode=WorkspaceMode.PERSONAL,
                    cohort_id=resolved.cohort_id,
                    assignee_id=annotator.annotator_id,
                    member_annotator_ids=(annotator.annotator_id,),
                )
            )
        overlap_ids = set(resolved.overlap_record_ids)
        for record_id in resolved.production_record_ids:
            record = records_by_id[record_id]
            target_ids = production_targets[record_id]
            primary_id = min(
                target_ids,
                key=lambda annotator_id: (
                    _seed_rank(
                        normalized.seed,
                        normalized.algorithm_version,
                        "primary",
                        record_id,
                        annotator_id,
                    ),
                    annotator_id,
                ),
            )
            for annotator_id in target_ids:
                workspace_group_id = personal_workspaces[annotator_id]
                dataset_group_id = _stable_id(
                    "dataset",
                    resolved.input_fingerprint,
                    AssignmentPhase.PRODUCTION.value,
                    annotator_id,
                )
                dataset_specs.setdefault(
                    dataset_group_id,
                    _DatasetSpec(
                        dataset_group_id=dataset_group_id,
                        workspace_group_id=workspace_group_id,
                        phase=AssignmentPhase.PRODUCTION,
                        min_submitted=1,
                        assignee_id=annotator_id,
                        pool_id=None,
                    ),
                )
                role = AssignmentRole.PRIMARY
                if record_id in overlap_ids and annotator_id != primary_id:
                    role = AssignmentRole.OVERLAP
                assignments.append(
                    _assignment(
                        resolved,
                        record,
                        phase=AssignmentPhase.PRODUCTION,
                        role=role,
                        workspace_group_id=workspace_group_id,
                        dataset_group_id=dataset_group_id,
                        assignee_id=annotator_id,
                        pool_id=None,
                        required_submissions=1,
                        gate_id=gate_id,
                    )
                )

    assignments_tuple = tuple(sorted(assignments, key=_assignment_sort_key))
    dataset_groups = _dataset_groups(assignments_tuple, dataset_specs)
    annotator_loads = _annotator_loads(
        resolved,
        assignments_tuple,
        calibration_loads,
        final_loads,
    )
    overlap_matrix = _overlap_matrix(resolved, assignments_tuple)
    plan = AllocationPlan(
        schema_version="allocation_plan_v1",
        strategy=normalized.strategy,
        task_revision=normalized.task_revision,
        source_manifest=normalized.source_manifest,
        seed=normalized.seed,
        algorithm_version=normalized.algorithm_version,
        input_fingerprint=resolved.input_fingerprint,
        fingerprint="",
        assignments=assignments_tuple,
        workspace_groups=tuple(sorted(workspace_groups, key=lambda item: item.workspace_group_id)),
        dataset_groups=dataset_groups,
        annotator_loads=annotator_loads,
        overlap_matrix=overlap_matrix,
        gates=tuple(gates),
        unsatisfied_constraints=(),
    )
    fingerprint = canonical_hash(plan.fingerprint_payload())
    bound_gates = tuple(replace(item, plan_fingerprint=fingerprint) for item in plan.gates)
    return replace(plan, fingerprint=fingerprint, gates=bound_gates)


def _allocate_production_targets(
    resolved: _ResolvedRequest,
    initial_loads: Mapping[str, int],
) -> tuple[dict[str, tuple[str, ...]], dict[str, int]]:
    request = resolved.request
    capacities = {item.annotator_id: item.capacity for item in request.annotators}
    loads = {
        item.annotator_id: int(initial_loads.get(item.annotator_id, 0))
        for item in request.annotators
    }
    targets: dict[str, tuple[str, ...]] = {}
    overlap_ids = set(resolved.overlap_record_ids)
    ordered_overlap = sorted(
        overlap_ids,
        key=lambda record_id: (
            _seed_rank(
                request.seed,
                request.algorithm_version,
                "overlap-record-order",
                record_id,
            ),
            record_id,
        ),
    )
    if ordered_overlap:
        quotas = _overlap_quotas(
            resolved,
            loads=loads,
            capacities=capacities,
            overlap_count=len(ordered_overlap),
            overlap_width=request.overlap.assignees_per_record,
        )
        remaining_quotas = dict(quotas)
        for record_id in ordered_overlap:
            candidates = sorted(
                (annotator_id for annotator_id, quota in remaining_quotas.items() if quota > 0),
                key=lambda annotator_id: (
                    -remaining_quotas[annotator_id],
                    _seed_rank(
                        request.seed,
                        request.algorithm_version,
                        "overlap-edge",
                        record_id,
                        annotator_id,
                    ),
                    annotator_id,
                ),
            )
            selected = candidates[: request.overlap.assignees_per_record]
            if len(selected) != request.overlap.assignees_per_record:
                raise RuntimeError("validated overlap degree sequence could not be realized")
            targets[record_id] = tuple(sorted(selected))
            for annotator_id in selected:
                remaining_quotas[annotator_id] -= 1
                loads[annotator_id] += 1
        if any(remaining_quotas.values()):
            raise RuntimeError("validated overlap degree sequence left unused quota")

    regular_ids = sorted(
        (record_id for record_id in resolved.production_record_ids if record_id not in overlap_ids),
        key=lambda record_id: (
            _seed_rank(
                request.seed,
                request.algorithm_version,
                "regular-record-order",
                record_id,
            ),
            record_id,
        ),
    )
    for record_id in regular_ids:
        candidates = [
            annotator_id
            for annotator_id, capacity in capacities.items()
            if loads[annotator_id] < capacity
        ]
        if not candidates:
            raise RuntimeError("validated production capacity was exhausted")
        annotator_id = min(
            candidates,
            key=lambda candidate: (
                Fraction(loads[candidate], capacities[candidate]),
                loads[candidate],
                _seed_rank(
                    request.seed,
                    request.algorithm_version,
                    "regular-assignee",
                    record_id,
                    candidate,
                ),
                candidate,
            ),
        )
        targets[record_id] = (annotator_id,)
        loads[annotator_id] += 1

    return targets, loads


def _overlap_quotas(
    resolved: _ResolvedRequest,
    loads: Mapping[str, int],
    capacities: Mapping[str, int],
    overlap_count: int,
    overlap_width: int,
) -> dict[str, int]:
    request = resolved.request
    quotas = {annotator_id: 0 for annotator_id in capacities}
    for slot in range(overlap_count * overlap_width):
        candidates = [
            annotator_id
            for annotator_id, capacity in capacities.items()
            if quotas[annotator_id] < min(capacity - loads[annotator_id], overlap_count)
        ]
        if not candidates:
            raise RuntimeError("validated overlap capacity was exhausted while building quotas")
        annotator_id = min(
            candidates,
            key=lambda candidate: (
                Fraction(loads[candidate] + quotas[candidate], capacities[candidate]),
                loads[candidate] + quotas[candidate],
                _seed_rank(
                    request.seed,
                    request.algorithm_version,
                    "overlap-quota",
                    str(slot),
                    candidate,
                ),
                candidate,
            ),
        )
        quotas[annotator_id] += 1
    return quotas


def _assignment(
    resolved: _ResolvedRequest,
    record: RecordSpec,
    *,
    phase: AssignmentPhase,
    role: AssignmentRole,
    workspace_group_id: str,
    dataset_group_id: str,
    assignee_id: str | None,
    pool_id: str | None,
    required_submissions: int,
    gate_id: str | None,
) -> Assignment:
    assignment_id = _stable_id(
        "assignment",
        resolved.input_fingerprint,
        phase.value,
        record.record_id,
        record.content_hash,
        role.value,
        assignee_id or "",
        pool_id or "",
        str(required_submissions),
    )
    return Assignment(
        assignment_id=assignment_id,
        record_id=record.record_id,
        record_content_hash=record.content_hash,
        phase=phase,
        batch_id=record.batch_id,
        role=role,
        workspace_group_id=workspace_group_id,
        dataset_group_id=dataset_group_id,
        assignee_id=assignee_id,
        pool_id=pool_id,
        required_submissions=required_submissions,
        gate_id=gate_id,
    )


def _dataset_groups(
    assignments: Sequence[Assignment],
    specs: Mapping[str, _DatasetSpec],
) -> tuple[DatasetGroup, ...]:
    by_dataset: dict[str, list[Assignment]] = {dataset_id: [] for dataset_id in specs}
    for assignment in assignments:
        by_dataset[assignment.dataset_group_id].append(assignment)
    groups: list[DatasetGroup] = []
    for dataset_id, spec in specs.items():
        dataset_assignments = by_dataset[dataset_id]
        groups.append(
            DatasetGroup(
                dataset_group_id=dataset_id,
                workspace_group_id=spec.workspace_group_id,
                phase=spec.phase,
                min_submitted=spec.min_submitted,
                assignee_id=spec.assignee_id,
                pool_id=spec.pool_id,
                assignment_ids=tuple(sorted(item.assignment_id for item in dataset_assignments)),
                record_ids=tuple(sorted({item.record_id for item in dataset_assignments})),
                batch_ids=tuple(sorted({item.batch_id for item in dataset_assignments})),
            )
        )
    return tuple(sorted(groups, key=lambda item: item.dataset_group_id))


def _annotator_loads(
    resolved: _ResolvedRequest,
    assignments: Sequence[Assignment],
    calibration_loads: Mapping[str, int],
    final_loads: Mapping[str, int],
) -> tuple[AnnotatorLoad, ...]:
    calibration_rows = {item.annotator_id: 0 for item in resolved.request.annotators}
    production_rows = {item.annotator_id: 0 for item in resolved.request.annotators}
    for assignment in assignments:
        if assignment.assignee_id is None:
            continue
        if assignment.phase == AssignmentPhase.CALIBRATION:
            calibration_rows[assignment.assignee_id] += 1
        else:
            production_rows[assignment.assignee_id] += 1

    shared = resolved.request.strategy == AllocationStrategy.SHARED_QUEUE
    loads: list[AnnotatorLoad] = []
    for annotator in resolved.request.annotators:
        assigned_rows = calibration_rows[annotator.annotator_id] + production_rows[annotator.annotator_id]
        advisory_reserved_shared_rows = 0
        if shared:
            advisory_reserved_shared_rows = (
                final_loads[annotator.annotator_id] - calibration_loads[annotator.annotator_id]
            )
        planned_rows = assigned_rows + advisory_reserved_shared_rows
        loads.append(
            AnnotatorLoad(
                annotator_id=annotator.annotator_id,
                cohort_id=annotator.cohort_id,
                mode=(
                    AnnotatorLoadMode.SHARED_QUEUE_ADVISORY
                    if shared
                    else AnnotatorLoadMode.DIRECT_ASSIGNMENT
                ),
                capacity=annotator.capacity,
                calibration_rows=calibration_rows[annotator.annotator_id],
                production_rows=production_rows[annotator.annotator_id],
                assigned_rows=assigned_rows,
                advisory_reserved_shared_rows=advisory_reserved_shared_rows,
                eligible_shared_rows=(len(resolved.production_record_ids) if shared else 0),
                remaining_capacity=annotator.capacity - planned_rows,
            )
        )
    return tuple(loads)


def _overlap_matrix(
    resolved: _ResolvedRequest,
    assignments: Sequence[Assignment],
) -> OverlapMatrix:
    if resolved.request.strategy == AllocationStrategy.SHARED_QUEUE:
        requirements = tuple(
            PoolOverlapRequirement(
                phase=assignment.phase,
                pool_id=assignment.pool_id or resolved.cohort_id,
                record_id=assignment.record_id,
                required_submissions=assignment.required_submissions,
            )
            for assignment in assignments
            if assignment.required_submissions > 1
        )
        return OverlapMatrix(
            mode=OverlapMatrixMode.POOL,
            cells=(),
            pool_requirements=tuple(
                sorted(requirements, key=lambda item: (item.phase.value, item.record_id, item.pool_id))
            ),
        )

    assignees_by_record: dict[tuple[AssignmentPhase, str], set[str]] = {}
    for assignment in assignments:
        if assignment.assignee_id is None:
            continue
        assignees_by_record.setdefault((assignment.phase, assignment.record_id), set()).add(
            assignment.assignee_id
        )
    records_by_pair: dict[tuple[AssignmentPhase, str, str], list[str]] = {}
    for (phase, record_id), assignee_ids in assignees_by_record.items():
        for annotator_a, annotator_b in combinations(sorted(assignee_ids), 2):
            records_by_pair.setdefault((phase, annotator_a, annotator_b), []).append(record_id)
    cells = tuple(
        OverlapCell(
            phase=phase,
            annotator_a=annotator_a,
            annotator_b=annotator_b,
            record_ids=tuple(sorted(record_ids)),
            count=len(record_ids),
        )
        for (phase, annotator_a, annotator_b), record_ids in sorted(
            records_by_pair.items(),
            key=lambda item: (item[0][0].value, item[0][1], item[0][2]),
        )
    )
    return OverlapMatrix(mode=OverlapMatrixMode.ASSIGNEE, cells=cells, pool_requirements=())


def _assignment_sort_key(assignment: Assignment) -> tuple[str, ...]:
    return (
        assignment.phase.value,
        assignment.dataset_group_id,
        assignment.batch_id,
        assignment.record_id,
        assignment.assignee_id or "",
        assignment.pool_id or "",
        assignment.role.value,
    )


def _stable_id(prefix: str, *parts: str) -> str:
    return f"{prefix}_{canonical_hash(parts)[:24]}"


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
