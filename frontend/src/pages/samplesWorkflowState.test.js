import test from "node:test";
import assert from "node:assert/strict";
import {
  computeSampleWorkflow,
  hasBatchPlan,
  newestSample,
  sampleCompletionNotice,
  STAGE_STATUS,
} from "./samplesWorkflowState.js";

test("loading state does not report empty-ready workflow", () => {
  const workflow = computeSampleWorkflow({ assetsLoading: true });

  assert.equal(workflow.imports.status, STAGE_STATUS.LOADING);
  assert.equal(workflow.sample.status, STAGE_STATUS.LOADING);
  assert.equal(workflow.batch.status, STAGE_STATUS.LOADING);
  assert.equal(workflow.argilla.status, STAGE_STATUS.LOADING);
});

test("sample creation is blocked until imports exist", () => {
  const workflow = computeSampleWorkflow({ assetsLoading: false, imports: [], samples: [] });

  assert.equal(workflow.imports.status, STAGE_STATUS.BLOCKED);
  assert.equal(workflow.sample.status, STAGE_STATUS.BLOCKED);
  assert.equal(workflow.argilla.disabledReason, "需要先创建样本集。");
});

test("existing sample completes step two and prepares batch configuration", () => {
  const samples = [{ sample_id: "sample_a", path: "/tmp/sample_a.jsonl", manifest: { created_at: "2026-01-01T00:00:00Z" } }];
  const workflow = computeSampleWorkflow({ imports: [{ import_id: "raw" }], samples, selectedSample: samples[0] });

  assert.equal(workflow.sample.status, STAGE_STATUS.COMPLETED);
  assert.equal(workflow.sample.badgeTone, "green");
  assert.equal(workflow.batchConfig.status, STAGE_STATUS.READY);
});

test("argilla step requires a generated batch plan", () => {
  const sampleWithoutPlan = { sample_id: "sample_a", path: "/tmp/sample_a.jsonl", manifest: {} };
  const sampleWithPlan = {
    sample_id: "sample_b",
    path: "/tmp/sample_b.jsonl",
    latest_batch_manifest: { plan_id: "qc_round_1", batch_count: 2 },
  };

  const blocked = computeSampleWorkflow({
    imports: [{ import_id: "raw" }],
    samples: [sampleWithoutPlan],
    selectedSample: sampleWithoutPlan,
  });
  const ready = computeSampleWorkflow({
    imports: [{ import_id: "raw" }],
    samples: [sampleWithPlan],
    selectedSample: sampleWithPlan,
  });

  assert.equal(hasBatchPlan(sampleWithoutPlan), false);
  assert.equal(blocked.argilla.status, STAGE_STATUS.BLOCKED);
  assert.equal(blocked.argilla.disabledReason, "请先生成批次计划。");
  assert.equal(hasBatchPlan(sampleWithPlan), true);
  assert.equal(ready.batch.status, STAGE_STATUS.COMPLETED);
  assert.equal(ready.argilla.status, STAGE_STATUS.READY);
});

test("running and failed states are explicit", () => {
  const running = computeSampleWorkflow({ imports: [{ import_id: "raw" }], action: "sample" });
  const failed = computeSampleWorkflow({ imports: [{ import_id: "raw" }], action: "sample", actionError: "boom" });

  assert.equal(running.sample.status, STAGE_STATUS.RUNNING);
  assert.equal(failed.sample.status, STAGE_STATUS.FAILED);
});

test("newest sample prefers latest created_at", () => {
  const latest = newestSample([
    { sample_id: "old", path: "/tmp/old", manifest: { created_at: "2026-01-01T00:00:00Z" } },
    { sample_id: "new", path: "/tmp/new", manifest: { created_at: "2026-02-01T00:00:00Z" } },
  ]);

  assert.equal(latest.sample_id, "new");
});

test("sample completion notice is clear for idempotent reuse", () => {
  assert.match(
    sampleCompletionNotice({ existedBefore: true, result: { artifact: "/tmp/sample.jsonl" } }),
    /样本已存在且内容一致，已复用/,
  );
});
