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

export function firstDefinedString(...values) {
  const value = firstDefined(...values);
  return value === undefined || value === null ? "" : String(value);
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
    key: firstDefinedString(manifest.plan_id, manifest.batch_plan_id, manifestPath, planDir, planId),
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

export function annotationJobKey(job) {
  return firstDefinedString(job?.annotation_id, job?.argilla_dataset, job?.job_id, job?.id, job?.manifest_path);
}

export function annotationJobLabel(job) {
  if (!job) return "未选择标注任务";
  return firstDefinedString(job.annotation_id, job.argilla_dataset, job.job_id, job.id, "未命名标注任务");
}

export function annotationJobStatusLabel(job) {
  const statusValue = firstDefined(job?.status, job?.state);
  const status = statusValue === undefined || statusValue === null ? "" : String(statusValue).toLowerCase();
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
    ["批次数", firstDefined(countLike(job.batch_ids), countLike(job.batch_files))],
    ["一致性样本", firstDefined(countLike(job.overlap_item_ids), countLike(job.selected_overlap_item_ids))],
    ["记录 ID 策略", job.record_id_policy?.strategy],
  ];
}

export function annotationJobActionAvailability(job, decisions = [], samplePath = "") {
  const dataset = firstDefinedString(job?.argilla_dataset, job?.dataset).trim();
  const resolvedSamplePath = firstDefinedString(samplePath, job?.sample_path).trim();
  const usableDecision = (decisions || []).find((item) => item?.path);
  const decisionSamplePath = firstDefinedString(usableDecision?.sample_path, resolvedSamplePath).trim();

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
  const dataset = firstDefinedString(job.argilla_dataset, job.dataset);
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

export function decisionArtifactKey(decision) {
  return firstDefinedString(decision?.decision_id, decision?.path, decision?.argilla_dataset, decision?.created_at);
}

export function decisionArtifactLabel(decision) {
  if (!decision) return "未选择标注结果";
  return firstDefinedString(decision.decision_id, decision.argilla_dataset, decision.path, "未命名标注结果");
}

export function decisionArtifactSourceLabel(decision) {
  const source = String(decision?.source || "").trim().toLowerCase();
  if (source === "argilla") return "Argilla";
  if (source === "run") return "本地运行";
  if (source === "unknown") return "来源待确认";
  return decision?.source || "-";
}

export function decisionArtifactStatusLabel(decision) {
  if (!decision) return "-";
  const statusValue = firstDefined(decision.status, decision.state);
  const status = statusValue === undefined || statusValue === null ? "" : String(statusValue).trim().toLowerCase();
  if (["done", "completed", "complete", "succeeded", "success", "pulled", "recovered", "已回收"].includes(status)) return "已回收";
  if (["running", "pending", "queued", "in_progress"].includes(status)) return "执行中";
  if (["failed", "error"].includes(status)) return "失败";
  if (["incomplete", "partial"].includes(status) || !decision.path) return "记录不完整";
  return status || "已回收";
}

export function decisionArtifactLineageFields(decision) {
  if (!decision) return [];
  return [
    ["来源", decisionArtifactSourceLabel(decision)],
    ["标注任务", firstDefined(decision.annotation_id, decision.source_annotation_id)],
    ["Argilla 数据集", decision.argilla_dataset],
    ["样本编号", decision.sample_id],
    ["样本路径", decision.sample_path],
    ["分发方式", annotationJobDispatchLabel(decision)],
    ["批次计划", decision.batch_plan_id],
    ["批次清单", decision.batch_manifest_path],
    ["批次数", firstDefined(countLike(decision.batch_ids), countLike(decision.batch_files))],
    ["一致性样本", firstDefined(countLike(decision.overlap_item_ids), countLike(decision.selected_overlap_item_ids))],
    ["产物路径", decision.path],
  ];
}

export function decisionArtifactDebugFields(decision) {
  if (!decision) return [];
  return [
    ["decision_id", decision.decision_id],
    ["source", decision.source],
    ["argilla_dataset", decision.argilla_dataset],
    ["annotation_id", decision.annotation_id],
    ["source_annotation_id", decision.source_annotation_id],
    ["sample_id", decision.sample_id],
    ["sample_path", decision.sample_path],
    ["path", decision.path],
    ["rows", decision.rows],
    ["dispatch_mode", decision.dispatch_mode],
    ["batch_plan_id", decision.batch_plan_id],
    ["batch_manifest_path", decision.batch_manifest_path],
    ["batch_ids", decision.batch_ids],
    ["batch_files", decision.batch_files],
    ["overlap_item_ids", decision.overlap_item_ids],
    ["created_at", decision.created_at],
    ["result", decision.result],
  ];
}

export function agreementAuditKey(audit) {
  return firstDefinedString(audit?.audit_id, audit?.summary_path, audit?.created_at);
}

export function agreementAuditLabel(audit) {
  if (!audit) return "未选择一致性检查";
  return firstDefinedString(audit.audit_id, audit.summary_path, "未命名检查");
}

export function agreementAuditStatusLabel(audit) {
  if (!audit) return "-";
  const stateValue = firstDefined(audit.status, audit.state);
  const state = stateValue === undefined || stateValue === null ? "" : String(stateValue).trim().toLowerCase();
  if (["running", "pending", "queued", "in_progress"].includes(state)) return "执行中";
  if (["failed", "error"].includes(state)) return "失败";
  if (["incomplete", "partial"].includes(state)) return "记录不完整";
  if (audit.passed === true) return "通过";
  if (audit.passed === false) return "未通过";
  return state || "已记录";
}

export function agreementAuditCoverageLabel(audit) {
  if (!audit) return "-";
  const coverage = audit.sample_coverage || {};
  const covered = countLike(firstDefined(coverage.covered_ids, audit.covered_ids));
  const total = countLike(firstDefined(coverage.sample_ids, audit.sample_unique_ids, audit.sample_rows));
  const rate = coverage.coverage_rate;
  if (covered === undefined || total === undefined) return "-";
  const rateText = Number.isFinite(Number(rate)) ? `（${Math.round(Number(rate) * 1000) / 10}%）` : "";
  return `${covered}/${total}${rateText}`;
}

export function agreementAuditIssueSummary(audit) {
  if (!audit) return "-";
  if (audit.passed === true) return "无阻塞问题";
  const counts = audit.issue_counts || {};
  const labels = [
    ["sample_missing_id_rows", "样本缺失 ID"],
    ["sample_duplicate_ids", "样本重复 ID"],
    ["decision_missing_id_rows", "标注缺失 ID"],
    ["unknown_ids", "未知样本 ID"],
    ["duplicate_submissions", "重复提交"],
    ["primary_label_missing", "缺主标签"],
    ["below_min_submitted_ids", "提交数不足"],
  ];
  const parts = labels
    .map(([key, label]) => [label, Number(counts[key] || 0)])
    .filter(([, count]) => count > 0)
    .map(([label, count]) => `${label} ${count}`);
  if (parts.length) return parts.join("；");
  if (audit.passed === false) return "存在未归类问题";
  return "-";
}

export function agreementAuditsForDecision(decision, audits = []) {
  if (!decision) return [];
  const decisionId = String(decision.decision_id || "");
  const dataset = firstDefinedString(decision.argilla_dataset, decision.dataset);
  const annotationId = firstDefinedString(decision.annotation_id, decision.source_annotation_id);
  const decisionPath = String(decision.path || "");
  const samplePath = String(decision.sample_path || "");

  return (audits || []).filter((item) => {
    const auditId = String(item.audit_id || "");
    const decisionsPath = String(item.decisions_path || "");
    const auditSamplePath = String(item.sample_path || "");
    return (
      (auditId && [decisionId, dataset, annotationId].filter(Boolean).includes(auditId))
      || (decisionPath && decisionsPath === decisionPath)
      || (samplePath && auditSamplePath === samplePath && auditId && [decisionId, dataset, annotationId].filter(Boolean).includes(auditId))
    );
  });
}

export function agreementAuditDebugFields(audit) {
  if (!audit) return [];
  return [
    ["audit_id", audit.audit_id],
    ["passed", audit.passed],
    ["created_at", audit.created_at],
    ["sample_path", audit.sample_path],
    ["decisions_path", audit.decisions_path],
    ["summary_path", audit.summary_path],
    ["id_field", audit.id_field],
    ["primary_label", audit.primary_label],
    ["min_submitted", audit.min_submitted],
    ["sample_rows", audit.sample_rows],
    ["sample_unique_ids", audit.sample_unique_ids],
    ["decision_rows", audit.decision_rows],
    ["sample_coverage", audit.sample_coverage],
    ["issue_counts", audit.issue_counts],
    ["label_distribution", audit.label_distribution],
    ["input_fingerprint", audit.input_fingerprint],
  ];
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
