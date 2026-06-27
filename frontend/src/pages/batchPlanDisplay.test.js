import test from "node:test";
import assert from "node:assert/strict";
import {
  agreementAuditsForAnnotationJob,
  annotationJobActionAvailability,
  annotationJobBatchSummary,
  annotationJobLabel,
  annotationJobStatusLabel,
  batchPlanDebugFields,
  batchPlanFromManifest,
  batchPlanOptionLabel,
  formatBatchPlanSummary,
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
