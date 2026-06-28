import test from "node:test";
import assert from "node:assert/strict";
import { annotationPageSections } from "./annotationPageState.js";

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
