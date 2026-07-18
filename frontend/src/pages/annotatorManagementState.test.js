import test from "node:test";
import assert from "node:assert/strict";
import {
  isVerifiedAnnotator,
  unwrapAnnotators,
  verificationLabel,
} from "./annotatorManagementState.js";

const verified = {
  annotator_id: "annotator-1",
  argilla_role: "annotator",
  membership: { present: true },
  verification: { status: "verified", label: "已验证" },
};

test("人员组候选只接受已验证 annotator 和有效成员关系", () => {
  assert.equal(isVerifiedAnnotator(verified), true);
  assert.equal(isVerifiedAnnotator({ ...verified, verification: { status: "identity_drift" } }), false);
  assert.equal(isVerifiedAnnotator({ ...verified, argilla_role: "owner" }), false);
  assert.equal(isVerifiedAnnotator({ ...verified, membership: { present: false } }), false);
  assert.equal(isVerifiedAnnotator({ ...verified, is_owner: true }), false);
});

test("验证状态缺失时 fail closed 为未验证", () => {
  assert.equal(verificationLabel({}), "未验证");
  assert.deepEqual(unwrapAnnotators({ annotators: [verified] }), [verified]);
});
