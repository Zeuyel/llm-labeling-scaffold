import test from "node:test";
import assert from "node:assert/strict";
import {
  agreementAuditCoverageLabel,
  agreementAuditIssueSummary,
  agreementAuditStatusLabel,
  agreementAuditsForDecision,
  agreementAuditsForAnnotationJob,
  annotationJobActionAvailability,
  annotationJobBatchSummary,
  annotationJobKey,
  annotationJobLabel,
  annotationJobLineageFields,
  annotationJobStatusLabel,
  batchPlanDebugFields,
  batchPlanFromManifest,
  batchPlanOptionLabel,
  defaultSuggestionId,
  decisionArtifactKey,
  decisionArtifactLabel,
  decisionArtifactLineageFields,
  decisionArtifactStatusLabel,
  agreementAuditKey,
  firstDefinedString,
  formatBatchPlanSummary,
  suggestionActionAvailability,
  suggestionStatusLabel,
  suggestionSummaryLabel,
} from "./batchPlanDisplay.js";

const technicalFieldNames = [
  "plan_id",
  "strategy_id",
  "overlap_rate",
  "overlap_item_count",
  "min_annotators_per_overlap_item",
];

test("formats batch plan as an execution summary", () => {
  const plan = batchPlanFromManifest({
    plan_id: "qc_round_1",
    strategy_id: "quality_control_overlap_v1",
    batch_count: 5,
    batch_size: 100,
    overlap_item_count: 50,
    min_annotators_per_overlap_item: 2,
  });

  assert.equal(
    formatBatchPlanSummary(plan),
    "5 个批次，100 条/批，50 条一致性样本，每条一致性样本至少 2 人标注。",
  );
});

test("visible batch plan labels do not expose technical field names", () => {
  const plan = batchPlanFromManifest({
    plan_id: "qc_round_1",
    strategy_id: "quality_control_overlap_v1",
    batch_count: 5,
    batch_size: 100,
    overlap_rate: 0.5,
    overlap_item_count: 50,
    min_annotators_per_overlap_item: 2,
  });
  const visibleText = [
    formatBatchPlanSummary(plan),
    batchPlanOptionLabel(plan, 0),
  ].join(" ");

  for (const fieldName of technicalFieldNames) {
    assert.equal(visibleText.includes(fieldName), false);
  }
});

test("advanced debug fields preserve technical audit data", () => {
  const plan = batchPlanFromManifest({
    plan_id: "qc_round_1",
    strategy_id: "quality_control_overlap_v1",
    batch_count: 5,
    batch_size: 100,
    overlap_rate: 0.5,
    overlap_item_count: 50,
    min_annotators_per_overlap_item: 2,
  });

  const fields = Object.fromEntries(batchPlanDebugFields(plan));

  assert.equal(fields.plan_id, "qc_round_1");
  assert.equal(fields.strategy_id, "quality_control_overlap_v1");
  assert.equal(fields.overlap_rate, 0.5);
  assert.equal(fields.overlap_item_count, 50);
  assert.equal(fields.min_annotators_per_overlap_item, 2);
});

test("annotation job table summary prefers readable batch information", () => {
  const summary = annotationJobBatchSummary({
    dispatch_mode: "batch_plan",
    batch_plan_id: "qc_round_1",
    batch_ids: ["batch_00001.jsonl", "batch_00002.jsonl", "batch_00003.jsonl"],
    overlap_item_ids: ["r1", "r2"],
  });

  assert.equal(summary, "3 个批次，每批条数待确认，2 条一致性样本，一致性样本标注人数待确认。");
  assert.equal(summary.includes("batch_plan_id"), false);
  assert.equal(summary.includes("dispatch_mode"), false);
});

test("lineage fields preserve zero counts", () => {
  const jobFields = Object.fromEntries(annotationJobLineageFields({
    dispatch_mode: "batch_plan",
    batch_ids: [],
    overlap_item_ids: [],
  }));
  const decisionFields = Object.fromEntries(decisionArtifactLineageFields({
    dispatch_mode: "batch_plan",
    batch_ids: [],
    overlap_item_ids: [],
    path: "/tmp/decisions.jsonl",
  }));

  assert.equal(jobFields["批次数"], 0);
  assert.equal(jobFields["一致性样本"], 0);
  assert.equal(decisionFields["批次数"], 0);
  assert.equal(decisionFields["一致性样本"], 0);
});

test("annotation job list uses business labels and readable statuses", () => {
  const job = {
    annotation_id: "round_1",
    argilla_dataset: "dataset_round_1",
    status: "succeeded",
  };

  assert.equal(annotationJobLabel(job), "round_1");
  assert.equal(annotationJobStatusLabel(job), "已推送");
  assert.equal(annotationJobStatusLabel({ status: "已分发" }), "已推送");
  assert.equal(annotationJobStatusLabel({ state: "running" }), "执行中");
  assert.equal(annotationJobStatusLabel({ status: "failed" }), "失败");
  assert.equal(annotationJobStatusLabel({ state: "incomplete" }), "记录不完整");
  assert.equal(annotationJobStatusLabel({ annotation_id: "round_2" }), "已记录");
});

test("empty annotation and decision keys do not stringify as undefined", () => {
  assert.equal(firstDefinedString(undefined, null, ""), "");
  assert.equal(annotationJobKey({}), "");
  assert.equal(decisionArtifactKey({}), "");
  assert.equal(agreementAuditKey({}), "");
  assert.notEqual(annotationJobKey({}), "undefined");
  assert.notEqual(decisionArtifactKey({}), "undefined");
  assert.notEqual(agreementAuditKey({}), "undefined");
});

