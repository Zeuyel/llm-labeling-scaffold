import test from "node:test";
import assert from "node:assert/strict";
import {
  goldSummary,
  goldStatusLabel,
  goldTrainAction,
  labelCountsText,
  modelInferAction,
  modelMetricSummary,
  modelSummary,
  modelStatusLabel,
} from "./trainingResourceDisplay.js";

test("gold summary prefers business fields and derives legacy paths", () => {
  const summary = goldSummary({
    version: "v001",
    rows: 12,
    unique_ids: 12,
    primary_label: "class_label",
    source: "decision_artifact",
    label_counts: { non_target: 5, service_upgrade: 7 },
    created_at: "2026-06-17T06:21:55.037431+00:00",
  }, "toy_multiclass_v1");

  assert.equal(summary.version, "v001");
  assert.equal(summary.status, "可用");
  assert.equal(summary.rows, 12);
  assert.equal(summary.source, "标注结果产物");
  assert.equal(summary.labelDistribution, "service_upgrade: 7, non_target: 5");
  assert.equal(summary.path, "runs/toy_multiclass_v1/gold/gold_v001.jsonl");
  assert.equal(summary.manifestPath, "runs/toy_multiclass_v1/gold/gold_v001.manifest.json");
  assert.equal(summary.createdAt, "2026-06-17T06:21:55");
});

test("gold status identifies empty or incomplete versions", () => {
  assert.equal(goldStatusLabel({ version: "v001", rows: 1 }, "task_a"), "可用");
  assert.equal(goldStatusLabel({ version: "v002", rows: 0 }, "task_a"), "空版本");
  assert.equal(goldStatusLabel({ rows: 10 }, ""), "记录不完整");
});

test("label count formatting handles missing and stable sorted counts", () => {
  assert.equal(labelCountsText(null), "-");
  assert.equal(labelCountsText({ beta: 2, alpha: 2, gamma: 1 }), "alpha: 2, beta: 2, gamma: 1");
});

test("gold train action is disabled only when detail data cannot identify a usable dataset", () => {
  assert.deepEqual(goldTrainAction({ version: "v002", rows: 5 }, "task_a"), {
    enabled: true,
    reason: "",
    gold: "runs/task_a/gold/gold_v002.jsonl",
  });
  assert.equal(goldTrainAction({ rows: 5 }, "task_a").enabled, false);
  assert.equal(goldTrainAction({ version: "v003", rows: 0 }, "task_a").reason, "训练集没有可训练行");
});

test("model summary combines manifest and metrics without requiring external records", () => {
  const summary = modelSummary({
    model_id: "baseline_v001",
    manifest: {
      trainer: "tfidf_sgd",
      model_path: "runs/task/models/baseline_v001/model.joblib",
      gold_path: "runs/task/gold/gold_v001.jsonl",
    },
    metrics: {
      train_rows: 9,
      test_rows: 3,
      labels: ["a", "b"],
      classification_report: { "macro avg": { "f1-score": 0.625 } },
    },
  });

  assert.equal(summary.modelId, "baseline_v001");
  assert.equal(summary.status, "可用");
  assert.equal(summary.trainer, "tfidf_sgd");
  assert.equal(summary.metricSummary, "macro F1 0.625 · 测试 3 行 · 训练 9 行");
  assert.equal(summary.externalRecord, "仅本地");
  assert.equal(summary.labels, "a, b");
  assert.equal(summary.goldPath, "runs/task/gold/gold_v001.jsonl");
  assert.equal(summary.path, "runs/task/models/baseline_v001/model.joblib");
});

test("model status identifies incomplete registry entries", () => {
  assert.equal(modelStatusLabel({ manifest: { model_path: "model.joblib" } }), "可用");
  assert.equal(modelStatusLabel({ model_id: "legacy", path: "model.joblib" }), "记录不完整");
  assert.equal(modelStatusLabel({ manifest: {} }), "记录不完整");
});

test("model inference action requires a model artifact path", () => {
  assert.deepEqual(modelInferAction({ manifest: { model_path: "model.joblib" } }), {
    enabled: true,
    reason: "",
    model: "model.joblib",
  });
  assert.equal(modelInferAction({ model_id: "missing_path" }).enabled, false);
});

test("model metric summary degrades cleanly when metrics are unavailable", () => {
  assert.equal(modelMetricSummary({}), "-");
});
