# Allocation planner contract

`llm_labeling_scaffold.allocation` is a pure, deterministic planner. It accepts immutable task,
source manifest, record, cohort, capacity, seed, algorithm version, overlap, and calibration DTOs.
It returns the complete assignment matrix and provisioning groups; callers must persist the
returned plan rather than reconstructing assignments from the seed.

## Load accounting

`AnnotatorLoad.assigned_rows` only counts rows bound to a named annotator. In `shared_queue`, it is
always zero because Argilla's shared task distribution cannot enforce a per-user quota.

`AnnotatorLoad.advisory_reserved_shared_rows` is only a deterministic capacity-feasibility plan.
Its `mode` is `shared_queue_advisory`; UI and API consumers must not present it as assigned,
promised, or enforceable per-user workload.

## Record materialization

`Assignment` represents expected answers. A calibration record therefore has one assignment per
expected annotator. `DatasetGroup.record_ids` represents physical record materialization and is
deduplicated, so the calibration dataset still contains each source record exactly once.

For `shared_queue`, each source record also appears once in the assignment matrix. Records with
different `required_submissions` values are placed in separate dataset groups with matching
`min_submitted` values.

## Fingerprints and gates

The planner canonicalizes normalized DTO data and hashes the full generated plan. Calibration
gates bind the plan fingerprint, task revision hash, source manifest hash, cohort, calibration
record IDs, and expected responder sets. Production assignments carry the gate ID that must pass
before dispatch.
