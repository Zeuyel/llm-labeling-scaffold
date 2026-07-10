import test from "node:test";
import assert from "node:assert/strict";
import {
  computeSampleDetailActions,
  computeSampleWorkflow,
  computeSamplesListView,
  filterSampleAuditEvents,
  findSampleById,
  hasBatchPlan,
  newestSample,
  sampleCreatedAt,
  sampleCompletionNotice,
  sampleDetailPath,
  sampleStateLabel,
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

test("samples page defaults to list-first state", () => {
  const samples = [{ sample_id: "sample_a", path: "/tmp/sample_a.jsonl", manifest: { rows: 10 } }];
  const ready = computeSamplesListView({ imports: [{ import_id: "raw" }], samples });
  const empty = computeSamplesListView({ imports: [{ import_id: "raw" }], samples: [] });
  const loading = computeSamplesListView({ assetsLoading: true, imports: [{ import_id: "raw" }], samples: [] });

  assert.equal(ready.defaultSurface, "list");
  assert.equal(ready.showList, true);
  assert.equal(ready.showEmpty, false);
  assert.equal(empty.defaultSurface, "list");
  assert.equal(empty.showList, false);
  assert.equal(empty.showEmpty, true);
  assert.equal(empty.canCreate, true);
  assert.equal(loading.defaultSurface, "list");
  assert.equal(loading.status, STAGE_STATUS.LOADING);
  assert.equal(loading.showEmpty, false);
});

test("sample detail route helpers encode task and sample ids", () => {
  assert.equal(
    sampleDetailPath("task a", "sample/one"),
    "/task/task%20a/samples/sample%2Fone",
  );
  assert.deepEqual(
    findSampleById([{ sample_id: "sample_a" }, { sample_id: "sample_b" }], "sample_b"),
    { sample_id: "sample_b" },
  );
  assert.equal(findSampleById([{ sample_id: "sample_a" }], ""), null);
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

test("sample detail actions enable argilla only after batch plan exists", () => {
  const sampleWithoutPlan = { sample_id: "sample_a", path: "/tmp/sample_a.jsonl", manifest: {}, dependencies: [] };
  const sampleWithPlan = {
    sample_id: "sample_b",
    path: "/tmp/sample_b.jsonl",
    latest_batch_manifest: { plan_id: "qc_round_1", batch_count: 2 },
    dependencies: [],
  };
  const blocked = computeSampleDetailActions({ sample: sampleWithoutPlan });
  const ready = computeSampleDetailActions({ sample: sampleWithPlan });
  const busy = computeSampleDetailActions({ sample: sampleWithPlan, busy: true });
  const archived = computeSampleDetailActions({ sample: { ...sampleWithPlan, manifest: { state: "archived" } } });

  assert.equal(blocked.generateBatch.enabled, true);
  assert.equal(blocked.pushArgilla.enabled, false);
  assert.equal(blocked.pushArgilla.disabledReason, "请先生成批次计划。");
  assert.equal(ready.generateBatch.enabled, true);
  assert.equal(ready.pushArgilla.enabled, true);
  assert.equal(busy.generateBatch.enabled, false);
  assert.equal(busy.pushArgilla.enabled, false);
  assert.equal(archived.archive.enabled, false);
  assert.equal(archived.archive.disabledReason, "样本集已归档。");
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

test("sample detail display exposes state, creation time, and filtered audit events", () => {
  const sample = {
    sample_id: "sample_a",
    manifest: { state: "active", created_at: "2026-06-17T06:21:55.037431+00:00" },
  };
  const events = filterSampleAuditEvents([
    { asset_type: "sample", asset_id: "sample_a", event: "sample.create" },
    { asset_type: "sample", asset_id: "sample_b", event: "sample.create" },
    { asset_type: "import", asset_id: "sample_a", event: "import.create" },
  ], "sample_a");

  assert.equal(sampleStateLabel(sample), "可用");
  assert.equal(sampleStateLabel({ manifest: { state: "archived" } }), "已归档");
  assert.equal(sampleCreatedAt(sample), "2026-06-17T06:21:55");
  assert.deepEqual(events.map((event) => event.event), ["sample.create"]);
});
