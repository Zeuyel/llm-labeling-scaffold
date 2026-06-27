export function slug(value) {
  return String(value || "item")
    .trim()
    .replace(/[^A-Za-z0-9_.-]+/g, "_")
    .replace(/_+/g, "_")
    .replace(/^[_.-]+|[_.-]+$/g, "") || "item";
}

export function defaultDatasetName(taskId, sampleId, planId) {
  return `${slug(taskId)}_${slug(sampleId || "sample")}_${slug(planId || "batch_plan")}_v001`;
}

export function firstDefined(...values) {
  return values.find((value) => value !== undefined && value !== null && value !== "");
}

function planIdFromPath(value) {
  const parts = String(value || "").split(/[\\/]+/).filter(Boolean);
  if (!parts.length) return "";
  const last = parts[parts.length - 1];
  if (last === "manifest.json" && parts.length > 1) return parts[parts.length - 2];
  return last;
}

function countLike(value) {
  return Array.isArray(value) ? value.length : value;
}

export function displayPlanValue(value) {
  if (value === undefined || value === null || value === "") return "-";
  if (Array.isArray(value)) return String(value.length);
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function integerish(value) {
  if (value === undefined || value === null || value === "") return null;
  const number = Number(value);
  if (!Number.isFinite(number)) return null;
  return Number.isInteger(number) ? String(number) : String(value);
}

function percentish(value) {
  if (value === undefined || value === null || value === "") return null;
  const number = Number(value);
  if (!Number.isFinite(number)) return String(value);
  if (number > 0 && number <= 1) return `${Math.round(number * 100)}%`;
  return `${number}%`;
}

export function batchPlanFromManifest(manifest, index = 0) {
  const consistency = manifest.consistency || manifest.quality_controls || manifest.policy || {};
  const manifestPath = firstDefined(manifest.manifest_path, manifest.batch_manifest_path, manifest.path);
  const planDir = firstDefined(manifest.plan_dir, manifest.batch_plan_dir);
  const planId = firstDefined(
    manifest.plan_id,
    manifest.batch_plan_id,
    manifest.id,
    manifest.name,
    planIdFromPath(planDir),
    planIdFromPath(manifestPath),
    `plan_${index + 1}`,
  );
  const batchCount = firstDefined(
    manifest.batch_count,
    manifest.batches_count,
    Array.isArray(manifest.batches) ? manifest.batches.length : undefined,
  );
  const overlapItemCount = firstDefined(
    manifest.overlap_item_count,
    countLike(manifest.overlap_item_ids),
    countLike(manifest.overlap_items),
    manifest.overlap_count,
    consistency.overlap_item_count,
    countLike(consistency.overlap_item_ids),
    countLike(consistency.overlap_items),
    consistency.overlap_count,
  );

  return {
    key: String(firstDefined(manifest.plan_id, manifest.batch_plan_id, manifestPath, planDir, planId)),
    manifest,
    manifest_path: manifestPath,
    plan_id: planId,
    strategy_id: firstDefined(manifest.strategy_id, consistency.strategy_id, manifest.strategy),
    batch_count: batchCount,
    batch_size: firstDefined(manifest.batch_size, manifest.rows_per_batch),
    overlap_rate: firstDefined(manifest.overlap_rate, consistency.overlap_rate),
    overlap_item_count: overlapItemCount,
    min_annotators_per_overlap_item: firstDefined(
      manifest.min_annotators_per_overlap_item,
      manifest.min_annotators,
      consistency.min_annotators_per_overlap_item,
      consistency.min_annotators,
    ),
  };
}

export function getBatchPlans(sample) {
  const plans = [];
  const seen = new Set();
  const pushManifest = (value) => {
    if (!value) return;
    if (Array.isArray(value)) {
      value.forEach(pushManifest);
      return;
    }
    if (typeof value !== "object") return;
    const plan = batchPlanFromManifest(value, plans.length);
    if (seen.has(plan.key)) return;
    seen.add(plan.key);
    plans.push(plan);
  };

  pushManifest(sample?.latest_batch_manifest);
  pushManifest(Array.isArray(sample?.batch_manifests) ? [...sample.batch_manifests].reverse() : sample?.batch_manifests);
  pushManifest(sample?.batch_manifest);
  pushManifest(sample?.batch);
  pushManifest(Array.isArray(sample?.batches) ? [...sample.batches].reverse() : sample?.batches);
  pushManifest(sample?.manifest?.batch_manifest);
  pushManifest(sample?.manifest?.batch);
  if (sample?.manifest?.batch_count || sample?.manifest?.batch_size || sample?.manifest?.batches) {
    pushManifest(sample.manifest);
  }
  return plans;
}

export function formatBatchPlanSummary(plan) {
  if (!plan) return "尚未选择批次方案。";

  const parts = [];
  const batchCount = integerish(plan.batch_count);
  const batchSize = integerish(plan.batch_size);
  const overlapItemCount = integerish(plan.overlap_item_count);
  const minAnnotators = integerish(plan.min_annotators_per_overlap_item);

  parts.push(batchCount ? `${batchCount} 个批次` : "批次数待确认");
  parts.push(batchSize ? `${batchSize} 条/批` : "每批条数待确认");

  if (overlapItemCount !== null) {
    parts.push(Number(overlapItemCount) > 0 ? `${overlapItemCount} 条一致性样本` : "无一致性样本");
  } else {
    const overlapRate = percentish(plan.overlap_rate);
    parts.push(overlapRate ? `${overlapRate} 一致性抽样` : "一致性样本数待确认");
  }

  if (minAnnotators) {
    parts.push(`每条一致性样本至少 ${minAnnotators} 人标注`);
  } else {
    parts.push("一致性样本标注人数待确认");
  }

  return `${parts.join("，")}。`;
}

export function batchPlanOptionLabel(plan, index = 0) {
  const prefix = index === 0 ? "最新 · " : "";
  return `${prefix}${formatBatchPlanSummary(plan)}`;
}

export function annotationJobBatchSummary(job) {
  if (!job) return "-";
  if ((job.dispatch_mode || "sample") === "sample" && !job.batch_plan_id) {
    return "整样本直接推送。";
  }
  const planLike = {
    batch_count: firstDefined(countLike(job.batch_ids), countLike(job.batch_files)),
    batch_size: job.batch_size,
    overlap_item_count: firstDefined(countLike(job.overlap_item_ids), countLike(job.selected_overlap_item_ids)),
    min_annotators_per_overlap_item: job.min_annotators_per_overlap_item,
  };
  return formatBatchPlanSummary(planLike);
}

export function annotationJobLabel(job) {
  if (!job) return "未选择标注任务";
  return String(firstDefined(job.annotation_id, job.argilla_dataset, job.job_id, job.id, "未命名标注任务"));
}

export function annotationJobStatusLabel(job) {
  const status = String(firstDefined(job?.status, job?.state, "")).toLowerCase();
  if (["done", "completed", "complete", "succeeded", "success", "published", "pushed", "已分发", "已推送"].includes(status)) return "已推送";
  if (["running", "pending", "queued", "in_progress"].includes(status)) return "执行中";
  if (["failed", "error"].includes(status)) return "失败";
  if (["incomplete", "partial"].includes(status)) return "记录不完整";
  if (["cancelled", "canceled"].includes(status)) return "已取消";
  if (!status && job) return "已记录";
  return status || "-";
}

export function annotationJobDispatchLabel(job) {
  if (!job) return "-";
  if ((job.dispatch_mode || "sample") === "batch_plan" || job.batch_plan_id) return "按批次计划分发";
  return "整样本直接推送";
}

export function annotationJobLineageFields(job) {
  if (!job) return [];
  return [
    ["分发方式", annotationJobDispatchLabel(job)],
    ["批次计划", job.batch_plan_id || "未使用批次计划"],
    ["批次清单", job.batch_manifest_path],
    ["分发文件", job.dispatch_path],
    ["批次数", countLike(job.batch_ids) || countLike(job.batch_files)],
    ["一致性样本", countLike(job.overlap_item_ids) || countLike(job.selected_overlap_item_ids)],
    ["记录 ID 策略", job.record_id_policy?.strategy],
  ];
}

export function annotationJobActionAvailability(job, decisions = [], samplePath = "") {
  const dataset = String(firstDefined(job?.argilla_dataset, job?.dataset) || "").trim();
  const resolvedSamplePath = String(firstDefined(samplePath, job?.sample_path) || "").trim();
  const usableDecision = (decisions || []).find((item) => item?.path);
  const decisionSamplePath = String(firstDefined(usableDecision?.sample_path, resolvedSamplePath) || "").trim();

  const pull = !job
    ? { enabled: false, reason: "未选择标注任务。" }
    : !dataset
      ? { enabled: false, reason: "缺少 Argilla 数据集名，不能拉回结果。" }
      : !resolvedSamplePath
        ? { enabled: false, reason: "缺少样本路径，不能拉回结果。" }
        : { enabled: true, reason: "" };

  const agreement = !job
    ? { enabled: false, reason: "未选择标注任务。", decision: null }
    : !usableDecision
      ? { enabled: false, reason: "先拉回标注结果后才能运行一致性检查。", decision: null }
      : !decisionSamplePath
        ? { enabled: false, reason: "标注结果缺少样本路径，不能运行一致性检查。", decision: usableDecision }
        : { enabled: true, reason: "", decision: usableDecision };

  return { pull, agreement };
}

export function agreementAuditsForAnnotationJob(job, decisions = [], audits = []) {
  if (!job) return [];
  const annotationId = String(job.annotation_id || "");
  const dataset = String(firstDefined(job.argilla_dataset, job.dataset, ""));
  const samplePath = String(job.sample_path || "");
  const decisionIds = new Set((decisions || []).map((item) => String(item.decision_id || "")).filter(Boolean));
  const decisionPaths = new Set((decisions || []).map((item) => String(item.path || "")).filter(Boolean));

  return (audits || []).filter((item) => {
    const auditId = String(item.audit_id || "");
    const decisionsPath = String(item.decisions_path || "");
    const auditSamplePath = String(item.sample_path || "");
    return (
      (auditId && (auditId === annotationId || auditId === dataset || decisionIds.has(auditId)))
      || (decisionsPath && decisionPaths.has(decisionsPath))
      || (samplePath && auditSamplePath === samplePath && decisionIds.has(auditId))
    );
  });
}

export function batchPlanDebugFields(plan) {
  if (!plan) return [];
  return [
    ["plan_id", plan.plan_id],
    ["strategy_id", plan.strategy_id],
    ["batch_count", plan.batch_count],
    ["batch_size", plan.batch_size],
    ["overlap_rate", plan.overlap_rate],
    ["overlap_item_count", plan.overlap_item_count],
    ["min_annotators_per_overlap_item", plan.min_annotators_per_overlap_item],
    ["manifest_path", plan.manifest_path],
  ];
}

export function annotationJobDebugFields(job) {
  if (!job) return [];
  return [
    ["dispatch_mode", job.dispatch_mode],
    ["batch_plan_id", job.batch_plan_id],
    ["batch_manifest_path", job.batch_manifest_path],
    ["dispatch_path", job.dispatch_path],
    ["batch_ids", job.batch_ids],
    ["batch_files", job.batch_files],
    ["overlap_item_ids", job.overlap_item_ids],
    ["selected_overlap_item_ids", job.selected_overlap_item_ids],
    ["record_id_policy", job.record_id_policy],
    ["duplicate_record_ids", job.duplicate_record_ids],
    ["manifest_path", job.manifest_path],
    ["created_at", job.created_at],
    ["result", job.result],
  ];
}
