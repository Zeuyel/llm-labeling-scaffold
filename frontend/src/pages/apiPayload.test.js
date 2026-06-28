import test from "node:test";
import assert from "node:assert/strict";
import {
  dataLakeImportPayload,
  importFromDataLake,
} from "../api.js";

test("data lake import payload adds explicit submit gates", () => {
  const payload = dataLakeImportPayload("task-a", { import_id: "lake-import" });

  assert.equal(payload.task_id, "task-a");
  assert.equal(payload.import_id, "lake-import");
  assert.equal(payload.confirm, true);
  assert.match(payload.idempotency_key, /^data-lake-import:task-a:lake-import:/);
  assert.equal(payload.idempotency_key.includes("secret"), false);
});

test("data lake dry-run payload does not add mutating submit fields", () => {
  const payload = dataLakeImportPayload("task-a", { import_id: "lake-import", dry_run: true });

  assert.deepEqual(payload, {
    task_id: "task-a",
    import_id: "lake-import",
    dry_run: true,
  });
});

test("importFromDataLake sends gated submit payload", async () => {
  const previousFetch = globalThis.fetch;
  let captured;
  globalThis.fetch = async (path, opts) => {
    captured = { path, opts };
    return {
      ok: true,
      async json() {
        return { ok: true, job: { id: "job-1" } };
      },
    };
  };

  try {
    const result = await importFromDataLake("task-a", { import_id: "lake-import" });
    const body = JSON.parse(captured.opts.body);

    assert.equal(captured.path, "/api/import/data_lake");
    assert.equal(captured.opts.method, "POST");
    assert.deepEqual(result, { ok: true, job: { id: "job-1" } });
    assert.equal(body.task_id, "task-a");
    assert.equal(body.import_id, "lake-import");
    assert.equal(body.confirm, true);
    assert.match(body.idempotency_key, /^data-lake-import:task-a:lake-import:/);
  } finally {
    globalThis.fetch = previousFetch;
  }
});
