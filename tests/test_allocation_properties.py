from __future__ import annotations

import hashlib
import json
import random
from collections import Counter, defaultdict
from dataclasses import FrozenInstanceError, replace

import pytest

from llm_labeling_scaffold.allocation import (
    AllocationRequest,
    AllocationStrategy,
    AllocationValidationError,
    AnnotatorSpec,
    AssignmentPhase,
    CalibrationRule,
    OverlapRule,
    RecordSpec,
    SourceManifestRef,
    TaskRevisionRef,
    canonical_hash,
    plan_allocation,
    validate_allocation_request,
)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _records(count: int, namespace: str) -> tuple[RecordSpec, ...]:
    return tuple(
        RecordSpec(
            record_id=f"{namespace}-record-{index:03d}",
            content_hash=_hash(f"{namespace}-content-{index}"),
            batch_id=f"batch-{index // 5:03d}",
        )
        for index in range(count)
    )


def _request(
    *,
    strategy: AllocationStrategy,
    records: tuple[RecordSpec, ...],
    annotators: tuple[AnnotatorSpec, ...],
    seed: int,
    overlap: OverlapRule = OverlapRule(),
    calibration: CalibrationRule | None = None,
) -> AllocationRequest:
    return AllocationRequest(
        strategy=strategy,
        task_revision=TaskRevisionRef(
            task_id="task-property",
            revision_id="revision-property",
            revision_hash=_hash("revision-property"),
        ),
        source_manifest=SourceManifestRef(
            manifest_id="manifest-property",
            manifest_hash=_hash("manifest-property"),
        ),
        records=records,
        annotators=annotators,
        seed=seed,
        algorithm_version="allocation-property-v1",
        overlap=overlap,
        calibration=calibration,
    )


def test_randomized_fixed_partition_properties():
    rng = random.Random(6101)

    for case in range(120):
        record_count = rng.randint(0, 28)
        annotator_count = rng.randint(1, 6)
        overlap_count = rng.randint(0, record_count)
        overlap_width = rng.randint(2, max(2, annotator_count + 1))
        records = _records(record_count, f"fixed-{case}")
        annotators = tuple(
            AnnotatorSpec(
                annotator_id=f"annotator-{index}",
                cohort_id="cohort-property",
                capacity=rng.randint(0, max(1, record_count)),
            )
            for index in range(annotator_count)
        )
        request = _request(
            strategy=AllocationStrategy.FIXED_PARTITION,
            records=records,
            annotators=annotators,
            seed=rng.randint(-10_000, 10_000),
            overlap=OverlapRule(count=overlap_count, assignees_per_record=overlap_width),
        )

        report = validate_allocation_request(request)
        if not report.ok:
            with pytest.raises(AllocationValidationError) as exc_info:
                plan_allocation(request)
            assert exc_info.value.report == report
            continue

        plan = plan_allocation(request)
        reordered = plan_allocation(
            replace(
                request,
                records=tuple(reversed(request.records)),
                annotators=tuple(reversed(request.annotators)),
            )
        )
        assert plan == reordered
        assert plan.fingerprint == canonical_hash(plan.fingerprint_payload())
        assert len({item.assignment_id for item in plan.assignments}) == len(plan.assignments)

        by_record = defaultdict(list)
        for assignment in plan.assignments:
            by_record[assignment.record_id].append(assignment)
        expected_rows = record_count + overlap_count * (overlap_width - 1)
        assert len(plan.assignments) == expected_rows
        assert set(by_record) == {item.record_id for item in records}
        assert sum(len(items) == overlap_width for items in by_record.values()) == overlap_count
        assert all(
            len({item.assignee_id for item in items}) == len(items)
            for items in by_record.values()
        )

        assignment_counts = Counter(item.assignee_id for item in plan.assignments)
        for load in plan.annotator_loads:
            assert load.assigned_rows == assignment_counts[load.annotator_id]
            assert load.assigned_rows <= load.capacity
            assert load.remaining_capacity == load.capacity - load.assigned_rows


