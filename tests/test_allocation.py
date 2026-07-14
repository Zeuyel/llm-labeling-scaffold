from __future__ import annotations

import hashlib

import pytest

from llm_labeling_scaffold.allocation import (
    AllocationRequest,
    AllocationStrategy,
    AnnotatorSpec,
    CalibrationRule,
    ManifestKind,
    OverlapRule,
    RecordSpec,
    SourceManifestRef,
    TaskRevisionRef,
    canonical_hash,
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
