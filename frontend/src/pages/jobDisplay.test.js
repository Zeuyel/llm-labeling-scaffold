import test from "node:test";
import assert from "node:assert/strict";
import {
  jobBadgeClass,
  jobKindLabel,
  jobStatusLabel,
  shortJobResult,
} from "./jobDisplay.js";

test("job display helpers normalize status and action labels", () => {
  assert.equal(jobStatusLabel("SUCCEEDED"), "成功");
  assert.equal(jobStatusLabel("queued"), "排队中");
  assert.equal(jobStatusLabel("failed"), "失败");
  assert.equal(jobStatusLabel(""), "-");
  assert.equal(jobBadgeClass("completed"), "badge-green");
  assert.equal(jobBadgeClass("running"), "badge-blue");
  assert.equal(jobBadgeClass("unknown"), "badge-gray");
  assert.equal(jobKindLabel("argilla_pull"), "拉回标注结果");
  assert.equal(jobKindLabel("prelabel_export"), "导出预标注模板");
  assert.equal(jobKindLabel("prelabel_publish"), "写入机器建议");
  assert.equal(jobKindLabel("prelabel_suggest"), "生成预标注建议");
});

test("job result summary prefers errors and hides empty objects", () => {
  assert.equal(shortJobResult({ error: "backend failed because dataset is missing" }), "backend failed because dataset is missing");
  assert.equal(shortJobResult({ result: {} }), "-");
  assert.equal(shortJobResult({ result: { records: 12, dataset: "round_1" } }), "{\"records\":12,\"dataset\":\"round_1\"}");
});
