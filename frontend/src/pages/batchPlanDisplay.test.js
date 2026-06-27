import test from "node:test";
import assert from "node:assert/strict";
import {
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
  assert.equal(annotationJobStatusLabel({ state: "running" }), "执行中");
  assert.equal(annotationJobStatusLabel({ status: "failed" }), "失败");
});
