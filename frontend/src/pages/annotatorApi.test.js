import test from "node:test";
import assert from "node:assert/strict";
import {
  bindAnnotator,
  createAnnotator,
  getAnnotator,
  getAnnotators,
  getCohort,
  getCohorts,
  replaceCohortMembers,
  updateCohort,
  verifyAnnotator,
} from "../api.js";

test("标注人员和人员组 API 使用专用 workspace 路径", async () => {
  const previousFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (path, opts = {}) => {
    calls.push({ path, opts });
    return { ok: true, async json() { return { annotator_id: "annotator-1", cohort_id: "cohort-1" }; } };
  };

  try {
    await getAnnotators("workspace-a");
    await getAnnotator("annotator-1", "workspace-a");
    await createAnnotator({ workspace: "workspace-a", scaffold_user_id: "principal-1", initial_password: "one-time" });
    await bindAnnotator({ workspace: "workspace-a", scaffold_user_id: "principal-1", argilla_user_id: "argilla-1" });
    await verifyAnnotator("annotator-1", "workspace-a");
    await getCohorts("workspace-a");
    await getCohort("cohort-1", "workspace-a");
    await updateCohort("cohort-1", { workspace: "workspace-a", name: "reviewers", default_capacity: 4, expected_revision: 2 });
    await replaceCohortMembers("cohort-1", { workspace: "workspace-a", member_annotator_ids: ["annotator-1"], expected_revision: 2 });
  } finally {
    globalThis.fetch = previousFetch;
  }

  assert.deepEqual(calls.map(({ path }) => path), [
    "/api/annotators?workspace=workspace-a",
    "/api/annotators/annotator-1?workspace=workspace-a",
    "/api/annotators",
    "/api/annotators/bind",
    "/api/annotators/annotator-1/verify",
    "/api/cohorts?workspace=workspace-a",
    "/api/cohorts/cohort-1?workspace=workspace-a",
    "/api/cohorts/cohort-1",
    "/api/cohorts/cohort-1/members",
  ]);
  assert.equal(JSON.parse(calls[2].opts.body).initial_password, "one-time");
  assert.equal(JSON.parse(calls[4].opts.body).annotator_id, "annotator-1");
  assert.equal(JSON.parse(calls[7].opts.body).expected_revision, 2);
  assert.deepEqual(JSON.parse(calls[8].opts.body).member_annotator_ids, ["annotator-1"]);
  assert.equal(calls.some(({ path }) => path === "/api/action" || path.includes("/api/argilla/")), false);
});

test("管理 API 没有 workspace 时拒绝发送请求", () => {
  assert.throws(() => getAnnotators(""), /显式提供 Scaffold 工作区/);
  assert.throws(() => createAnnotator({ scaffold_user_id: "principal-1", initial_password: "one-time" }), /显式提供 Scaffold 工作区/);
  assert.throws(() => updateCohort("cohort-1", { name: "reviewers" }), /显式提供 Scaffold 工作区/);
});
