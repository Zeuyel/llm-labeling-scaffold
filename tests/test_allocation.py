from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import replace

import pytest

from llm_labeling_scaffold.allocation import (
    AllocationValidationError,
    AllocationRequest,
    AllocationStrategy,
    AnnotatorLoadMode,
    AnnotatorSpec,
    AssignmentPhase,
    AssignmentRole,
    CalibrationRule,
    ManifestKind,
    OverlapMatrixMode,
    OverlapRule,
    RecordSpec,
    SourceManifestRef,
    TaskRevisionRef,
    WorkspaceMode,
    canonical_hash,
    plan_allocation,
    preview_allocation,
    validate_allocation_request,
)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _request(
    *,
    strategy: AllocationStrategy = AllocationStrategy.FIXED_PARTITION,
    records: tuple[RecordSpec, ...] | None = None,
    annotators: tuple[AnnotatorSpec, ...] | None = None,
    overlap_rules: tuple[OverlapRule, ...] = (),
    calibration: CalibrationRule | None = None,
    seed: int = 17,
) -> AllocationRequest:
    if records is None:
        records = tuple(
            RecordSpec(record_id=f"record-{index}", content_hash=_hash(f"content-{index}"))
            for index in range(6)
        )
    if annotators is None:
        annotators = tuple(
            AnnotatorSpec(annotator_id=f"annotator-{index}", cohort_id="cohort-a", capacity=10)
            for index in range(3)
        )
    return AllocationRequest(
        strategy=strategy,
        task_revision=TaskRevisionRef(
            task_id="task-a",
            revision_id="revision-3",
            revision_hash=_hash("revision-3"),
        ),
        source_manifest=SourceManifestRef(
            manifest_id="sample-9",
            manifest_hash=_hash("sample-9"),
            kind=ManifestKind.SAMPLE,
        ),
        records=records,
        annotators=annotators,
        seed=seed,
        algorithm_version="allocation-v1",
        overlap_rules=overlap_rules,
        calibration=calibration,
    )


def test_canonical_hash_is_mapping_order_independent_and_unicode_normalized():
    composed = {"label": "caf\u00e9", "nested": {"b": 2, "a": 1}}
    decomposed = {"nested": {"a": 1, "b": 2}, "label": "cafe\u0301"}

    assert canonical_hash(composed) == canonical_hash(decomposed)


def test_validation_accepts_empty_record_set_with_a_valid_cohort():
    report = validate_allocation_request(_request(records=()))

    assert report.ok
    assert report.blocking_errors == ()


def test_validation_rejects_duplicate_record_ids_and_annotators():
    duplicate_hash = _hash("same-content")
    request = _request(
        records=(
            RecordSpec(record_id="record-a", content_hash=duplicate_hash),
            RecordSpec(record_id="record-a", content_hash=duplicate_hash),
        ),
        annotators=(
            AnnotatorSpec(annotator_id="annotator-a", cohort_id="cohort-a", capacity=2),
            AnnotatorSpec(annotator_id="annotator-a", cohort_id="cohort-a", capacity=2),
        ),
    )

    report = validate_allocation_request(request)

    assert not report.ok
    assert {issue.code for issue in report.blocking_errors} == {
        "duplicate_annotator_id",
        "duplicate_record_id",
    }


def test_distinct_source_records_may_share_a_content_hash():
    duplicate_hash = _hash("same-content")
    request = _request(
        records=(
            RecordSpec(record_id="record-a", content_hash=duplicate_hash),
            RecordSpec(record_id="record-b", content_hash=duplicate_hash),
        )
    )

    report = validate_allocation_request(request)
    plan = plan_allocation(request)

    assert report.ok
    assert len(plan.assignments) == 2
    assert {item.record_id for item in plan.assignments} == {"record-a", "record-b"}


@pytest.mark.parametrize("capacity", [-1, 1.5, True])
def test_validation_rejects_invalid_capacity(capacity):
    request = _request(
        records=(),
        annotators=(AnnotatorSpec(annotator_id="annotator-a", cohort_id="cohort-a", capacity=capacity),),
    )

    report = validate_allocation_request(request)

    assert [issue.code for issue in report.blocking_errors] == ["invalid_capacity"]


