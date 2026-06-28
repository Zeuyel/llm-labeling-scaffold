import test from "node:test";
import assert from "node:assert/strict";
import {
  annotationJobDetailActions,
  annotationJobGoldAction,
  annotationPageSections,
} from "./annotationPageState.js";

test("annotation page defaults to the annotation jobs table as the primary resource", () => {
  const sections = annotationPageSections({
    annotationJobs: [{ annotation_id: "round_1" }],
    decisionArtifacts: [{ decision_id: "round_1" }],
    agreementAudits: [{ audit_id: "round_1" }],
    debugRuns: [{ run_id: "debug_1" }],
  });

  assert.equal(sections.primary.key, "annotation_jobs");
  assert.equal(sections.primary.title, "标注任务");
  assert.equal(sections.primary.count, 1);
  assert.equal(sections.primary.defaultOpen, true);
  assert.equal(sections.primary.createAction, "create_annotation_job");
  assert.equal(sections.primary.detailTarget, "annotation_job_detail");
});

test("annotation page keeps downstream resources as collapsed secondary lists", () => {
  const sections = annotationPageSections({
    annotationJobs: [],
    decisionArtifacts: [{ decision_id: "round_1" }, { decision_id: "round_2" }],
    agreementAudits: [{ audit_id: "round_1" }],
  });

  assert.deepEqual(
    [
      sections.decisionArtifacts,
      sections.agreementAudits,
      sections.debugRuns,
    ].map((section) => [section.key, section.count, section.defaultOpen, section.detailTarget]),
    [
      ["decision_artifacts", 2, false, "decision_artifact_detail"],
      ["agreement_audits", 1, false, "agreement_audit_detail"],
      ["debug_runs", 0, false, "debug_run_actions"],
    ],
  );
});

test("annotation job detail exposes archive but blocks edit and destructive delete", () => {
  const actions = annotationJobDetailActions({
    job: { annotation_id: "round_1" },
    decisions: [],
  });

  assert.equal(actions.edit.enabled, false);
  assert.match(actions.edit.disabledReason, /新建标注任务/);
  assert.equal(actions.delete.enabled, false);
  assert.match(actions.delete.disabledReason, /不支持.*删除/);
  assert.equal(actions.archive.enabled, true);
});

test("annotation job archive is blocked once downstream decisions exist", () => {
  const withDecision = annotationJobDetailActions({
    job: { annotation_id: "round_1" },
    decisions: [{ decision_id: "round_1" }],
  });
  const busy = annotationJobDetailActions({
    busy: true,
    job: { annotation_id: "round_1" },
    decisions: [],
  });

  assert.equal(withDecision.archive.enabled, false);
  assert.equal(withDecision.archive.disabledReason, "标注任务已有下游标注结果，不能归档。");
  assert.equal(busy.archive.enabled, false);
  assert.equal(busy.archive.disabledReason, "当前有操作正在执行。");
});

test("annotation job archive is blocked for archived states in either locale", () => {
  for (const state of ["archived", "已归档"]) {
    const actions = annotationJobDetailActions({
      job: { annotation_id: "round_1", state },
      decisions: [],
    });

    assert.equal(actions.archive.enabled, false);
    assert.equal(actions.archive.disabledReason, "标注任务已归档。");
  }
});

test("annotation job gold action requires a passing agreement audit", () => {
  assert.deepEqual(annotationJobGoldAction({ audits: [] }), {
    enabled: false,
    disabledReason: "通过一致性检查后构建 Gold",
  });
  assert.deepEqual(annotationJobGoldAction({ audits: [{ passed: false }, { passed: true }] }), {
    enabled: true,
    disabledReason: "",
  });
});
