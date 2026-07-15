from __future__ import annotations

import hashlib
import json
import random
from collections import Counter, defaultdict
from dataclasses import FrozenInstanceError, replace

import pytest

from llm_labeling_scaffold import allocation as allocation_module
from llm_labeling_scaffold.allocation import (
    AllocationRequest,
    AllocationStrategy,
    AllocationValidationError,
    AnnotatorLoadMode,
    AnnotatorSpec,
    AssignmentPhase,
    CalibrationRule,
    OverlapRule,
    RecordSpec,
    SourceManifestRef,
    TaskRevisionRef,
    canonical_hash,
    plan_allocation,
    preview_allocation,
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
    overlap_rules: tuple[OverlapRule, ...] = (),
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
        algorithm_version="allocation-v1",
        overlap_rules=overlap_rules,
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
            overlap_rules=(
                OverlapRule(count=overlap_count, required_submissions=overlap_width),
            ),
        )

        report = validate_allocation_request(request)
        if not report.ok:
            with pytest.raises(AllocationValidationError) as exc_info:
                plan_allocation(request)
            assert exc_info.value.report == report
            assert preview_allocation(request).blocking_errors == report.blocking_errors
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
        assert len({(item.record_id, item.assignee_id) for item in plan.assignments}) == len(
            plan.assignments
        )

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
        assert sum(item.assigned_rows for item in plan.annotator_loads) == len(plan.assignments)
        for load in plan.annotator_loads:
            assert load.mode == AnnotatorLoadMode.DIRECT_ASSIGNMENT
            assert load.assigned_rows == assignment_counts[load.annotator_id]
            assert load.assigned_rows <= load.capacity
            assert load.remaining_capacity == load.capacity - load.assigned_rows


