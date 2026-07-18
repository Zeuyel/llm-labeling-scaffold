import test from "node:test";
import assert from "node:assert/strict";
import {
  allocationPreviewPayload,
  confirmAvailability,
  emptyAllocationForm,
  normalizeAssignments,
  normalizePreview,
  planStatusLabel,
} from "./allocationPlanState.js";

const HASH = "a".repeat(64);

function validForm() {
  return {
    ...emptyAllocationForm({ workspace: "workspace-a" }, "task-a"),
    revision_id: "revision-1",
    revision_hash: HASH,
    source_manifest_id: "manifest-1",
    source_manifest_hash: HASH,
    cohort_id: "cohort-1",
    annotators: [{ annotator_id: "annotator-1", cohort_id: "cohort-1", capacity: 10 }],
    records_json: JSON.stringify([{ record_id: "record-1", content_hash: HASH, batch_id: "batch-1" }]),
  };
}

test("allocation preview payload follows the documented scope/request contract", () => {
  const payload = allocationPreviewPayload(validForm());

  assert.deepEqual(payload.scope, {
    workspace: "workspace-a",
    task_id: "task-a",
    revision_id: "revision-1",
    revision_hash: HASH,
  });
  assert.equal(payload.request.strategy, "shared_queue");
  assert.equal(payload.request.source_manifest.manifest_id, "manifest-1");
  assert.equal(payload.request.annotators[0].capacity, 10);
  assert.equal(payload.request.records[0].record_id, "record-1");
  assert.equal("password" in payload, false);
  assert.equal("api_key" in payload, false);
});

test("preview blocking errors and backend action state both disable confirmation", () => {
  const plan = { plan_id: "plan-1", status: "preview_ready" };
  const blocked = normalizePreview({ ready: true, blocking_errors: [{ code: "capacity", message: "容量不足" }] });
  assert.equal(confirmAvailability({ plan, preview: blocked }).enabled, false);
  assert.match(confirmAvailability({ plan, preview: blocked }).disabledReason, /容量不足/);

  const backendBlocked = { plan_id: "plan-1", status: "preview_ready", actions: { confirm: { enabled: false, reason: "校准未通过" } } };
  const ready = normalizePreview({ ready: true, blocking_errors: [] });
  assert.deepEqual(confirmAvailability({ plan: backendBlocked, preview: ready }), {
    enabled: false,
    disabledReason: "校准未通过",
  });
});

test("assignment responses expose accepted and quarantined states without inventing rows", () => {
  const assignments = normalizeAssignments({ assignments: [
    { assignment_id: "a-1", record_id: "r-1", status: "submitted" },
    { assignment_id: "a-2", record_id: "r-2", collection_status: "quarantined", quarantine_reason: "回答者不匹配" },
  ] });

  assert.equal(assignments.length, 2);
  assert.equal(assignments[0].collection_status, "submitted");
  assert.equal(assignments[1].collection_status, "quarantined");
  assert.equal(planStatusLabel({ status: "dispatching" }), "分发中");
});
