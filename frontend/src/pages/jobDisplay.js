export const JOB_BADGE = {
  succeeded: "badge-green",
  success: "badge-green",
  completed: "badge-green",
  failed: "badge-red",
  error: "badge-red",
  running: "badge-blue",
  pending: "badge-gray",
  queued: "badge-gray",
};

export const JOB_STATUS_LABEL = {
  succeeded: "成功",
  success: "成功",
  completed: "成功",
  failed: "失败",
  error: "失败",
  running: "运行中",
  pending: "等待中",
  queued: "排队中",
};

export const JOB_KIND_LABEL = {
  sample: "创建样本",
  batch: "切分批次",
  annotate: "本地调试标注",
  argilla_push: "推送 Argilla",
  argilla_pull: "拉回标注结果",
  agreement_audit: "一致性检查",
  audit: "审核摘要",
  merge: "合并输出",
  gold: "构建训练集",
  train: "训练模型",
  infer: "模型推理",
};

export function normalizeJobStatus(value) {
  return String(value || "").trim().toLowerCase();
}

export function jobStatusLabel(value) {
  const status = normalizeJobStatus(value);
  return JOB_STATUS_LABEL[status] || status || "-";
}

export function jobBadgeClass(value) {
  return JOB_BADGE[normalizeJobStatus(value)] || "badge-gray";
}

export function jobKindLabel(value) {
  return JOB_KIND_LABEL[value] || value || "-";
}

export function shortJobResult(job) {
  if (!job) return "-";
  if (job.error) return String(job.error).slice(0, 120);
  const result = job.result ?? {};
  const text = Object.keys(result).length ? JSON.stringify(result) : "";
  return text ? text.slice(0, 120) : "-";
}

export function jobDebugFields(job) {
  if (!job) return [];
  return [
    ["id", job.id],
    ["kind", job.kind],
    ["status", job.status],
    ["created_at", job.created_at],
    ["updated_at", job.updated_at],
    ["params", job.params],
    ["result", job.result],
    ["error", job.error],
  ];
}
