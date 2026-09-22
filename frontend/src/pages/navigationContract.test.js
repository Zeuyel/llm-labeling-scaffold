import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const readSource = (path) => readFileSync(new URL(path, import.meta.url), "utf8");

const app = readSource("../App.jsx");
const sidebar = readSource("../components/Sidebar.jsx");
const taskLayout = readSource("../components/TaskLayout.jsx");
const taskNavigation = readSource("../components/TaskNavigation.jsx");
const router = readSource("../router.jsx");

test("全局侧栏只暴露既有入口且支持当前页语义", () => {
  for (const label of ["全部任务", "数据资产", "成员管理", "标注人员", "人员组", "系统设置"]) {
    assert.match(sidebar, new RegExp(`label: "${label}"`));
  }
  assert.equal((sidebar.match(/label: "/g) || []).length, 6);
  assert.doesNotMatch(sidebar, /task_id|activeTaskId|任务列表/);
  assert.match(sidebar, /aria-current=\{activePage === key \? "page" : undefined\}/);
  assert.match(sidebar, /aria-expanded=\{!collapsed\}/);
});

test("任务导航保留既有页签，归档只通过配置入口兼容", () => {
  for (const label of ["概览", "流程画布", "任务输入", "样本", "分配", "分发", "Gold 版本", "执行记录", "训练与推理"]) {
    assert.match(taskNavigation, new RegExp(`label: "${label}"`));
  }
  assert.match(taskNavigation, /任务配置/);
  assert.doesNotMatch(taskNavigation, /归档入口|\/archive/);
  assert.match(taskNavigation, /aria-current=\{active \? "page" : undefined\}/);
});

test("任务路由契约包含新建、配置和旧归档重定向", () => {
  assert.ok(app.includes('pattern: "/tasks/new", page: "task-create"'));
  assert.ok(app.includes('pattern: "/task/:id/configuration", page: "configuration"'));
  assert.ok(app.includes('pattern: "/task/:id/archive", page: "archive-redirect"'));
  assert.match(app, /createMode=\{createMode\}/);
  assert.match(app, /detailTaskId=\{detailTaskId\}/);
  assert.match(app, /<LegacyTaskArchiveRedirect taskId=\{activeTaskId\} \/>/);
  assert.doesNotMatch(app, /TaskArchivePage/);
  assert.match(taskLayout, /navigate\(destination, \{ replace: true \}\)/);
});

test("任务列表请求显式绑定 workspace，并区分正常与归档恢复加载", () => {
  assert.match(app, /api\.getTasks\(workspace, requestOptions\)/);
  assert.match(app, /workspace=\{managementWorkspace\}/);
  assert.match(app, /workspaces=\{managementWorkspaces\}/);
  assert.match(app, /onWorkspaceChange=\{setManagementWorkspace\}/);
  assert.match(app, /include_archived: normalizedOptions\.include_archived \?\?/);
  assert.match(app, /includeArchived \?\? false/);
  assert.match(app, /limit: normalizedOptions\.limit \?\? 100/);
  assert.match(app, /tasksNextCursor=\{tasksNextCursor\}/);
  assert.match(app, /tasksRequestSeq/);
  assert.match(app, /requestSeq !== tasksRequestSeq\.current/);
});

test("任务详情使用只读 active summary，不由列表存在性或 draft envelope 门控", () => {
  assert.match(app, /api\.getTaskSummary\(taskId, workspace\)/);
  assert.match(app, /data\?\.task \|\| typeof data\.task !== "object" \|\| Array\.isArray\(data\.task\)/);
  assert.match(app, /task_summary_invalid_response/);
  assert.match(app, /需要 api\.getTaskSummary\(taskId, workspace\)/);
  assert.doesNotMatch(app, /api\.getTask\(taskId, workspace\)/);
  assert.doesNotMatch(app, /api\.getTaskControl\(taskId, workspace\)/);
  assert.match(app, /taskSummaryRequired/);
  assert.match(app, /taskSummaryRequestSeq/);
  assert.match(app, /requestSeq !== taskSummaryRequestSeq\.current/);
  assert.match(app, /const activeTask = resolvedTaskSummary/);
  assert.doesNotMatch(taskLayout, /taskContext|\.task \|\| taskSummary|\.record/);
  assert.match(taskLayout, /taskSummaryRequired/);
  assert.match(taskLayout, /全部任务/);
  assert.match(taskLayout, /当前任务/);
  assert.doesNotMatch(taskLayout, /task-breadcrumb-detail/);
});

test("配置页独立于 active summary，旧归档地址保留 task id 后替换跳转", () => {
  assert.match(app, /const TASK_LAYOUT_PAGES = new Set\(\[\.\.\.TASK_SUMMARY_PAGES, "configuration"\]\)/);
  assert.match(app, /const TASK_ROUTE_PAGES = new Set\(\[\.\.\.TASK_LAYOUT_PAGES, "archive-redirect"\]\)/);
  assert.match(app, /const activeTaskId = TASK_ROUTE_PAGES\.has\(matched\.page\)/);
  assert.match(app, /if \(!taskId \|\| !requiresTaskSummary\)/);
  assert.match(taskLayout, /!taskSummaryRequired/);
  assert.match(taskLayout, /navigate\(destination, \{ replace: true \}\)/);
  assert.match(app, /pattern: "\/task\/:id\/archive", page: "archive-redirect"/);
  assert.doesNotMatch(app, /TaskArchivePage/);
});

test("任务数据和设置状态由 TasksPage 接收，避免默认 R2 闪现", () => {
  assert.match(app, /tasksLoading=\{tasksLoading\}/);
  assert.match(app, /tasksError=\{tasksError\}/);
  assert.match(app, /settingsReady=\{settingsReady\}/);
  assert.match(app, /settingsError=\{settingsError\}/);
  assert.match(app, /taskSource=\{settingsAvailable \? \(settings\.task_source \|\| ""\) : ""\}/);
  assert.doesNotMatch(app, /TaskDataStatus/);
  assert.match(app, /onRetryTaskSummary=\{\(\) => loadTaskSummary\(\)\.catch\(\(\) => \{\}\)\}/);
});

test("布局样式不通过裁剪 content 修复横向布局，也不保留未使用维护入口", () => {
  const styles = readSource("../styles.css");
  const navigationCss = readSource("../components/navigation.css");
  assert.doesNotMatch(styles, /\.content[^}]*overflow-x\s*:/);
  assert.doesNotMatch(navigationCss, /task-maintenance/);
});

test("router 支持替换式兼容跳转和 aria-current 透传", () => {
  assert.match(router, /replace = false/);
  assert.match(router, /window\.history\.replaceState/);
  assert.match(router, /\.\.\.props/);
});
