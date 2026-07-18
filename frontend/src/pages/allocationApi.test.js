import test from "node:test";
import assert from "node:assert/strict";
import {
  allocationPlanConfirmIdempotencyKey,
  confirmAllocationPlan,
  getAllocationPlanAssignments,
  getAllocationPlanProgress,
  getAllocationPlans,
  previewAllocation,
} from "../api.js";

test("allocation API helpers use dedicated paths", async () => {
  const previousFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (path, opts = {}) => {
    calls.push({ path, opts });
    return { ok: true, async json() { return { ok: true }; } };
  };

  try {
    await getAllocationPlans({ workspace: "workspace-a", taskId: "task-a" });
    await previewAllocation({ scope: { task_id: "task-a" }, request: {} });
    await getAllocationPlanProgress("plan-1", { taskId: "task-a" });
    await getAllocationPlanAssignments("plan-1", { taskId: "task-a" });
    await confirmAllocationPlan("plan-1", { plan_fingerprint: "fingerprint-1" });
  } finally {
    globalThis.fetch = previousFetch;
  }

  assert.equal(calls[0].path, "/api/allocation/plans?workspace=workspace-a&task_id=task-a");
  assert.equal(calls[1].path, "/api/allocation/preview");
  assert.equal(calls[2].path, "/api/allocation/plans/plan-1/progress?task_id=task-a");
  assert.equal(calls[3].path, "/api/allocation/plans/plan-1/assignments?task_id=task-a");
  assert.equal(calls[4].path, "/api/allocation/plans/plan-1/confirm");
  assert.equal(calls[4].opts.headers["If-Match"], "fingerprint-1");
  assert.equal(JSON.parse(calls[4].opts.body).confirm, true);
  assert.equal(
    JSON.parse(calls[4].opts.body).idempotency_key,
    allocationPlanConfirmIdempotencyKey("plan-1", "fingerprint-1"),
  );
  assert.equal(calls.some(({ path }) => path === "/api/action"), false);
});
