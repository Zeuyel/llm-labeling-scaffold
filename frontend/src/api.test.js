import test from "node:test";
import assert from "node:assert/strict";

import {
  getTaskSummary,
  getTasks,
  getWorkflowStatus,
  listWorkflows,
  resumeWorkflow,
  startWorkflow,
} from "./api.js";

const originalFetch = globalThis.fetch;

test.afterEach(() => {
  globalThis.fetch = originalFetch;
});

test("workflow API helpers use the panel contract", async () => {
  const requests = [];
  globalThis.fetch = async (path, options = {}) => {
    requests.push({ path, options });
    const response = path === "/api/workflow?task_id=task%2F1"
      ? { workflows: [{ workflow_id: "wf-1" }] }
      : path === "/api/workflow/status?task_id=task%2F1&workflow_id=wf-1"
        ? { workflow: { workflow_id: "wf-1", status: "waiting_for_human" } }
        : { workflow: { workflow_id: "wf-1", status: "running" } };
    return { ok: true, json: async () => response };
  };

  const started = await startWorkflow("task/1", { profile: "manual_labeling_cv_v1" });
  const resumed = await resumeWorkflow("task/1", "wf-1", { params: { retry: true } });
  const listed = await listWorkflows("task/1");
  const status = await getWorkflowStatus("task/1", "wf-1");

  assert.equal(started.status, "running");
  assert.equal(resumed.status, "running");
  assert.deepEqual(listed.workflows, [{ workflow_id: "wf-1" }]);
  assert.equal(status.status, "waiting_for_human");
  assert.deepEqual(
    requests.map(({ path }) => path),
    [
      "/api/workflow/start",
      "/api/workflow/resume",
      "/api/workflow?task_id=task%2F1",
      "/api/workflow/status?task_id=task%2F1&workflow_id=wf-1",
    ],
  );
  assert.deepEqual(JSON.parse(requests[0].options.body), {
    task_id: "task/1",
    profile: "manual_labeling_cv_v1",
  });
  assert.deepEqual(JSON.parse(requests[1].options.body), {
    task_id: "task/1",
    workflow_id: "wf-1",
    params: { retry: true },
  });
});

test("task list and summary helpers carry the workspace contract", async () => {
  const requests = [];
  globalThis.fetch = async (path, options = {}) => {
    requests.push({ path, options });
    return { ok: true, json: async () => ({ tasks: [], task: {} }) };
  };

  await getTasks("workspace-a", {
    include_archived: true,
    after: "cursor-1",
    limit: 25,
  });
  await getTaskSummary("task/1", "workspace-a");

  assert.deepEqual(
    requests.map(({ path }) => path),
    [
      "/api/tasks?workspace=workspace-a&include_archived=true&after=cursor-1&limit=25",
      "/api/task/summary?task_id=task%2F1&workspace=workspace-a",
    ],
  );
});
