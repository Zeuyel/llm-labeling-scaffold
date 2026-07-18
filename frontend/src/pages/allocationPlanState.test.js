import test from "node:test";
import assert from "node:assert/strict";
import {
  allocationFormAvailability,
  allocationPlanPayload,
  allocationPreviewPayload,
  applyTrustedSource,
  confirmAvailability,
  emptyAllocationForm,
  normalizeAssignments,
  normalizePreview,
  planStatusLabel,
  trustedAllocationSourceOptions,
} from "./allocationPlanState.js";

const HASH = "a".repeat(64);

function previewRequest() {
  return {
    scope: {
      workspace: "workspace-a",
      task_id: "task-a",
      revision_id: "revision-1",
      revision_hash: HASH,
    },
    request: {
      strategy: "shared_queue",
      source_manifest: { manifest_id: "manifest-1", manifest_hash: HASH, kind: "sample" },
      records: [{ record_id: "record-1", content_hash: HASH, batch_id: "batch-1" }],
      annotators: [{ annotator_id: "annotator-1", cohort_id: "cohort-1", capacity: 10 }],
      seed: 17,
      algorithm_version: "allocation-v1",
      overlap_rules: [],
      calibration: null,
    },
  };
}

function trustedForm() {
  const source = {
    key: "sample:sample-1",
    kind: "sample",
    sample_id: "sample-1",
    manifest_id: "manifest-1",
    manifest_hash: HASH,
    preview_request: previewRequest(),
    available: true,
    disabled_reason: "",
  };
  return {
    ...applyTrustedSource(emptyAllocationForm({ workspace: "workspace-a" }, "task-a"), source),
    annotator_source_available: true,
    cohort_id: "cohort-1",
    annotators: [{ annotator_id: "annotator-1", cohort_id: "cohort-1", capacity: 10 }],
  };
}

test("trusted sources are only available when backend supplies a complete preview request", () => {
  const options = trustedAllocationSourceOptions([
    {
      sample_id: "sample-1",
      manifest: { manifest_id: "manifest-1", manifest_hash: HASH, preview_request: previewRequest() },
      batch_manifests: [{ plan_id: "batch-1" }],
    },
  ]);

  assert.equal(options.length, 2);
  assert.equal(options[0].available, true);
  assert.equal(options[1].available, false);
  assert.match(options[1].disabled_reason, /可信 preview_request/);
});

test("preview and plan payloads use trusted backend records, never a hand-entered records field", () => {
  const form = trustedForm();
  const payload = allocationPreviewPayload({ ...form, records_json: "[{}]", revision_hash: "hand-entered" });
  assert.deepEqual(payload.scope, previewRequest().scope);
  assert.deepEqual(payload.request.records, previewRequest().request.records);
  assert.equal("records_json" in payload, false);
  assert.equal(payload.scope.revision_hash, HASH);

  const planPayload = allocationPlanPayload(form, { ready: true, blocking_errors: [], plan_fingerprint: "plan-fingerprint" });
  assert.equal(planPayload.plan_fingerprint, "plan-fingerprint");
});

test("missing trusted source and unimplemented #66 personnel API disable preview and save", () => {
  const form = {
    ...emptyAllocationForm({ workspace: "workspace-a" }, "task-a"),
    revision_id: "revision-1",
    revision_hash: HASH,
    source_manifest_id: "manual-manifest",
    source_manifest_hash: HASH,
    records_json: "[]",
  };
  assert.throws(() => allocationPreviewPayload(form), /暂无可用来源/);
  assert.equal(allocationFormAvailability({ form }).enabled, false);

  const personnelUnavailable = trustedForm();
  personnelUnavailable.annotator_source_available = false;
  assert.equal(allocationFormAvailability({ form: personnelUnavailable }).enabled, false);
  assert.match(allocationFormAvailability({ form: personnelUnavailable }).disabledReason, /人员组接口尚未接入/);
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