@pytest.mark.parametrize("record_count", [0, 1, 7, 19])
@pytest.mark.parametrize("capacities", [(8, 8, 8, 8), (4, 7, 9, 15)])
@pytest.mark.parametrize("overlap_width", [2, 3])
@pytest.mark.parametrize("seed", [0, 17, 999])
def test_fixed_partition_explicit_property_grid(
    record_count,
    capacities,
    overlap_width,
    seed,
):
    overlap_count = min(record_count, record_count // 3)
    records = _records(record_count, f"grid-{record_count}-{capacities}-{overlap_width}-{seed}")
    annotators = tuple(
        AnnotatorSpec(f"annotator-{index}", "cohort-property", capacity)
        for index, capacity in enumerate(capacities)
    )
    request = _request(
        strategy=AllocationStrategy.FIXED_PARTITION,
        records=records,
        annotators=annotators,
        seed=seed,
        overlap_rules=(OverlapRule(count=overlap_count, required_submissions=overlap_width),),
    )

    first = plan_allocation(request)
    repeated = plan_allocation(request)
    reversed_input = plan_allocation(
        replace(
            request,
            records=tuple(reversed(records)),
            annotators=tuple(reversed(annotators)),
        )
    )

    assert first.fingerprint == repeated.fingerprint == reversed_input.fingerprint
    assert first == repeated == reversed_input
    assert first.fingerprint == canonical_hash(first.fingerprint_payload())
    assert len({(item.record_id, item.assignee_id) for item in first.assignments}) == len(
        first.assignments
    )

    by_record = defaultdict(list)
    for assignment in first.assignments:
        by_record[assignment.record_id].append(assignment)
    assert len(by_record) == record_count
    assert sum(len(items) == overlap_width for items in by_record.values()) == overlap_count
    assert all(
        len({item.assignee_id for item in items}) == len(items) for items in by_record.values()
    )

    expected_rows = record_count + overlap_count * (overlap_width - 1)
    assert len(first.assignments) == expected_rows
    assert sum(item.assigned_rows for item in first.annotator_loads) == expected_rows
    assert all(item.assigned_rows <= item.capacity for item in first.annotator_loads)
    preview = preview_allocation(request)
    assert preview.ready
    assert preview.input_fingerprint == first.input_fingerprint
    assert preview.plan_fingerprint == first.fingerprint
    assert preview.algorithm_version == "allocation-v1"
    assert sum(item.assigned_rows for item in preview.annotator_loads) == expected_rows
    assert preview.workspace_requirements == first.to_preview().workspace_requirements
    assert preview.dataset_requirements == first.to_preview().dataset_requirements


@pytest.mark.parametrize("record_count", [8, 17])
@pytest.mark.parametrize("capacities", [(12, 12, 12, 12), (8, 10, 14, 18)])
@pytest.mark.parametrize("seed", [1, 29, 6101])
def test_multiple_overlap_tier_properties(record_count, capacities, seed):
    records = _records(record_count, f"multi-{record_count}-{capacities}-{seed}")
    k2_count = max(1, record_count // 4)
    k3_count = max(1, record_count // 5)
    k2_ids = tuple(item.record_id for item in records[:k2_count])
    k3_ids = tuple(item.record_id for item in records[k2_count : k2_count + k3_count])
    overlap_rules = (
        OverlapRule(record_ids=k2_ids, required_submissions=2),
        OverlapRule(record_ids=k3_ids, required_submissions=3),
    )
    annotators = tuple(
        AnnotatorSpec(f"annotator-{index}", "cohort-property", capacity)
        for index, capacity in enumerate(capacities)
    )
    fixed_request = _request(
        strategy=AllocationStrategy.FIXED_PARTITION,
        records=records,
        annotators=annotators,
        seed=seed,
        overlap_rules=overlap_rules,
    )
    shared_request = replace(fixed_request, strategy=AllocationStrategy.SHARED_QUEUE)

    fixed = plan_allocation(fixed_request)
    reversed_fixed = plan_allocation(
        replace(
            fixed_request,
            records=tuple(reversed(records)),
            annotators=tuple(reversed(annotators)),
        )
    )
    shared = plan_allocation(shared_request)

    assert fixed == reversed_fixed
    assert fixed.fingerprint == canonical_hash(fixed.fingerprint_payload())
    assert len({(item.record_id, item.assignee_id) for item in fixed.assignments}) == len(
        fixed.assignments
    )
    by_record = defaultdict(list)
    for assignment in fixed.assignments:
        by_record[assignment.record_id].append(assignment)
    assert all(len(by_record[record_id]) == 2 for record_id in k2_ids)
    assert all(len(by_record[record_id]) == 3 for record_id in k3_ids)
    assert all(
        len({item.assignee_id for item in items}) == len(items) for items in by_record.values()
    )
    expected_rows = record_count + k2_count + 2 * k3_count
    assert len(fixed.assignments) == expected_rows
    assert sum(item.assigned_rows for item in fixed.annotator_loads) == expected_rows
    assert all(item.assigned_rows <= item.capacity for item in fixed.annotator_loads)
    assert len(shared.assignments) == record_count
    assert {item.min_submitted for item in shared.dataset_groups} == {1, 2, 3}


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
        overlap_rules=(OverlapRule(rate=rate, required_submissions=2),),
    )

    plan = plan_allocation(request)

    assert len(plan.assignments) == record_count
    assert len({item.record_id for item in plan.assignments}) == record_count
    assert all(item.assignee_id is None for item in plan.assignments)
    assert all(item.mode == AnnotatorLoadMode.SHARED_QUEUE_ADVISORY for item in plan.annotator_loads)
    assert all(item.assigned_rows == 0 for item in plan.annotator_loads)
    assert sum(item.advisory_reserved_shared_rows for item in plan.annotator_loads) == required_rows
    assert len(plan.overlap_matrix.pool_requirements) == expected_overlap_count
    expected_min_submitted = set()
    if record_count > expected_overlap_count:
        expected_min_submitted.add(1)
    if expected_overlap_count:
        expected_min_submitted.add(2)
    assert {item.min_submitted for item in plan.dataset_groups} == expected_min_submitted
    preview = preview_allocation(request)
    assert all(item.mode == AnnotatorLoadMode.SHARED_QUEUE_ADVISORY for item in preview.annotator_loads)
    assert all(item.assigned_rows == 0 for item in preview.annotator_loads)


def test_randomized_calibration_properties():
    rng = random.Random(6119)

    for case in range(40):
        annotator_count = rng.randint(2, 5)
        record_count = rng.randint(3, 18)
        calibration_count = rng.randint(1, record_count - 1)
        production_count = record_count - calibration_count
        expected_count = annotator_count
        overlap_count = rng.randint(0, production_count)
        overlap_width = rng.randint(2, annotator_count)
        records = _records(record_count, f"calibration-{case}")
        annotator_ids = tuple(f"annotator-{index}" for index in range(annotator_count))
        expected_annotators = annotator_ids
        required_capacity = record_count * max(annotator_count, overlap_width)
        annotators = tuple(
            AnnotatorSpec(item, "cohort-property", required_capacity) for item in annotator_ids
        )
        request = _request(
            strategy=AllocationStrategy.CALIBRATION_THEN_PARTITION,
            records=records,
            annotators=annotators,
            seed=rng.randint(0, 100_000),
            overlap_rules=(
                OverlapRule(count=overlap_count, required_submissions=overlap_width),
            ),
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
        calibration_dataset = next(
            item for item in plan.dataset_groups if item.phase == AssignmentPhase.CALIBRATION
        )
        assert len(calibration_dataset.record_ids) == calibration_count
        assert len(calibration_dataset.record_ids) == len(set(calibration_dataset.record_ids))
        assert len(calibration_dataset.assignment_ids) == calibration_count * expected_count
        preview = preview_allocation(request)
        assert preview.calibration_set is not None
        assert preview.calibration_set.record_ids == calibration_dataset.record_ids
        preview_calibration_dataset = next(
            item for item in preview.dataset_requirements if item.phase == AssignmentPhase.CALIBRATION
        )
        assert preview_calibration_dataset.record_count == calibration_count


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
            OverlapRule(count=1, required_submissions=2),
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
        overlap_rules=(overlap,),
    )

    report = validate_allocation_request(request)

    assert expected_code in {item.code for item in report.blocking_errors}


def test_selector_conflicts_and_excess_are_structured():
    records = _records(2, "selector")
    annotators = (
        AnnotatorSpec("annotator-0", "cohort-property", 10),
        AnnotatorSpec("annotator-1", "cohort-property", 10),
    )
    conflict = _request(
        strategy=AllocationStrategy.FIXED_PARTITION,
        records=records,
        annotators=annotators,
        seed=1,
        overlap_rules=(OverlapRule(record_ids=(records[0].record_id,), count=1),),
    )
    excess = replace(conflict, overlap_rules=(OverlapRule(count=3),))

    assert [item.code for item in validate_allocation_request(conflict).blocking_errors] == [
        "selector_conflict"
    ]
    assert [item.code for item in validate_allocation_request(excess).blocking_errors] == [
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
        overlap_rules=(
            OverlapRule(record_ids=(records[0].record_id,), required_submissions=2),
        ),
        calibration=CalibrationRule(record_ids=(records[0].record_id,)),
    )

    capacity_codes = {item.code for item in validate_allocation_request(capacity_request).blocking_errors}
    overlap_codes = {item.code for item in validate_allocation_request(excluded_overlap_request).blocking_errors}

    assert "annotator_capacity_insufficient" in capacity_codes
    assert overlap_codes == {"unknown_record_id"}


def test_unknown_algorithm_version_fails_fast():
    request = _request(
        strategy=AllocationStrategy.FIXED_PARTITION,
        records=_records(20, "algorithm-version"),
        annotators=tuple(
            AnnotatorSpec(f"annotator-{index}", "cohort-property", 10) for index in range(3)
        ),
        seed=4,
        overlap_rules=(OverlapRule(rate=0.25, required_submissions=2),),
    )

    unsupported = replace(request, algorithm_version="allocation-v2")
    report = validate_allocation_request(unsupported)

    assert [item.code for item in report.blocking_errors] == ["unsupported_algorithm_version"]
    with pytest.raises(AllocationValidationError):
        plan_allocation(unsupported)


def test_large_plan_uses_linearithmic_seed_rank_budget_and_is_order_independent(monkeypatch):
    records = _records(2_000, "performance")
    capacities = (220, 240, 260, 280, 300, 320, 340, 360, 380, 400)
    annotators = tuple(
        AnnotatorSpec(f"annotator-{index}", "cohort-performance", capacity)
        for index, capacity in enumerate(capacities)
    )
    request = _request(
        strategy=AllocationStrategy.FIXED_PARTITION,
        records=records,
        annotators=annotators,
        seed=67,
        overlap_rules=(
            OverlapRule(
                record_ids=tuple(item.record_id for item in records[:200]),
                required_submissions=2,
            ),
            OverlapRule(
                record_ids=tuple(item.record_id for item in records[200:400]),
                required_submissions=3,
            ),
        ),
    )
    original_seed_rank = allocation_module._seed_rank
    calls = 0

    def seed_rank_spy(seed, algorithm_version, namespace, *parts):
        nonlocal calls
        calls += 1
        return original_seed_rank(seed, algorithm_version, namespace, *parts)

    monkeypatch.setattr(allocation_module, "_seed_rank", seed_rank_spy)

    plan = plan_allocation(request)
    plan_seed_calls = calls
    calls = 0
    preview = preview_allocation(request)
    preview_seed_calls = calls
    calls = 0
    reversed_plan = plan_allocation(
        replace(
            request,
            records=tuple(reversed(records)),
            annotators=tuple(reversed(annotators)),
        )
    )

    assert plan == reversed_plan
    assert plan.fingerprint == canonical_hash(plan.fingerprint_payload())
    assert preview.plan_fingerprint == plan.fingerprint
    assert len(plan.assignments) == 2_600
    assert plan_seed_calls < 4_000
    assert preview_seed_calls < 4_000
