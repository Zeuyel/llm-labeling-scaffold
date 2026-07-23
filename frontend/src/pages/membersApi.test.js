import test from "node:test";
import assert from "node:assert/strict";
import {
  createMemberInvitation,
  getMembers,
  revokeMember,
  revokeMemberInvitation,
  updateMemberRole,
} from "../api.js";

test("成员管理 API 查询同时读取 members 和 invitations", async () => {
  const previousFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (path, opts = {}) => {
    calls.push({ path, opts });
    return { ok: true, async json() { return { members: [], invitations: [] }; } };
  };

  try {
    const result = await getMembers("workspace-a");
    assert.deepEqual(result, { members: [], invitations: [] });
  } finally {
    globalThis.fetch = previousFetch;
  }

  assert.equal(calls.length, 1);
  assert.equal(calls[0].path, "/api/members?workspace=workspace-a");
});

test("成员邀请、角色变更和撤销发送正确的幂等键", async () => {
  const previousFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (path, opts = {}) => {
    calls.push({ path, opts });
    return { ok: true, async json() { return { invitation: {}, membership: {} }; } };
  };

  try {
    await createMemberInvitation(
      { workspace: "workspace-a", email: "member@example.com", role: "viewer" },
      { idempotencyKey: "invite-key" },
    );
    await updateMemberRole(
      "00000000-0000-4000-8000-000000000001",
      { workspace: "workspace-a", role: "admin" },
      { idempotencyKey: "role-key" },
    );
    await revokeMember(
      "00000000-0000-4000-8000-000000000001",
      { workspace: "workspace-a" },
      { idempotencyKey: "revoke-key" },
    );
    await revokeMemberInvitation(
      "00000000-0000-4000-8000-000000000002",
      { workspace: "workspace-a" },
      { idempotencyKey: "revoke-invitation-key" },
    );
  } finally {
    globalThis.fetch = previousFetch;
  }

  assert.deepEqual(calls.map(({ path }) => path), [
    "/api/members/invitations",
    "/api/members/00000000-0000-4000-8000-000000000001/role",
    "/api/members/00000000-0000-4000-8000-000000000001/revoke",
    "/api/members/invitations/00000000-0000-4000-8000-000000000002/revoke",
  ]);
  assert.deepEqual(calls.map(({ opts }) => opts.headers["Idempotency-Key"]), [
    "invite-key",
    "role-key",
    "revoke-key",
    "revoke-invitation-key",
  ]);
  assert.deepEqual(JSON.parse(calls[0].opts.body), {
    workspace: "workspace-a",
    email: "member@example.com",
    role: "viewer",
  });
  assert.deepEqual(JSON.parse(calls[1].opts.body), {
    workspace: "workspace-a",
    role: "admin",
  });
  assert.deepEqual(JSON.parse(calls[2].opts.body), { workspace: "workspace-a" });
  assert.deepEqual(JSON.parse(calls[3].opts.body), { workspace: "workspace-a" });
});

test("成员写 API 没有幂等键时拒绝发送请求", () => {
  assert.throws(
    () => createMemberInvitation({ workspace: "workspace-a", email: "member@example.com", role: "viewer" }),
    (error) => error.code === "missing_idempotency_key" && error.status === 422,
  );
  assert.throws(
    () => updateMemberRole("principal-1", { workspace: "workspace-a", role: "viewer" }),
    (error) => error.code === "missing_idempotency_key" && error.status === 422,
  );
  assert.throws(
    () => revokeMember("principal-1", { workspace: "workspace-a" }),
    (error) => error.code === "missing_idempotency_key" && error.status === 422,
  );
  assert.throws(
    () => revokeMemberInvitation("invitation-1", { workspace: "workspace-a" }),
    (error) => error.code === "missing_idempotency_key" && error.status === 422,
  );
});
