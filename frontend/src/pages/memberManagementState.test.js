import test from "node:test";
import assert from "node:assert/strict";
import {
  invitationStatusLabel,
  memberStatusLabel,
  roleLabel,
  unwrapMembers,
} from "./memberManagementState.js";

test("成员角色和邀请状态使用中文标签", () => {
  assert.equal(roleLabel("viewer"), "查看者");
  assert.equal(roleLabel("annotator"), "标注人员");
  assert.equal(roleLabel("experimenter"), "实验者");
  assert.equal(roleLabel("admin"), "管理员");
  assert.equal(invitationStatusLabel("pending"), "待认领");
  assert.equal(invitationStatusLabel("claimed"), "已认领");
  assert.equal(invitationStatusLabel("revoked"), "已撤销");
  assert.equal(invitationStatusLabel("expired"), "已过期");
  assert.equal(memberStatusLabel("active"), "活跃");
  assert.equal(memberStatusLabel("inactive"), "已停用");
  assert.equal(roleLabel("unknown"), "未知角色");
});

test("成员列表保留 members 和 invitations 两组数据", () => {
  const members = [{ principal_id: "principal-1", role: "viewer" }];
  const invitations = [{ invitation_id: "invitation-1", role: "annotator" }];

  assert.deepEqual(
    unwrapMembers({ workspace: "workspace-a", members, invitations }),
    { workspace: "workspace-a", members, invitations },
  );
  assert.deepEqual(unwrapMembers({ workspace: "workspace-a" }), {
    workspace: "workspace-a",
    members: [],
    invitations: [],
  });
});