def test_validation_returns_structured_overlap_capacity_failures():
    request = _request(
        records=tuple(
            RecordSpec(record_id=f"record-{index}", content_hash=_hash(f"overlap-{index}"))
            for index in range(2)
        ),
        annotators=(
            AnnotatorSpec(annotator_id="annotator-a", cohort_id="cohort-a", capacity=10),
            AnnotatorSpec(annotator_id="annotator-b", cohort_id="cohort-a", capacity=1),
        ),
        overlap_rules=(OverlapRule(count=2, required_submissions=2),),
    )

    report = validate_allocation_request(request)

    assert not report.ok
    assert {issue.code for issue in report.blocking_errors} == {"overlap_unassignable"}
    overlap_issue = next(issue for issue in report.blocking_errors if issue.code == "overlap_unassignable")
    assert dict(overlap_issue.context) == {"assigned": "3", "required": "4"}


def test_validation_rejects_insufficient_total_capacity():
    request = _request(
        records=tuple(
            RecordSpec(record_id=f"record-{index}", content_hash=_hash(f"capacity-{index}"))
            for index in range(3)
        ),
        annotators=(
            AnnotatorSpec(annotator_id="annotator-a", cohort_id="cohort-a", capacity=1),
        ),
    )

    report = validate_allocation_request(request)

    assert [issue.code for issue in report.blocking_errors] == ["insufficient_capacity"]


def test_calibration_requires_known_unique_expected_annotators():
    request = _request(
        strategy=AllocationStrategy.CALIBRATION_THEN_PARTITION,
        calibration=CalibrationRule(
            count=1,
            expected_annotator_ids=(
                "annotator-0",
                "annotator-1",
                "annotator-2",
                "annotator-2",
                "outside-cohort",
            ),
        ),
    )

    report = validate_allocation_request(request)

    assert not report.ok
    assert {issue.code for issue in report.blocking_errors} == {
        "duplicate_expected_annotator",
        "unknown_expected_annotator",
    }


def test_fixed_partition_is_stable_balanced_and_capacity_bounded():
    records = tuple(
        RecordSpec(
            record_id=f"record-{index:02d}",
            content_hash=_hash(f"fixed-content-{index}"),
            batch_id=f"batch-{index // 4}",
        )
        for index in range(12)
    )
    request = _request(
        records=records,
        annotators=tuple(
            AnnotatorSpec(annotator_id=f"annotator-{index}", cohort_id="cohort-a", capacity=5)
            for index in range(4)
        ),
        overlap_rules=(OverlapRule(count=4, required_submissions=2),),
    )

    first = plan_allocation(request)
    reordered = plan_allocation(
        replace(request, records=tuple(reversed(request.records)), annotators=tuple(reversed(request.annotators)))
    )

    assert first == reordered
    assert first.fingerprint == canonical_hash(first.fingerprint_payload())
    assert first.blocking_errors == ()
    assert first.warnings == ()
    assert all(group.mode == WorkspaceMode.PERSONAL for group in first.workspace_groups)
    assert all(group.min_submitted == 1 for group in first.dataset_groups)

    by_record = defaultdict(list)
    for assignment in first.assignments:
        by_record[assignment.record_id].append(assignment)
    overlap_records = {
        record_id
        for record_id, assignments in by_record.items()
        if any(item.role == AssignmentRole.OVERLAP for item in assignments)
    }
    assert len(overlap_records) == 4
    assert all(len(by_record[record_id]) == 2 for record_id in overlap_records)
    assert all(
        len({item.assignee_id for item in by_record[record_id]}) == 2
        for record_id in overlap_records
    )
    assert len({(item.record_id, item.assignee_id) for item in first.assignments}) == len(
        first.assignments
    )

    assigned_rows = [item.assigned_rows for item in first.annotator_loads]
    assert sum(assigned_rows) == 16
    assert max(assigned_rows) - min(assigned_rows) <= 1
    assert all(item.assigned_rows <= item.capacity for item in first.annotator_loads)
    assert first.overlap_matrix.mode == OverlapMatrixMode.ASSIGNEE
    assert sum(cell.count for cell in first.overlap_matrix.cells) == 4


def test_fixed_partition_assignment_changes_across_seeds():
    request = _request(
        records=tuple(
            RecordSpec(record_id=f"record-{index}", content_hash=_hash(f"seed-content-{index}"))
            for index in range(18)
        ),
        annotators=tuple(
            AnnotatorSpec(annotator_id=f"annotator-{index}", cohort_id="cohort-a", capacity=9)
            for index in range(3)
        ),
    )

    matrices = {
        tuple((item.record_id, item.assignee_id) for item in plan_allocation(replace(request, seed=seed)).assignments)
        for seed in range(6)
    }

    assert len(matrices) > 1


