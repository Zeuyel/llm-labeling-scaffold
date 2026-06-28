import test from "node:test";
import assert from "node:assert/strict";
import { matchAppRoute } from "./appRoutes.js";

test("matches sample detail routes with sample navigation active", () => {
  const matched = matchAppRoute("/task/patent_boundary_v0_1/samples/patent_seed_500");

  assert.equal(matched.page, "sampleDetail");
  assert.equal(matched.activePage, "samples");
  assert.deepEqual(matched.params, {
    id: "patent_boundary_v0_1",
    sampleId: "patent_seed_500",
  });
});

test("decodes sample detail route params", () => {
  const matched = matchAppRoute("/task/task%20a/samples/sample%2Fone");

  assert.equal(matched.params.id, "task a");
  assert.equal(matched.params.sampleId, "sample/one");
});
