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
    validate_allocation_request,
)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _request(
    *,
    strategy: AllocationStrategy = AllocationStrategy.FIXED_PARTITION,
    records: tuple[RecordSpec, ...] | None = None,
    annotators: tuple[AnnotatorSpec, ...] | None = None,
    overlap: OverlapRule = OverlapRule(),
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
        overlap=overlap,
        calibration=calibration,
    )


def test_canonical_hash_is_mapping_order_independent_and_unicode_normalized():
    composed = {"label": "caf\u00e9", "nested": {"b": 2, "a": 1}}
    decomposed = {"nested": {"a": 1, "b": 2}, "label": "cafe\u0301"}

    assert canonical_hash(composed) == canonical_hash(decomposed)


def test_validation_accepts_empty_record_set_with_a_valid_cohort():
    report = validate_allocation_request(_request(records=()))

    assert report.ok
    assert report.issues == ()


def test_validation_rejects_duplicate_record_ids_hashes_and_annotators():
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
    assert {issue.code for issue in report.issues} == {
        "duplicate_annotator_id",
        "duplicate_record_content_hash",
        "duplicate_record_id",
    }


@pytest.mark.parametrize("capacity", [-1, 1.5, True])
def test_validation_rejects_invalid_capacity(capacity):
    request = _request(
        records=(),
        annotators=(AnnotatorSpec(annotator_id="annotator-a", cohort_id="cohort-a", capacity=capacity),),
    )

    report = validate_allocation_request(request)

    assert [issue.code for issue in report.issues] == ["invalid_capacity"]


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
        overlap=OverlapRule(count=2, assignees_per_record=2),
    )

    report = validate_allocation_request(request)

    assert not report.ok
    assert {issue.code for issue in report.issues} == {"overlap_unassignable"}
    overlap_issue = next(issue for issue in report.issues if issue.code == "overlap_unassignable")
    assert dict(overlap_issue.context)["failing_prefix"] == "2"


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

    assert [issue.code for issue in report.issues] == ["insufficient_capacity"]


def test_calibration_requires_known_unique_expected_annotators():
    request = _request(
        strategy=AllocationStrategy.CALIBRATION_THEN_PARTITION,
        calibration=CalibrationRule(
            count=1,
            expected_annotator_ids=("annotator-0", "annotator-0", "outside-cohort"),
        ),
    )

    report = validate_allocation_request(request)

    assert not report.ok
    assert {issue.code for issue in report.issues} == {
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
        overlap=OverlapRule(count=4, assignees_per_record=2),
    )

    first = plan_allocation(request)
    reordered = plan_allocation(
        replace(request, records=tuple(reversed(request.records)), annotators=tuple(reversed(request.annotators)))
    )

    assert first == reordered
    assert first.fingerprint == canonical_hash(first.fingerprint_payload())
    assert first.unsatisfied_constraints == ()
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
        overlap=OverlapRule(record_ids=("record-1", "record-5"), assignees_per_record=3),
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
        overlap=OverlapRule(count=2, assignees_per_record=2),
        calibration=CalibrationRule(
            record_ids=("record-0", "record-1"),
            expected_annotator_ids=("annotator-0", "annotator-2"),
        ),
    )

    plan = plan_allocation(request)

    calibration_assignments = tuple(
        item for item in plan.assignments if item.phase == AssignmentPhase.CALIBRATION
    )
    production_assignments = tuple(
        item for item in plan.assignments if item.phase == AssignmentPhase.PRODUCTION
    )
    assert len(calibration_assignments) == 4
    assert {item.record_id for item in calibration_assignments} == {"record-0", "record-1"}
    assert {item.assignee_id for item in calibration_assignments} == {
        "annotator-0",
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
        item.expected_annotator_ids == ("annotator-0", "annotator-2")
        for item in gate.expectations
    )
    assert all(item.gate_id == gate.gate_id for item in production_assignments)

    calibration_datasets = [
        item for item in plan.dataset_groups if item.phase == AssignmentPhase.CALIBRATION
    ]
    assert len(calibration_datasets) == 1
    assert calibration_datasets[0].min_submitted == 2
    assert calibration_datasets[0].record_ids == ("record-0", "record-1")
    assert len(calibration_datasets[0].record_ids) == len(set(calibration_datasets[0].record_ids))
    assert len(calibration_datasets[0].assignment_ids) == 4
    assert sum(item.assigned_rows for item in plan.annotator_loads) == len(plan.assignments)
    assert all(item.assigned_rows <= item.capacity for item in plan.annotator_loads)


def test_plan_allocation_fails_before_emitting_a_partial_plan():
    request = _request(
        annotators=(AnnotatorSpec(annotator_id="annotator-a", cohort_id="cohort-a", capacity=1),),
    )

    with pytest.raises(AllocationValidationError) as exc_info:
        plan_allocation(request)

    assert [issue.code for issue in exc_info.value.report.issues] == ["insufficient_capacity"]