def test_shared_queue_keeps_one_source_row_and_splits_min_submitted_groups():
    records = tuple(
        RecordSpec(record_id=f"record-{index}", content_hash=_hash(f"shared-content-{index}"))
        for index in range(8)
    )
    request = _request(
        strategy=AllocationStrategy.SHARED_QUEUE,
        records=records,
        annotators=tuple(
            AnnotatorSpec(annotator_id=f"annotator-{index}", cohort_id="cohort-a", capacity=6)
            for index in range(3)
        ),
        overlap_rules=(
            OverlapRule(record_ids=("record-1", "record-5"), required_submissions=3),
        ),
    )

    plan = plan_allocation(request)

    assert len(plan.assignments) == len(records)
    assert len({item.record_id for item in plan.assignments}) == len(records)
    assert all(item.assignee_id is None and item.pool_id == "cohort-a" for item in plan.assignments)
    assert {group.min_submitted for group in plan.dataset_groups} == {1, 3}
    assert len(plan.dataset_groups) == 2
    assert [group.mode for group in plan.workspace_groups] == [WorkspaceMode.SHARED]
    assert plan.overlap_matrix.mode == OverlapMatrixMode.POOL
    assert {item.record_id for item in plan.overlap_matrix.pool_requirements} == {
        "record-1",
        "record-5",
    }
    assert sum(item.assigned_rows for item in plan.annotator_loads) == 0
    assert all(item.mode == AnnotatorLoadMode.SHARED_QUEUE_ADVISORY for item in plan.annotator_loads)
    assert sum(item.advisory_reserved_shared_rows for item in plan.annotator_loads) == 12
    assert all(
        item.advisory_reserved_shared_rows <= item.capacity for item in plan.annotator_loads
    )

    preview = preview_allocation(request)
    assert preview == plan.to_preview()
    assert preview.ready
    assert set(preview.to_dict()) == {
        "algorithm_version",
        "annotator_loads",
        "blocking_errors",
        "calibration_set",
        "cohort_id",
        "dataset_requirements",
        "input_fingerprint",
        "overlap",
        "plan_fingerprint",
        "schema_version",
        "source_manifest",
        "strategy",
        "task_revision",
        "warnings",
        "workspace_requirements",
    }
    assert preview.algorithm_version == "allocation-v1"
    assert preview.input_fingerprint == plan.input_fingerprint
    assert preview.plan_fingerprint == plan.fingerprint
    assert all(item.assigned_rows == 0 for item in preview.annotator_loads)
    assert sum(item.advisory_reserved_shared_rows for item in preview.annotator_loads) == 12
    assert preview.overlap.pool_requirements[0].record_count == 2
    assert preview.overlap.pool_requirements[0].required_submissions == 3
    assert sum(item.record_count for item in preview.dataset_requirements) == len(records)


def test_calibration_then_partition_emits_expected_responder_gate():
    records = tuple(
        RecordSpec(record_id=f"record-{index}", content_hash=_hash(f"calibration-content-{index}"))
        for index in range(10)
    )
    request = _request(
        strategy=AllocationStrategy.CALIBRATION_THEN_PARTITION,
        records=records,
        annotators=tuple(
            AnnotatorSpec(annotator_id=f"annotator-{index}", cohort_id="cohort-a", capacity=8)
            for index in range(3)
        ),
        overlap_rules=(OverlapRule(count=2, required_submissions=2),),
        calibration=CalibrationRule(
            record_ids=("record-0", "record-1"),
            expected_annotator_ids=("annotator-0", "annotator-1", "annotator-2"),
        ),
    )

    plan = plan_allocation(request)

    calibration_assignments = tuple(
        item for item in plan.assignments if item.phase == AssignmentPhase.CALIBRATION
    )
    production_assignments = tuple(
        item for item in plan.assignments if item.phase == AssignmentPhase.PRODUCTION
    )
    assert len(calibration_assignments) == 6
    assert {item.record_id for item in calibration_assignments} == {"record-0", "record-1"}
    assert {item.assignee_id for item in calibration_assignments} == {
        "annotator-0",
        "annotator-1",
        "annotator-2",
    }
    assert not {"record-0", "record-1"} & {item.record_id for item in production_assignments}

    assert len(plan.gates) == 1
    gate = plan.gates[0]
    assert gate.plan_fingerprint == plan.fingerprint
    assert gate.task_revision_hash == request.task_revision.revision_hash
    assert gate.source_manifest_hash == request.source_manifest.manifest_hash
    assert gate.cohort_id == "cohort-a"
    assert {item.record_id for item in gate.expectations} == {"record-0", "record-1"}
    assert all(
        item.expected_annotator_ids == ("annotator-0", "annotator-1", "annotator-2")
        for item in gate.expectations
    )
    assert all(item.gate_id == gate.gate_id for item in production_assignments)

    calibration_datasets = [
        item for item in plan.dataset_groups if item.phase == AssignmentPhase.CALIBRATION
    ]
    assert len(calibration_datasets) == 1
    assert calibration_datasets[0].min_submitted == 3
    assert calibration_datasets[0].record_ids == ("record-0", "record-1")
    assert len(calibration_datasets[0].record_ids) == len(set(calibration_datasets[0].record_ids))
    assert len(calibration_datasets[0].assignment_ids) == 6
    assert sum(item.assigned_rows for item in plan.annotator_loads) == len(plan.assignments)
    assert all(item.assigned_rows <= item.capacity for item in plan.annotator_loads)

    preview = preview_allocation(request)
    assert preview.calibration_set is not None
    assert preview.calibration_set.record_ids == ("record-0", "record-1")
    assert preview.calibration_set.expected_annotator_ids == (
        "annotator-0",
        "annotator-1",
        "annotator-2",
    )
    assert preview.calibration_set.expected_assignment_rows == 6
    calibration_requirement = next(
        item for item in preview.dataset_requirements if item.phase == AssignmentPhase.CALIBRATION
    )
    assert calibration_requirement.record_count == 2


