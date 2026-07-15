# Allocation planner contract

`llm_labeling_scaffold.allocation` is a pure, deterministic planner. It accepts immutable task,
source manifest, record, cohort, capacity, seed, algorithm version, overlap, and calibration DTOs.
It returns the complete assignment matrix and provisioning groups; callers must persist the
returned plan rather than reconstructing assignments from the seed.

`preview_allocation()` returns a separate `AllocationPreview` whitelist for read-only UI use. Its
top-level fields include immutable task/source/cohort identifiers, `algorithm_version`,
`input_fingerprint`, `plan_fingerprint`, strategy, annotator loads, calibration set, overlap
summary, workspace requirements, dataset requirements, blocking errors, and warnings. Invalid
requests return this DTO with blocking errors and no partial workspace/dataset plan instead of
raising.

Dispatch must submit the preview's `plan_fingerprint` and reject a mismatch. Replanning after any
task revision, manifest, cohort, capacity, seed, rule, or algorithm change produces a new
fingerprint, preventing confirmation of a stale preview.

The supported algorithm allowlist currently contains only `allocation-v1`. The value is trimmed
before selector ranking, assignment ranking, normalized request hashing, and preview generation,
so surrounding whitespace cannot produce a different plan with the same input fingerprint.

## Overlap rules

`AllocationRequest.overlap_rules` is an immutable rule tuple. Each rule has its own record selector
and `required_submissions`, so one plan can contain separate `k=2`, `k=3`, or higher QC tiers.
Rules must select disjoint record sets; an intersecting match is a blocking validation error.

Only `record_id` must be unique. Separate source records may have the same `content_hash`.

## Load accounting

`AnnotatorLoad.assigned_rows` only counts rows bound to a named annotator. In `shared_queue`, it is
always zero because Argilla's shared task distribution cannot enforce a per-user quota.

`AnnotatorLoad.advisory_reserved_shared_rows` is only a deterministic capacity-feasibility plan.
Its `mode` is `shared_queue_advisory`; UI and API consumers must not present it as assigned,
promised, or enforceable per-user workload.

For `shared_queue`, `required_submissions` above cohort size remains a blocking structural error.
Total or per-user advisory capacity shortfalls are warnings: the preview remains ready because
Argilla does not enforce those capacities.

## Record materialization

`Assignment` represents expected answers. A calibration record therefore has one assignment per
expected annotator. `DatasetGroup.record_ids` represents physical record materialization and is
deduplicated, so the calibration dataset still contains each source record exactly once.

Every annotator selected for formal production must participate in calibration. A partial
`expected_annotator_ids` set is rejected before a plan is emitted.

For `shared_queue`, each source record also appears once in the assignment matrix. Records with
different `required_submissions` values are placed in separate dataset groups with matching
`min_submitted` values.

## Workspace identity

Personal workspace keys depend only on the stable annotator ID, so one annotator reuses the same
workspace across cohorts, tasks, seeds, and allocation plans. Shared workspace keys are scoped to
the cohort. Calibration workspace keys are scoped to the plan input fingerprint.

## Performance

Validation uses degree-sequence prefix checks and does not generate an assignment witness.
`plan_allocation()` and `preview_allocation()` each resolve once and generate assignments once.
Balanced annotator quotas and record realization use heaps, with work proportional to generated
assignment rows rather than repeated annotator-pair and record scans.

Local benchmark with Python 3.14.6 after one warm-up, using 10 annotators, 2,000 records,
heterogeneous capacities, 200 `k=2` records, and 200 `k=3` records (7 measured runs):

- `plan_allocation`: median `0.119s`, maximum `0.121s`.
- `preview_allocation`: median `0.119s`, maximum `0.120s`.

The property test also caps seed-rank calls below 4,000 for this fixture and verifies reversed
record/annotator input produces the same plan and fingerprint.

## Fingerprints and gates

The planner canonicalizes normalized DTO data and hashes the full generated plan. Calibration
gates bind the plan fingerprint, task revision hash, source manifest hash, cohort, calibration
record IDs, and expected responder sets. Production assignments carry the gate ID that must pass
before dispatch.
