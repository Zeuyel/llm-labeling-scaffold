import test from "node:test";
import assert from "node:assert/strict";
import {
  bindAnnotator,
  createCohort,
  getAnnotator,
  getAnnotators,
  getCohort,
  getCohorts,
  getMembers,
  managementIdempotencyKey,
  provisionAnnotator,
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
    await provisionAnnotator({ workspace: "workspace-a", principal_id: "principal-1", scaffold_user_id: "legacy-user", initial_password: "one-time" }, { idempotencyKey: "key-provision" });
    await bindAnnotator({ workspace: "workspace-a", principal_id: "principal-1", scaffold_user_id: "legacy-user", argilla_user_id: "argilla-1" }, { idempotencyKey: "key-bind" });
    await verifyAnnotator("annotator-1", "workspace-a", { idempotencyKey: "key-verify" });
    await getCohorts("workspace-a");
    await getCohort("cohort-1", "workspace-a");
    await createCohort({ workspace: "workspace-a", name: "reviewers", default_capacity: 4, member_annotator_ids: ["annotator-1"] }, { idempotencyKey: "key-cohort-create" });
    await updateCohort("cohort-1", { workspace: "workspace-a", name: "reviewers", default_capacity: 4, expected_revision: 2 }, { idempotencyKey: "key-cohort-update" });
    await replaceCohortMembers("cohort-1", { workspace: "workspace-a", member_annotator_ids: ["annotator-1"], expected_revision: 2 }, { idempotencyKey: "key-cohort-members" });
    await getMembers("workspace-a");
  } finally {
    globalThis.fetch = previousFetch;
  }

  assert.deepEqual(calls.map(({ path }) => path), [
    "/api/annotators?workspace=workspace-a",
    "/api/annotators/annotator-1?workspace=workspace-a",
    "/api/annotators/provision",
    "/api/annotators/bind",
    "/api/annotators/annotator-1/verify",
    "/api/cohorts?workspace=workspace-a",
    "/api/cohorts/cohort-1?workspace=workspace-a",
    "/api/cohorts",
    "/api/cohorts/cohort-1",
    "/api/cohorts/cohort-1/members",
    "/api/members?workspace=workspace-a",
  ]);
  assert.equal(JSON.parse(calls[2].opts.body).initial_password, "one-time");
  assert.equal(JSON.parse(calls[2].opts.body).workspace, "workspace-a");
  assert.equal(JSON.parse(calls[2].opts.body).principal_id, "principal-1");
  assert.equal(JSON.parse(calls[2].opts.body).scaffold_user_id, undefined);
  assert.equal(JSON.parse(calls[3].opts.body).workspace, "workspace-a");
  assert.equal(JSON.parse(calls[3].opts.body).principal_id, "principal-1");
  assert.equal(JSON.parse(calls[3].opts.body).scaffold_user_id, undefined);
  assert.equal(JSON.parse(calls[4].opts.body).annotator_id, "annotator-1");
  assert.equal(JSON.parse(calls[4].opts.body).workspace, "workspace-a");
  assert.equal(JSON.parse(calls[7].opts.body).workspace, "workspace-a");
  assert.equal(JSON.parse(calls[8].opts.body).expected_revision, 2);
  assert.equal(JSON.parse(calls[8].opts.body).workspace, "workspace-a");
  assert.deepEqual(JSON.parse(calls[9].opts.body).member_annotator_ids, ["annotator-1"]);
  assert.equal(JSON.parse(calls[9].opts.body).workspace, "workspace-a");
  assert.deepEqual(
    calls.filter(({ opts }) => opts.headers?.["Idempotency-Key"]).map(({ opts }) => opts.headers["Idempotency-Key"]),
    ["key-provision", "key-bind", "key-verify", "key-cohort-create", "key-cohort-update", "key-cohort-members"],
  );
  assert.equal(calls.some(({ path }) => path === "/api/action" || path.includes("/api/argilla/")), false);
});

test("管理 API 没有 workspace 时拒绝发送请求", () => {
  const cases = [
    () => getAnnotators(""),
    () => getMembers(""),
    () => provisionAnnotator({ principal_id: "principal-1", initial_password: "one-time" }, { idempotencyKey: "key" }),
    () => updateCohort("cohort-1", { name: "reviewers" }, { idempotencyKey: "key" }),
  ];
  for (const operation of cases) {
    assert.throws(operation, (error) => {
      assert.equal(error.code, "workspace_required");
      assert.equal(error.status, 422);
      return true;
    });
  }
});

test("前端生成的管理幂等键是非空且满足后端长度边界", () => {
  const key = managementIdempotencyKey("cohort.members", "workspace-a", "cohort-1");
  assert.ok(key.length > 0);
  assert.ok(key.length <= 200);
  assert.doesNotMatch(key, /\s/);
});

test("所有管理写操作都拒绝缺失或空的 Idempotency-Key", () => {
  const cases = [
    () => provisionAnnotator({ workspace: "workspace-a", principal_id: "principal-1", initial_password: "one-time" }),
    () => bindAnnotator({ workspace: "workspace-a", principal_id: "principal-1", argilla_user_id: "argilla-1" }),
    () => verifyAnnotator("annotator-1", "workspace-a"),
    () => createCohort({ workspace: "workspace-a", name: "reviewers", default_capacity: 4, member_annotator_ids: [] }),
    () => updateCohort("cohort-1", { workspace: "workspace-a", name: "reviewers", default_capacity: 4, expected_revision: 2 }, { idempotencyKey: " " }),
    () => replaceCohortMembers("cohort-1", { workspace: "workspace-a", member_annotator_ids: [], expected_revision: 2 }),
  ];
  for (const operation of cases) {
    assert.throws(operation, (error) => {
      assert.equal(error.code, "missing_idempotency_key");
      assert.equal(error.status, 422);
      return true;
    });
  }
});

test("专用 API 将后端错误码映射为安全中文错误，不回显响应正文", async () => {
  const previousFetch = globalThis.fetch;
  const responses = [
    { status: 422, payload: { code: "invalid_idempotency_key", error: "非法 key one-time-password" } },
    { status: 409, payload: { code: "idempotency_conflict", error: "冲突细节 one-time-password" } },
  ];
  let responseIndex = 0;
  globalThis.fetch = async () => ({
    ok: false,
    status: responses[responseIndex].status,
    statusText: "Error",
    async text() {
      return JSON.stringify(responses[responseIndex++].payload);
    },
  });

  try {
    await assert.rejects(
      () => getCohorts("workspace-a"),
      (error) => {
        assert.equal(error.code, "invalid_idempotency_key");
        assert.equal(error.status, 422);
        assert.equal(error.message, "幂等键无效");
        assert.doesNotMatch(error.message, /one-time-password/);
        return true;
      },
    );
    await assert.rejects(
      () => getCohorts("workspace-a"),
      (error) => {
        assert.equal(error.code, "idempotency_conflict");
        assert.equal(error.status, 409);
        assert.equal(error.message, "幂等键与既有请求冲突，请刷新后重试");
        assert.doesNotMatch(error.message, /one-time-password/);
        return true;
      },
    );
  } finally {
    globalThis.fetch = previousFetch;
  }
});