@pytest.mark.parametrize(
    ("record_count", "rate", "expected_overlap_count"),
    [
        (0, 0.0, 0),
        (1, 0.01, 1),
        (3, 0.34, 2),
        (10, 0.5, 5),
        (10, 1.0, 10),
    ],
)
def test_shared_queue_rate_boundaries(record_count, rate, expected_overlap_count):
    records = _records(record_count, f"shared-{record_count}-{rate}")
    required_rows = record_count + expected_overlap_count
    request = _request(
        strategy=AllocationStrategy.SHARED_QUEUE,
        records=records,
        annotators=(
            AnnotatorSpec("annotator-0", "cohort-property", required_rows),
            AnnotatorSpec("annotator-1", "cohort-property", required_rows),
        ),
        seed=19,
        overlap=OverlapRule(rate=rate, assignees_per_record=2),
    )

    plan = plan_allocation(request)

    assert len(plan.assignments) == record_count
    assert len({item.record_id for item in plan.assignments}) == record_count
    assert all(item.assignee_id is None for item in plan.assignments)
    assert sum(item.reserved_shared_rows for item in plan.annotator_loads) == required_rows
    assert len(plan.overlap_matrix.pool_requirements) == expected_overlap_count
    expected_min_submitted = set()
    if record_count > expected_overlap_count:
        expected_min_submitted.add(1)
    if expected_overlap_count:
        expected_min_submitted.add(2)
    assert {item.min_submitted for item in plan.dataset_groups} == expected_min_submitted


def test_randomized_calibration_properties():
    rng = random.Random(6119)

    for case in range(40):
        annotator_count = rng.randint(2, 5)
        record_count = rng.randint(3, 18)
        calibration_count = rng.randint(1, record_count - 1)
        production_count = record_count - calibration_count
        expected_count = rng.randint(1, annotator_count)
        overlap_count = rng.randint(0, production_count)
        overlap_width = rng.randint(2, annotator_count)
        records = _records(record_count, f"calibration-{case}")
        annotator_ids = tuple(f"annotator-{index}" for index in range(annotator_count))
        expected_annotators = tuple(sorted(rng.sample(annotator_ids, expected_count)))
        required_capacity = record_count * max(annotator_count, overlap_width)
        annotators = tuple(
            AnnotatorSpec(item, "cohort-property", required_capacity) for item in annotator_ids
        )
        request = _request(
            strategy=AllocationStrategy.CALIBRATION_THEN_PARTITION,
            records=records,
            annotators=annotators,
            seed=rng.randint(0, 100_000),
            overlap=OverlapRule(count=overlap_count, assignees_per_record=overlap_width),
            calibration=CalibrationRule(
                count=calibration_count,
                expected_annotator_ids=expected_annotators,
            ),
        )

        plan = plan_allocation(request)
        gate = plan.gates[0]
        calibration_assignments = [
            item for item in plan.assignments if item.phase == AssignmentPhase.CALIBRATION
        ]
        production_assignments = [
            item for item in plan.assignments if item.phase == AssignmentPhase.PRODUCTION
        ]
        calibration_record_ids = {item.record_id for item in calibration_assignments}

        assert gate.plan_fingerprint == plan.fingerprint
        assert len(gate.expectations) == calibration_count
        assert calibration_record_ids == {item.record_id for item in gate.expectations}
        assert len(calibration_assignments) == calibration_count * expected_count
        assert all(
            item.expected_annotator_ids == expected_annotators for item in gate.expectations
        )
        assert not calibration_record_ids & {item.record_id for item in production_assignments}
        assert all(item.gate_id == gate.gate_id for item in production_assignments)
        assert len(production_assignments) == production_count + overlap_count * (overlap_width - 1)
        assert sum(item.assigned_rows for item in plan.annotator_loads) == len(plan.assignments)


