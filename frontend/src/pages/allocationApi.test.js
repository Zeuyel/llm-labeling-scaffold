import test from "node:test";
import assert from "node:assert/strict";
import {
  allocationPlanConfirmIdempotencyKey,
  confirmAllocationPlan,
  createAllocationPlan,
  getAllocationPlanAssignments,
  getAllocationPlanProgress,
  getAllocationPlans,
  getTaskControl,
  getTaskSamples,
  previewAllocation,
  updateAllocationPlan,
} from "../api.js";

test("allocation API helpers use dedicated paths and server headers", async () => {
  const previousFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (path, opts = {}) => {
    calls.push({ path, opts });
    return { ok: true, async json() { return { ok: true }; } };
  };

  try {
    await getAllocationPlans({ workspace: "workspace-a", taskId: "task-a" });
    await getTaskControl("task-a", "workspace-a");
    await getTaskSamples("task-a", "workspace-a");
    await previewAllocation({ scope: { task_id: "task-a" }, request: {} });
    await getAllocationPlanProgress("plan-1", { taskId: "task-a" });
    await getAllocationPlanAssignments("plan-1", { taskId: "task-a" });
    await createAllocationPlan({ plan_fingerprint: "fingerprint-1" }, { idempotencyKey: "create-key" });
    await updateAllocationPlan("plan-1", { plan_fingerprint: "fingerprint-1" }, { idempotencyKey: "update-key" });
    await confirmAllocationPlan(
      "plan-1",
      { plan_fingerprint: "fingerprint-1", idempotency_key: "must-not-be-body", preview_fingerprint: "must-not-be-body" },
      { idempotencyKey: "confirm-key" },
    );
  } finally {
    globalThis.fetch = previousFetch;
  }

  assert.equal(calls[0].path, "/api/allocation/plans?workspace=workspace-a&task_id=task-a");
  assert.equal(calls[1].path, "/api/task/control?task_id=task-a&workspace=workspace-a");
  assert.equal(calls[2].path, "/api/task/samples?task_id=task-a&workspace=workspace-a");
  assert.equal(calls[3].path, "/api/allocation/preview");
  assert.equal(calls[4].path, "/api/allocation/plans/plan-1/progress?task_id=task-a");
  assert.equal(calls[5].path, "/api/allocation/plans/plan-1/assignments?task_id=task-a");
  assert.equal(calls[6].path, "/api/allocation/plans");
  assert.equal(calls[7].path, "/api/allocation/plans/plan-1");
  assert.equal(calls[8].path, "/api/allocation/plans/plan-1/confirm");

  assert.equal(calls[6].opts.headers["Idempotency-Key"], "create-key");
  assert.equal(calls[7].opts.headers["Idempotency-Key"], "update-key");
  assert.equal(calls[8].opts.headers["Idempotency-Key"], "confirm-key");
  assert.equal(calls[8].opts.headers["If-Match"], undefined);
  assert.equal(JSON.parse(calls[8].opts.body).confirm, true);
  assert.equal(JSON.parse(calls[8].opts.body).plan_fingerprint, "fingerprint-1");
  assert.equal(JSON.parse(calls[8].opts.body).idempotency_key, undefined);
  assert.equal(JSON.parse(calls[8].opts.body).preview_fingerprint, undefined);
  assert.equal(calls.some(({ path }) => path === "/api/action"), false);
  assert.equal(calls.some(({ path }) => /annotators|cohorts/.test(path)), false);
});

test("allocation write helpers require an explicit idempotency header value", () => {
  assert.throws(() => createAllocationPlan({ plan_fingerprint: "fp" }), /Idempotency-Key/);
  assert.throws(() => updateAllocationPlan("plan-1", { plan_fingerprint: "fp" }), /Idempotency-Key/);
  assert.equal(allocationPlanConfirmIdempotencyKey("plan-1", "fp"), "allocation-plan-confirm:plan-1:fp");
});
