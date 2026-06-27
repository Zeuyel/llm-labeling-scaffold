import test from "node:test";
import assert from "node:assert/strict";
import {
  createImportActions,
  filterImportAuditEvents,
  hasEffectiveDataLakeConfig,
  importActionState,
  summarizeImportAsset,
} from "./importsPageState.js";

test("summarizes import assets for list-first rows", () => {
  const summary = summarizeImportAsset({
    import_id: "raw_2026",
    rows: 120,
    unique_ids: 118,
    duplicate_ids: 2,
    missing_ids: 0,
    source: "data_lake",
    source_object_path: "datasets/raw.jsonl",
    content_sha256: "abcdef1234567890",
    path: "runs/task/imports/raw_2026/raw.jsonl",
  });

  assert.equal(summary.importId, "raw_2026");
  assert.equal(summary.rows, "120");
  assert.equal(summary.uniqueIds, "118");
  assert.equal(summary.idQuality, "0 缺失 / 2 重复");
  assert.equal(summary.source, "数据湖");
  assert.equal(summary.contentHash, "abcdef123456...");
});

test("manual upload action is hidden when production R2 gating does not allow it", () => {
  assert.deepEqual(
    createImportActions({ hasDataLakeConfig: true, showManualImports: false }).map((action) => action.key),
    ["data_lake"],
  );
  assert.deepEqual(
    createImportActions({ hasDataLakeConfig: false, showManualImports: false }).map((action) => action.key),
    [],
  );
});

test("archive action is blocked when an import has linked samples", () => {
  const actionState = importActionState({
    import_id: "raw_2026",
    linked_samples: [{ sample_id: "sample_a" }],
  });

  assert.equal(actionState.canArchive, false);
  assert.match(actionState.archiveDisabledReason, /sample_a/);
});

test("data lake config detection requires an effective source field", () => {
  assert.equal(hasEffectiveDataLakeConfig({ source_dataset_id: "dataset_a" }), true);
  assert.equal(hasEffectiveDataLakeConfig({ output_base_uri: "r2:bucket/out" }), false);
  assert.equal(hasEffectiveDataLakeConfig(null), false);
});

test("import detail audit log filters by import asset", () => {
  const events = filterImportAuditEvents([
    { asset_type: "import", asset_id: "raw_2026", event: "import.create" },
    { asset_type: "sample", asset_id: "raw_2026", event: "sample.create" },
    { asset_type: "import", asset_id: "other", event: "import.archive" },
  ], "raw_2026");

  assert.deepEqual(events.map((event) => event.event), ["import.create"]);
});
