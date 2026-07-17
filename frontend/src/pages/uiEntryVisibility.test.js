import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const pageSource = (name) => readFileSync(new URL(`./${name}`, import.meta.url), "utf8");

test("任务页不暴露 R2 任务登记或同步入口", () => {
  const source = pageSource("TasksPage.jsx");

  assert.match(source, /Scaffold 控制面/);
  assert.doesNotMatch(source, /taskRegistryUri|task_registry_uri|同步任务配置|同步任务/);
});

test("设置页不暴露旧任务登记和手动导入配置", () => {
  const source = pageSource("SettingsPage.jsx");

  assert.doesNotMatch(source, /task_registry_uri|任务登记表|allow_manual_imports|允许手动导入/);
  assert.match(source, /data_lake_r2_prefix/);
});

test("任务输入页只保留数据湖生成入口", () => {
  const source = pageSource("ImportsPage.jsx");

  assert.match(source, /生成任务输入/);
  assert.doesNotMatch(source, /手动上传|手动导入|保存手动导入|allowManualImports/);
});
