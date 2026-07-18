import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const source = readFileSync(new URL("./AllocationPlansPage.jsx", import.meta.url), "utf8");
const styles = readFileSync(new URL("../styles.css", import.meta.url), "utf8");

test("allocation page calls dedicated helpers and never routes through the generic action endpoint", () => {
  assert.match(source, /getAllocationPlans/);
  assert.match(source, /getAllocationPlanProgress/);
  assert.match(source, /getAllocationPlanAssignments/);
  assert.match(source, /previewAllocation/);
  assert.match(source, /confirmAllocationPlan/);
  assert.doesNotMatch(source, /startAction|\/api\/action/);
  assert.doesNotMatch(source, /setPlans\(\s*\[/);
});

test("allocation page exposes loading, success, error, retry, and duplicate-submit protection", () => {
  assert.match(styles, /allocation-operation-loading/);
  assert.match(styles, /allocation-operation-success/);
  assert.match(styles, /allocation-operation-error/);
  assert.match(source, /上一操作正在执行，请勿重复提交/);
  assert.match(source, /重试/);
  assert.match(source, /正在读取分配计划/);
});