def test_plan_manifest_is_frozen_and_json_serializable():
    request = _request(
        strategy=AllocationStrategy.FIXED_PARTITION,
        records=_records(2, "immutable"),
        annotators=(AnnotatorSpec("annotator-0", "cohort-property", 2),),
        seed=1,
    )
    plan = plan_allocation(request)

    with pytest.raises(FrozenInstanceError):
        plan.seed = 2
    with pytest.raises(FrozenInstanceError):
        plan.assignments[0].record_id = "changed"
    assert json.loads(json.dumps(plan.to_dict(), sort_keys=True))["fingerprint"] == plan.fingerprint


@pytest.mark.parametrize(
    ("annotators", "overlap", "expected_code"),
    [
        ((), OverlapRule(), "empty_cohort"),
        (
            (AnnotatorSpec("annotator-0", "cohort-a", 4), AnnotatorSpec("annotator-1", "cohort-b", 4)),
            OverlapRule(),
            "mixed_cohort",
        ),
        (
            (AnnotatorSpec("annotator-0", "cohort-a", 4),),
            OverlapRule(count=1, assignees_per_record=2),
            "cohort_too_small",
        ),
    ],
)
def test_cohort_boundary_validation(annotators, overlap, expected_code):
    request = _request(
        strategy=AllocationStrategy.FIXED_PARTITION,
        records=_records(1, expected_code),
        annotators=annotators,
        seed=1,
        overlap=overlap,
    )

    report = validate_allocation_request(request)

    assert expected_code in {item.code for item in report.issues}


def test_selector_conflicts_and_excess_are_structured():
    records = _records(2, "selector")
    annotators = (AnnotatorSpec("annotator-0", "cohort-property", 10),)
    conflict = _request(
        strategy=AllocationStrategy.FIXED_PARTITION,
        records=records,
        annotators=annotators,
        seed=1,
        overlap=OverlapRule(record_ids=(records[0].record_id,), count=1),
    )
    excess = replace(conflict, overlap=OverlapRule(count=3))

    assert [item.code for item in validate_allocation_request(conflict).issues] == [
        "selector_conflict"
    ]
    assert [item.code for item in validate_allocation_request(excess).issues] == [
        "selector_exceeds_records"
    ]


def test_calibration_capacity_and_excluded_overlap_fail_structurally():
    records = _records(3, "calibration-invalid")
    capacity_request = _request(
        strategy=AllocationStrategy.CALIBRATION_THEN_PARTITION,
        records=records,
        annotators=(AnnotatorSpec("annotator-0", "cohort-property", 1),),
        seed=1,
        calibration=CalibrationRule(count=2),
    )
    excluded_overlap_request = _request(
        strategy=AllocationStrategy.CALIBRATION_THEN_PARTITION,
        records=records,
        annotators=(
            AnnotatorSpec("annotator-0", "cohort-property", 10),
            AnnotatorSpec("annotator-1", "cohort-property", 10),
        ),
        seed=1,
        overlap=OverlapRule(record_ids=(records[0].record_id,), assignees_per_record=2),
        calibration=CalibrationRule(record_ids=(records[0].record_id,)),
    )

    capacity_codes = {item.code for item in validate_allocation_request(capacity_request).issues}
    overlap_codes = {item.code for item in validate_allocation_request(excluded_overlap_request).issues}

    assert "annotator_capacity_insufficient" in capacity_codes
    assert "insufficient_capacity" in capacity_codes
    assert overlap_codes == {"unknown_record_id"}


def test_algorithm_version_participates_in_selection_and_fingerprint():
    request = _request(
        strategy=AllocationStrategy.FIXED_PARTITION,
        records=_records(20, "algorithm-version"),
        annotators=tuple(
            AnnotatorSpec(f"annotator-{index}", "cohort-property", 10) for index in range(3)
        ),
        seed=4,
        overlap=OverlapRule(rate=0.25, assignees_per_record=2),
    )

    first = plan_allocation(request)
    second = plan_allocation(replace(request, algorithm_version="allocation-property-v2"))

    assert first.fingerprint != second.fingerprint
    assert tuple((item.record_id, item.assignee_id) for item in first.assignments) != tuple(
        (item.record_id, item.assignee_id) for item in second.assignments
    )
