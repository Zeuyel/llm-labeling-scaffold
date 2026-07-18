import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const source = (path) => readFileSync(new URL(`../${path}`, import.meta.url), "utf8");

test("人员管理入口和详情路由独立存在", () => {
  const app = source("App.jsx");
  const sidebar = source("components/Sidebar.jsx");
  assert.match(app, /pattern: "\/annotators"/);
  assert.match(app, /pattern: "\/cohorts"/);
  assert.match(sidebar, />标注人员</);
  assert.match(sidebar, />人员组</);
});

test("人员管理页面不调用 Argilla 或通用 action", () => {
  for (const path of ["pages/AnnotatorsPage.jsx", "pages/CohortsPage.jsx"]) {
    const page = source(path);
    assert.doesNotMatch(page, /\/api\/argilla/);
    assert.doesNotMatch(page, /\/api\/action/);
  }
});

test("provision 密码只提交当前请求并在完成后清空", () => {
  const page = source("pages/AnnotatorsPage.jsx");
  assert.match(page, /name="?initial_password|initial_password/);
  assert.match(page, /autoComplete="off"/);
  assert.match(page, /payload\.initial_password = ""/);
  assert.doesNotMatch(page, /localStorage\.(setItem|getItem)/);
  assert.doesNotMatch(page, /sessionStorage\.(setItem|getItem)/);
});

test("人员组编辑携带 expected_revision 且删除能力明确禁用", () => {
  const page = source("pages/CohortsPage.jsx");
  assert.match(page, /expected_revision/);
  assert.match(page, /删除人员组（未接入）/);
  assert.match(page, /disabled title="后端尚未接入人员组删除接口"/);
});