test("annotation job detail action availability explains disabled states", () => {
  const job = { annotation_id: "round_1", argilla_dataset: "dataset_round_1", sample_path: "/tmp/sample.jsonl" };

  assert.deepEqual(annotationJobActionAvailability(job, [], job.sample_path), {
    pull: { enabled: true, reason: "" },
    agreement: { enabled: false, reason: "先拉回标注结果后才能运行一致性检查。", decision: null },
  });

  const withDecision = annotationJobActionAvailability(job, [{ decision_id: "round_1", path: "/tmp/decisions.jsonl" }], job.sample_path);

  assert.equal(withDecision.pull.enabled, true);
  assert.equal(withDecision.agreement.enabled, true);
  assert.equal(withDecision.agreement.decision.decision_id, "round_1");

  const missingDataset = annotationJobActionAvailability({ sample_path: "/tmp/sample.jsonl" }, [], "/tmp/sample.jsonl");
  assert.equal(missingDataset.pull.enabled, false);
  assert.equal(missingDataset.pull.reason, "缺少 Argilla 数据集名，不能拉回结果。");
});

test("annotation job detail exposes suggestion status and actions", () => {
  const job = {
    annotation_id: "round_1",
    argilla_dataset: "dataset_round_1",
    dispatch_path: "/tmp/dispatch.jsonl",
    suggestion_summary: {
      count: 2,
      records: 18,
      published_records: 8,
      latest_status: "published",
    },
  };

  assert.equal(defaultSuggestionId("codex_exec", "prompt/v1"), "codex_exec_prompt_v1");
  assert.equal(suggestionStatusLabel({ status: "generated" }), "已生成");
  assert.equal(suggestionStatusLabel({ status: "published" }), "已写入 Argilla");
  assert.equal(suggestionStatusLabel({ status: "publish_failed" }), "写入失败");
  assert.equal(suggestionSummaryLabel(job), "2 次生成，18 条覆盖，8 条已写入，最近已写入 Argilla");

  const availability = suggestionActionAvailability(job);
  assert.equal(availability.generate.enabled, true);
  assert.equal(availability.publish.enabled, true);

  const missingDataset = suggestionActionAvailability({ annotation_id: "round_1", dispatch_path: "/tmp/dispatch.jsonl" });
  assert.equal(missingDataset.generate.enabled, true);
  assert.equal(missingDataset.publish.enabled, false);
  assert.equal(missingDataset.publish.reason, "缺少 Argilla 数据集名，不能写入 Suggestions。");
});

test("annotation job detail finds related agreement audits", () => {
  const job = { annotation_id: "round_1", argilla_dataset: "dataset_round_1", sample_path: "/tmp/sample.jsonl" };
  const decisions = [{ decision_id: "round_1", path: "/tmp/decisions.jsonl" }];
  const audits = [
    { audit_id: "round_1", passed: true },
    { audit_id: "other", decisions_path: "/tmp/decisions.jsonl", passed: false },
    { audit_id: "unrelated", decisions_path: "/tmp/other.jsonl", passed: true },
  ];

  assert.deepEqual(
    agreementAuditsForAnnotationJob(job, decisions, audits).map((item) => item.audit_id),
    ["round_1", "other"],
  );
});

test("decision artifact detail uses readable labels and lineage fields", () => {
  const decision = {
    decision_id: "round_1",
    source: "argilla",
    argilla_dataset: "dataset_round_1",
    sample_id: "sample_a",
    sample_path: "/tmp/sample.jsonl",
    path: "/tmp/decisions.jsonl",
    rows: 12,
    dispatch_mode: "batch_plan",
    batch_plan_id: "qc_round_1",
    batch_ids: ["batch_1", "batch_2"],
  };

  assert.equal(decisionArtifactLabel(decision), "round_1");
  assert.equal(decisionArtifactStatusLabel(decision), "已回收");
  assert.equal(decisionArtifactStatusLabel({ decision_id: "broken" }), "记录不完整");

  const fields = Object.fromEntries(decisionArtifactLineageFields(decision));
  assert.equal(fields["来源"], "Argilla");
  assert.equal(fields["分发方式"], "按批次计划分发");
  assert.equal(fields["批次数"], 2);
});

test("agreement audit detail summarizes status, coverage, issues, and decision linkage", () => {
  const decision = {
    decision_id: "round_1",
    argilla_dataset: "dataset_round_1",
    path: "/tmp/decisions.jsonl",
    sample_path: "/tmp/sample.jsonl",
  };
  const audits = [
    {
      audit_id: "round_1",
      passed: false,
      sample_path: "/tmp/sample.jsonl",
      decisions_path: "/tmp/decisions.jsonl",
      sample_coverage: { sample_ids: 10, covered_ids: 8, coverage_rate: 0.8 },
      issue_counts: { below_min_submitted_ids: 2, primary_label_missing: 1 },
    },
    {
      audit_id: "unrelated",
      passed: true,
      decisions_path: "/tmp/other.jsonl",
    },
  ];

  assert.deepEqual(agreementAuditsForDecision(decision, audits).map((item) => item.audit_id), ["round_1"]);
  assert.equal(agreementAuditStatusLabel(audits[0]), "未通过");
  assert.equal(agreementAuditCoverageLabel(audits[0]), "8/10（80%）");
  assert.equal(agreementAuditCoverageLabel({ sample_coverage: { sample_ids: ["a", "b"], covered_ids: ["a"] } }), "1/2");
  assert.equal(agreementAuditIssueSummary(audits[0]), "缺主标签 1；提交数不足 2");
  assert.equal(agreementAuditIssueSummary({ passed: true }), "无阻塞问题");
});
