export function firstDefined(...values) {
  return values.find((value) => value !== undefined && value !== null && value !== "");
}

export function displayResourceValue(value) {
  if (value === undefined || value === null || value === "") return "-";
  if (Array.isArray(value)) return value.length ? value.join(", ") : "-";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

export function formatDateTime(value) {
  return value ? String(value).slice(0, 19) : "-";
}

export function labelCountsText(counts) {
  if (!counts || typeof counts !== "object" || Array.isArray(counts)) return "-";
  const entries = Object.entries(counts);
  if (!entries.length) return "-";
  return entries
    .sort((a, b) => Number(b[1]) - Number(a[1]) || String(a[0]).localeCompare(String(b[0])))
    .map(([label, count]) => `${label}: ${count}`)
    .join(", ");
}

export function sourceLabel(source) {
  if (source === "decision_artifact") return "标注结果产物";
  if (source === "run") return "运行输出";
  return source || "-";
}

function normalizedState(...values) {
  const value = firstDefined(...values);
  return value === undefined || value === null ? "" : String(value).trim().toLowerCase();
}

export function goldStatusLabel(gold, taskId = "") {
  const state = normalizedState(gold?.status, gold?.state);
  if (["archived", "已归档"].includes(state)) return "已归档";
  if (["failed", "error"].includes(state)) return "失败";
  if (["incomplete", "partial"].includes(state)) return "记录不完整";
  if (!goldPathForTask(gold, taskId)) return "记录不完整";
  const rows = Number(gold?.rows);
  if (Number.isFinite(rows) && rows <= 0) return "空版本";
  return "可用";
}

export function goldPathForTask(gold, taskId = "") {
  return firstDefined(
    gold?.path,
    gold?.gold_path,
    gold?.output_path,
    gold?.artifact,
    gold?.version && taskId ? `runs/${taskId}/gold/gold_${gold.version}.jsonl` : "",
  );
}

export function goldManifestPathForTask(gold, taskId = "") {
  return firstDefined(
    gold?.manifest_path,
    gold?.manifest_uri,
    gold?.version && taskId ? `runs/${taskId}/gold/gold_${gold.version}.manifest.json` : "",
  );
}

export function goldResourceKey(gold, taskId = "") {
  return String(firstDefined(gold?.version, goldPathForTask(gold, taskId), gold?.created_at, ""));
}

export function goldSummary(gold, taskId = "") {
  return {
    key: goldResourceKey(gold, taskId),
    version: firstDefined(gold?.version, "-"),
    status: goldStatusLabel(gold, taskId),
    rows: firstDefined(gold?.rows, "-"),
    uniqueIds: firstDefined(gold?.unique_ids, "-"),
    primaryLabel: firstDefined(gold?.primary_label, "-"),
    labelDistribution: labelCountsText(gold?.label_counts),
    source: sourceLabel(gold?.source),
    createdAt: formatDateTime(gold?.created_at),
    path: goldPathForTask(gold, taskId),
    manifestPath: goldManifestPathForTask(gold, taskId),
  };
}

export function goldTrainAction(gold, taskId = "") {
  const path = goldPathForTask(gold, taskId);
  const rows = Number(gold?.rows);
  if (!path) return { enabled: false, reason: "缺少训练集路径", gold: "" };
  if (Number.isFinite(rows) && rows <= 0) return { enabled: false, reason: "训练集没有可训练行", gold: path };
  return { enabled: true, reason: "", gold: path };
}

export function modelPath(model) {
  return firstDefined(model?.path, model?.manifest?.model_path, model?.model_path);
}

export function modelStatusLabel(model) {
  const state = normalizedState(model?.status, model?.state, model?.manifest?.status, model?.manifest?.state);
  if (["archived", "已归档"].includes(state)) return "已归档";
  if (["failed", "error"].includes(state)) return "失败";
  if (["incomplete", "partial"].includes(state)) return "记录不完整";
  if (!model?.manifest) return "记录不完整";
  if (!modelPath(model)) return "记录不完整";
  return "可用";
}

export function modelResourceKey(model) {
  return String(firstDefined(model?.model_id, modelPath(model), model?.manifest?.metrics_path, ""));
}

export function modelTrainer(model) {
  return firstDefined(model?.metrics?.trainer, model?.manifest?.trainer, model?.trainer, "-");
}

export function modelLabelsText(model) {
  return displayResourceValue(firstDefined(model?.metrics?.labels, model?.manifest?.labels));
}

export function modelMetricSummary(model) {
  const report = model?.metrics?.classification_report || {};
  const macroF1 = report["macro avg"]?.["f1-score"];
  const accuracy = model?.metrics?.accuracy;
  const parts = [];
  if (macroF1 !== undefined) parts.push(`macro F1 ${Number(macroF1).toFixed(3)}`);
  if (accuracy !== undefined) parts.push(`accuracy ${Number(accuracy).toFixed(3)}`);
  if (model?.metrics?.test_rows !== undefined) parts.push(`测试 ${model.metrics.test_rows} 行`);
  if (model?.metrics?.train_rows !== undefined) parts.push(`训练 ${model.metrics.train_rows} 行`);
  return parts.length ? parts.join(" · ") : "-";
}

export function modelSummary(model) {
  return {
    key: modelResourceKey(model),
    modelId: firstDefined(model?.model_id, model?.manifest?.model_id, "-"),
    status: modelStatusLabel(model),
    trainer: modelTrainer(model),
    trainRows: firstDefined(model?.metrics?.train_rows, "-"),
    testRows: firstDefined(model?.metrics?.test_rows, "-"),
    metricSummary: modelMetricSummary(model),
    externalRecord: firstDefined(model?.manifest?.mlflow?.run_id, model?.mlflow?.run_id, "仅本地"),
    labels: modelLabelsText(model),
    goldPath: firstDefined(model?.metrics?.gold_path, model?.manifest?.gold_path, "-"),
    path: modelPath(model),
    metricsPath: firstDefined(model?.manifest?.metrics_path, model?.metrics_path),
    createdAt: formatDateTime(firstDefined(model?.created_at, model?.manifest?.created_at, model?.metrics?.created_at)),
  };
}

export function modelInferAction(model) {
  const path = modelPath(model);
  if (!path) return { enabled: false, reason: "缺少模型路径", model: "" };
  return { enabled: true, reason: "", model: path };
}