def test_plan_allocation_fails_before_emitting_a_partial_plan():
    request = _request(
        annotators=(AnnotatorSpec(annotator_id="annotator-a", cohort_id="cohort-a", capacity=1),),
    )

    with pytest.raises(AllocationValidationError) as exc_info:
        plan_allocation(request)

    assert [issue.code for issue in exc_info.value.report.blocking_errors] == ["insufficient_capacity"]

    preview = preview_allocation(request)
    assert not preview.ready
    assert [issue.code for issue in preview.blocking_errors] == ["insufficient_capacity"]
    assert preview.workspace_requirements == ()
    assert preview.dataset_requirements == ()
    assert preview.annotator_loads[0].capacity == 1
    assert preview.annotator_loads[0].assigned_rows == 0


def test_calibration_rejects_a_partial_production_cohort():
    request = _request(
        strategy=AllocationStrategy.CALIBRATION_THEN_PARTITION,
        calibration=CalibrationRule(
            count=1,
            expected_annotator_ids=("annotator-0", "annotator-1"),
        ),
    )

    report = validate_allocation_request(request)

    assert [item.code for item in report.blocking_errors] == ["calibration_cohort_mismatch"]


def test_multiple_overlap_tiers_share_one_plan_without_record_conflicts():
    records = tuple(
        RecordSpec(record_id=f"record-{index}", content_hash=_hash(f"multi-tier-{index}"))
        for index in range(9)
    )
    annotators = tuple(
        AnnotatorSpec(f"annotator-{index}", "cohort-a", 12) for index in range(4)
    )
    overlap_rules = (
        OverlapRule(record_ids=("record-0", "record-1"), required_submissions=2),
        OverlapRule(record_ids=("record-2", "record-3"), required_submissions=3),
    )
    fixed_request = _request(
        records=records,
        annotators=annotators,
        overlap_rules=overlap_rules,
    )
    shared_request = replace(fixed_request, strategy=AllocationStrategy.SHARED_QUEUE)

    fixed = plan_allocation(fixed_request)
    shared = plan_allocation(shared_request)

    fixed_by_record = defaultdict(list)
    for assignment in fixed.assignments:
        fixed_by_record[assignment.record_id].append(assignment)
    assert {record_id: len(fixed_by_record[record_id]) for record_id in ("record-0", "record-1")} == {
        "record-0": 2,
        "record-1": 2,
    }
    assert {record_id: len(fixed_by_record[record_id]) for record_id in ("record-2", "record-3")} == {
        "record-2": 3,
        "record-3": 3,
    }
    assert all(
        len({item.assignee_id for item in assignments}) == len(assignments)
        for assignments in fixed_by_record.values()
    )
    assert len(fixed.assignments) == 15

    assert len(shared.assignments) == len(records)
    assert {item.min_submitted for item in shared.dataset_groups} == {1, 2, 3}
    assert {
        item.record_id: item.required_submissions
        for item in shared.overlap_matrix.pool_requirements
    } == {
        "record-0": 2,
        "record-1": 2,
        "record-2": 3,
        "record-3": 3,
    }


def test_overlap_rules_must_select_disjoint_records():
    request = _request(
        overlap_rules=(
            OverlapRule(record_ids=("record-0",), required_submissions=2),
            OverlapRule(record_ids=("record-0",), required_submissions=3),
        )
    )

    report = validate_allocation_request(request)

    assert [item.code for item in report.blocking_errors] == ["overlap_rule_conflict"]


@pytest.mark.parametrize(
    ("capacities", "expected_warning"),
    [
        ((0, 0, 0), "shared_advisory_total_capacity_insufficient"),
        ((6, 0, 0), "shared_advisory_annotator_capacity_insufficient"),
    ],
)
def test_shared_advisory_capacity_shortfalls_warn_without_blocking(capacities, expected_warning):
    records = tuple(
        RecordSpec(record_id=f"record-{index}", content_hash=_hash(f"shared-warning-{index}"))
        for index in range(2)
    )
    request = _request(
        strategy=AllocationStrategy.SHARED_QUEUE,
        records=records,
        annotators=tuple(
            AnnotatorSpec(f"annotator-{index}", "cohort-a", capacity)
            for index, capacity in enumerate(capacities)
        ),
        overlap_rules=(OverlapRule(count=2, required_submissions=3),),
    )

    report = validate_allocation_request(request)
    preview = preview_allocation(request)
    plan = plan_allocation(request)

    assert report.ok
    assert [item.code for item in report.warnings] == [expected_warning]
    assert preview.ready
    assert preview.warnings == report.warnings
    assert plan.warnings == report.warnings
    assert len(plan.assignments) == len(records)


def test_shared_min_submitted_above_cohort_size_is_blocking():
    request = _request(
        strategy=AllocationStrategy.SHARED_QUEUE,
        overlap_rules=(OverlapRule(count=1, required_submissions=4),),
    )

    preview = preview_allocation(request)

    assert not preview.ready
    assert [item.code for item in preview.blocking_errors] == ["cohort_too_small"]


def test_workspace_identity_is_stable_for_personal_and_isolated_for_shared_and_calibration():
    records = tuple(
        RecordSpec(record_id=f"record-{index}", content_hash=_hash(f"workspace-{index}"))
        for index in range(4)
    )
    cohort_a = tuple(
        AnnotatorSpec(f"annotator-{index}", "cohort-a", 8) for index in range(2)
    )
    cohort_b = tuple(
        AnnotatorSpec(f"annotator-{index}", "cohort-b", 8) for index in range(2)
    )
    fixed_a = _request(records=records, annotators=cohort_a, seed=10)
    fixed_b = replace(fixed_a, annotators=cohort_b, seed=11)

    personal_a = {
        item.assignee_id: item.workspace_group_id
        for item in plan_allocation(fixed_a).workspace_groups
        if item.mode == WorkspaceMode.PERSONAL
    }
    personal_b = {
        item.assignee_id: item.workspace_group_id
        for item in plan_allocation(fixed_b).workspace_groups
        if item.mode == WorkspaceMode.PERSONAL
    }

    assert personal_a == personal_b

    shared_a = replace(fixed_a, strategy=AllocationStrategy.SHARED_QUEUE)
    shared_b = replace(fixed_b, strategy=AllocationStrategy.SHARED_QUEUE)
    shared_workspace_a = plan_allocation(shared_a).workspace_groups[0].workspace_group_id
    shared_workspace_b = plan_allocation(shared_b).workspace_groups[0].workspace_group_id
    assert shared_workspace_a != shared_workspace_b

    calibration_a = replace(
        fixed_a,
        strategy=AllocationStrategy.CALIBRATION_THEN_PARTITION,
        calibration=CalibrationRule(count=1),
    )
    calibration_b = replace(calibration_a, seed=12)
    calibration_workspaces_a = plan_allocation(calibration_a).workspace_groups
    calibration_workspaces_b = plan_allocation(calibration_b).workspace_groups
    calibration_workspace_a = next(
        item.workspace_group_id
        for item in calibration_workspaces_a
        if item.mode == WorkspaceMode.CALIBRATION
    )
    calibration_workspace_b = next(
        item.workspace_group_id
        for item in calibration_workspaces_b
        if item.mode == WorkspaceMode.CALIBRATION
    )
    assert calibration_workspace_a != calibration_workspace_b
    assert {
        item.assignee_id: item.workspace_group_id
        for item in calibration_workspaces_a
        if item.mode == WorkspaceMode.PERSONAL
    } == {
        item.assignee_id: item.workspace_group_id
        for item in calibration_workspaces_b
        if item.mode == WorkspaceMode.PERSONAL
    }
