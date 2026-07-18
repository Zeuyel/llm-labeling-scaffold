export const ALLOCATION_STRATEGIES = [
  { value: "shared_queue", label: "共享队列", description: "同一队列按提交要求回收，不承诺个人行数。" },
  { value: "fixed_partition", label: "固定分组", description: "把记录固定分配给指定 annotator。" },
  { value: "calibration_then_partition", label: "校准后分组", description: "先完成校准质量门，再进入正式分组。" },
];

export const STRATEGY_LABELS = Object.fromEntries(ALLOCATION_STRATEGIES.map((item) => [item.value, item.label]));

export const STATUS_LABELS = {
  draft: "草稿",
  pending: "待校验",
  preview: "已预览",
  preview_ready: "预览通过",
  validated: "已校验",
  confirmed: "已确认",
  provisioning: "资源准备中",
  dispatching: "分发中",
  collecting: "回收中",
  completed: "已完成",
  succeeded: "已完成",
  failed: "失败",
  blocked: "已阻塞",
  quarantined: "含隔离结果",
};

export const PHASE_LABELS = {
  calibration: "校准",
  production: "正式",
};

export const MODE_LABELS = {
  direct_assignment: "固定分配",
  shared_queue_advisory: "共享队列建议",
  shared: "共享队列",
  personal: "个人空间",
  calibration: "校准空间",
};

export const COLLECTION_LABELS = {
  accepted: "已接收",
  submitted: "已接收",
  quarantined: "已隔离",
  rejected: "已隔离",
  pending: "待回收",
};

const ACTION_LABELS = {
  edit: "编辑",
  preview: "预览",
  confirm: "确认",
};

function firstDefined(...values) {
  return values.find((value) => value !== undefined && value !== null && value !== "");
}

function stringValue(...values) {
  return String(firstDefined(...values) ?? "").trim();
}

export function planId(plan) {
  return stringValue(plan?.plan_id, plan?.allocation_plan_id, plan?.id);
}

export function planStatus(plan) {
  const value = firstDefined(plan?.status, plan?.state, plan?.lifecycle_state, "draft");
  return stringValue(value, "draft").toLowerCase();
}

export function planStatusLabel(plan) {
  const status = planStatus(plan);
  return STATUS_LABELS[status] || stringValue(plan?.status, plan?.state, "待确认");
}

export function strategyLabel(value) {
  const key = stringValue(value).toLowerCase();
  return STRATEGY_LABELS[key] || (key ? key : "未设置");
}

export function phaseLabel(value) {
  const key = stringValue(value).toLowerCase();
  return PHASE_LABELS[key] || (key ? key : "-");
}

export function modeLabel(value) {
  const key = stringValue(value).toLowerCase();
  return MODE_LABELS[key] || (key ? key : "-");
}

export function collectionLabel(value) {
  const key = stringValue(value).toLowerCase();
  return COLLECTION_LABELS[key] || (key ? key : "待回收");
}

export function valueOrDash(value) {
  return value === undefined || value === null || value === "" ? "-" : String(value);
}

export function normalizePreview(value) {
  const source = value?.preview && typeof value.preview === "object" ? value.preview : value || {};
  const blockingErrors = Array.isArray(source.blocking_errors)
    ? source.blocking_errors
    : Array.isArray(source.errors)
      ? source.errors
      : [];
  return {
    ...source,
    ready: source.ready === true && blockingErrors.length === 0,
    blocking_errors: blockingErrors,
    warnings: Array.isArray(source.warnings) ? source.warnings : [],
    annotator_loads: Array.isArray(source.annotator_loads) ? source.annotator_loads : [],
    overlap: source.overlap && typeof source.overlap === "object" ? source.overlap : {},
    workspace_requirements: Array.isArray(source.workspace_requirements) ? source.workspace_requirements : [],
    dataset_requirements: Array.isArray(source.dataset_requirements) ? source.dataset_requirements : [],
  };
}

export function issueMessage(issue) {
  if (typeof issue === "string") return issue;
  return stringValue(issue?.message, issue?.code, "未说明的阻塞错误");
}

export function previewBlockingMessages(preview) {
  return (normalizePreview(preview).blocking_errors || []).map(issueMessage);
}

export function actionAvailability({ plan, action, busy = false } = {}) {
  if (busy) return { enabled: false, disabledReason: "当前有操作正在执行，请稍候。" };
  if (!plan) return { enabled: false, disabledReason: "请先选择分配计划。" };
  const actionLabel = ACTION_LABELS[action] || action;
  const actionState = plan.actions?.[action] || plan.action_state?.[action] || plan.gates?.[action];
  if (actionState?.enabled === false) {
    return { enabled: false, disabledReason: stringValue(actionState.reason, actionState.disabled_reason, `后端状态不允许${actionLabel}。`) };
  }
  const allowedValue = plan[`${action}_allowed`];
  if (allowedValue === false) {
    return { enabled: false, disabledReason: stringValue(plan[`${action}_blocked_reason`], `后端状态不允许${actionLabel}。`) };
  }
  return { enabled: true, disabledReason: "" };
}

export function editAvailability({ plan, busy = false } = {}) {
  const action = actionAvailability({ plan, action: "edit", busy });
  if (!action.enabled) return action;
  if (["confirmed", "provisioning", "dispatching", "collecting", "completed", "succeeded"].includes(planStatus(plan))) {
    return { enabled: false, disabledReason: "计划已进入执行流程，后端状态不允许编辑。" };
  }
  return action;
}

export function previewAvailability({ plan, busy = false } = {}) {
  return actionAvailability({ plan, action: "preview", busy });
}

export function confirmAvailability({ plan, preview, busy = false } = {}) {
  const action = actionAvailability({ plan, action: "confirm", busy });
  if (!action.enabled) return action;
  if (plan.confirm_allowed === false || plan.can_confirm === false) {
    return { enabled: false, disabledReason: stringValue(plan.confirm_blocked_reason, "后端状态不允许确认计划。") };
  }
  if (["confirmed", "provisioning", "dispatching", "collecting", "completed", "succeeded"].includes(planStatus(plan))) {
    return { enabled: false, disabledReason: "当前计划已进入执行流程，不能重复确认。" };
  }
  if (!preview) return { enabled: false, disabledReason: "请先生成只读预览。" };
  const normalized = normalizePreview(preview);
  const errors = previewBlockingMessages(normalized);
  if (!normalized.ready || errors.length) {
    return { enabled: false, disabledReason: errors[0] || "预览未通过后端阻塞检查。" };
  }
  return { enabled: true, disabledReason: "" };
}

export function normalizeAnnotator(value) {
  return {
    ...value,
    annotator_id: stringValue(value?.annotator_id, value?.id, value?.user_id),
    display_name: stringValue(value?.display_name, value?.username, value?.name, value?.annotator_id, "未命名人员"),
    cohort_id: stringValue(value?.cohort_id, value?.cohort?.id, value?.cohort?.cohort_id),
    cohort_name: stringValue(value?.cohort_name, value?.cohort?.name),
    default_capacity: Number(firstDefined(value?.default_capacity, value?.capacity, 0)),
  };
}

export function emptyAllocationForm(task = {}, taskId = "") {
  task = task || {};
  return {
    workspace: stringValue(task.workspace, task.workspace_slug),
    task_id: taskId || stringValue(task.task_id),
    revision_id: stringValue(task.revision_id, task.revision, task.active_revision_id),
    revision_hash: stringValue(task.revision_hash, task.active_revision_hash),
    trusted_source_key: "",
    trusted_source: null,
    source_manifest_id: "",
    source_manifest_hash: "",
    source_manifest_kind: "sample",
    strategy: "shared_queue",
    cohort_id: "",
    seed: "17",
    algorithm_version: "allocation-v1",
    min_submitted: "1",
    overlap_required_submissions: "",
    calibration_enabled: false,
    calibration_count: "",
    calibration_annotator_ids: "",
    annotator_source_available: false,
    annotators: [],
  };
}

function trustedSourceOption(sample, manifest, kind, task = {}) {
  const sampleId = stringValue(sample?.sample_id);
  const batchId = kind === "batch"
    ? stringValue(manifest?.plan_id, manifest?.batch_id)
    : "";
  const trusted = manifest?.trusted_source || manifest?.allocation_source || sample?.trusted_source || {};
  const previewRequest = trusted?.preview_request
    || manifest?.preview_request
    || sample?.preview_request
    || null;
  const sourceManifest = previewRequest?.request?.source_manifest || {};
  const manifestId = stringValue(
    trusted?.manifest_id,
    manifest?.manifest_id,
    manifest?.source_manifest_id,
    sourceManifest.manifest_id,
    kind === "batch" ? batchId : sampleId,
  );
  const manifestHash = stringValue(
    trusted?.manifest_hash,
    manifest?.manifest_hash,
    manifest?.source_manifest_hash,
    manifest?.source_manifest_sha256,
    sourceManifest.manifest_hash,
  );
  const key = kind === "batch" ? `batch:${sampleId}:${batchId}` : `sample:${sampleId}`;
  const scope = previewRequest?.scope || {};
  const request = previewRequest?.request || {};
  const taskWorkspace = stringValue(task?.workspace, task?.workspace_slug);
  const taskId = stringValue(task?.task_id);
  const taskRevision = stringValue(task?.revision_id, task?.revision, task?.active_revision_id);
  const scopeMatchesTask = (!taskWorkspace || scope.workspace === taskWorkspace)
    && (!taskId || scope.task_id === taskId)
    && (!taskRevision || scope.revision_id === taskRevision);
  const available = Boolean(
    sampleId
    && manifestId
    && manifestHash
    && scope.workspace
    && scope.task_id
    && scope.revision_id
    && scope.revision_hash
    && request.source_manifest
    && Array.isArray(request.records)
    && scopeMatchesTask,
  );
  return {
    key,
    kind,
    sample_id: sampleId,
    batch_id: batchId,
    label: kind === "batch" ? `批次 · ${sampleId} / ${batchId || "未命名"}` : `样本 · ${sampleId}`,
    manifest_id: manifestId,
    manifest_hash: manifestHash,
    preview_request: previewRequest,
    available,
    disabled_reason: available
      ? ""
      : scopeMatchesTask
        ? "后端未返回可信 preview_request、revision hash 或 manifest hash。"
        : "可信来源与当前任务范围不一致。",
  };
}

export function trustedAllocationSourceOptions(samples = [], task = {}) {
  return (Array.isArray(samples) ? samples : []).flatMap((sample) => {
    if (!sample || String(sample.state || "").toLowerCase() === "archived") return [];
    const options = [];
    const sampleManifest = sample.manifest && typeof sample.manifest === "object"
      ? sample.manifest
      : sample.trusted_source && typeof sample.trusted_source === "object"
        ? sample.trusted_source
        : null;
    if (sampleManifest) {
      options.push(trustedSourceOption(sample, sampleManifest, "sample", task));
    }
    const batches = Array.isArray(sample.batch_manifests)
      ? sample.batch_manifests
      : Array.isArray(sample.batches) ? sample.batches : [];
    batches.forEach((manifest) => {
      if (manifest && typeof manifest === "object") options.push(trustedSourceOption(sample, manifest, "batch", task));
    });
    return options;
  });
}

export function applyTrustedSource(form, source) {
  if (!source) {
    return {
      ...form,
      trusted_source_key: "",
      trusted_source: null,
      source_manifest_id: "",
      source_manifest_hash: "",
      source_manifest_kind: "sample",
    };
  }
  const scope = source.preview_request?.scope || {};
  return {
    ...form,
    trusted_source_key: source.key,
    trusted_source: source,
    workspace: stringValue(scope.workspace, form.workspace),
    task_id: stringValue(scope.task_id, form.task_id),
    revision_id: stringValue(scope.revision_id, form.revision_id),
    revision_hash: stringValue(scope.revision_hash, form.revision_hash),
    source_manifest_id: source.manifest_id,
    source_manifest_hash: source.manifest_hash,
    source_manifest_kind: source.kind,
  };
}

export function allocationFormAvailability({ form, sourceLoading = false, sourceError = "", busy = false } = {}) {
  if (busy) return { enabled: false, disabledReason: "当前有操作正在执行，请稍候。" };
  if (sourceLoading) return { enabled: false, disabledReason: "正在读取可信来源。" };
  if (sourceError) return { enabled: false, disabledReason: "可信来源读取失败，请重试。" };
  if (!form?.trusted_source?.available) {
    return { enabled: false, disabledReason: "暂无可用来源：请选择后端返回的可信样本或批次。" };
  }
  if (form.annotator_source_available !== true) {
    return { enabled: false, disabledReason: "标注人员组接口尚未接入。" };
  }
  if (!String(form.cohort_id || "").trim()) {
    return { enabled: false, disabledReason: "尚未选择后端返回的人员组。" };
  }
  if (!Array.isArray(form.annotators) || !form.annotators.length) {
    return { enabled: false, disabledReason: "尚未选择后端返回的标注人员。" };
  }
  if (form.annotators.some((item) => item.capacity === "" || !Number.isFinite(Number(item.capacity)) || Number(item.capacity) < 0)) {
    return { enabled: false, disabledReason: "后端返回的人员容量不可用。" };
  }
  return { enabled: true, disabledReason: "" };
}

function calibrationFromForm(form) {
  if (!form.calibration_enabled) return null;
  const expected = String(form.calibration_annotator_ids || "")
    .split(/[\n,]/)
    .map((item) => item.trim())
    .filter(Boolean);
  return {
    count: form.calibration_count === "" ? undefined : Number(form.calibration_count),
    expected_annotator_ids: expected.length ? expected : undefined,
    exclude_from_production: true,
  };
}

function overlapRulesFromForm(form) {
  if (form.overlap_required_submissions === "") return [];
  return [{ required_submissions: Number(form.overlap_required_submissions) }];
}

export function allocationPreviewPayload(form) {
  const payload = form.trusted_source?.preview_request;
  if (!form.trusted_source?.available || !payload?.scope || !payload?.request) {
    throw new Error("暂无可用来源：后端尚未返回可信的 allocation preview 请求。");
  }
  if (form.annotator_source_available !== true) {
    throw new Error("人员组接口尚未接入，暂不能选择标注人员。");
  }
  const annotators = (form.annotators || []).map((item) => ({
    annotator_id: item.annotator_id,
    cohort_id: item.cohort_id || form.cohort_id,
    capacity: Number(item.capacity),
  }));
  if (!annotators.length) throw new Error("人员组接口尚未接入，暂不能选择标注人员。");
  return {
    scope: payload.scope,
    request: {
      ...payload.request,
      strategy: form.strategy,
      annotators,
      seed: Number(form.seed),
      algorithm_version: String(form.algorithm_version || "allocation-v1").trim(),
      overlap_rules: form.overlap_required_submissions === ""
        ? (Array.isArray(payload.request.overlap_rules) ? payload.request.overlap_rules : [])
        : overlapRulesFromForm(form),
      calibration: form.calibration_enabled ? calibrationFromForm(form) : null,
    },
  };
}

export function allocationPlanPayload(form, preview) {
  const preview_request = allocationPreviewPayload(form);
  const planFingerprint = stringValue(preview?.plan_fingerprint);
  if (!planFingerprint) throw new Error("缺少后端 preview plan_fingerprint，不能创建正式计划。");
  return {
    ...preview_request,
    plan_fingerprint: planFingerprint,
    cohort_id: String(form.cohort_id || "").trim(),
    min_submitted: Number(form.min_submitted),
  };
}

export function formFromAllocationPlan(plan, task = {}, taskId = "") {
  task = task || {};
  const request = plan?.preview_request?.request || plan?.request || plan || {};
  const scope = plan?.preview_request?.scope || plan?.scope || {};
  const source = request.source_manifest || plan?.source_manifest || {};
  const calibration = request.calibration || plan?.calibration || {};
  const annotators = (request.annotators || plan?.annotators || []).map((item) => ({
    ...normalizeAnnotator(item),
    capacity: firstDefined(item.capacity, item.default_capacity, 0),
  }));
  return {
    ...emptyAllocationForm(task, taskId),
    workspace: stringValue(scope.workspace, plan?.workspace, task.workspace, task.workspace_slug),
    task_id: stringValue(scope.task_id, plan?.task_id, taskId),
    revision_id: stringValue(scope.revision_id, plan?.revision_id, task.revision_id, task.revision),
    revision_hash: stringValue(scope.revision_hash, plan?.revision_hash, task.revision_hash),
    source_manifest_id: stringValue(source.manifest_id, plan?.source_manifest_id),
    source_manifest_hash: stringValue(source.manifest_hash, plan?.source_manifest_hash),
    source_manifest_kind: stringValue(source.kind, plan?.source_manifest_kind, "sample"),
    strategy: stringValue(request.strategy, plan?.strategy, "shared_queue"),
    cohort_id: stringValue(request.cohort_id, plan?.cohort_id, annotators[0]?.cohort_id),
    seed: String(firstDefined(request.seed, plan?.seed, 17)),
    algorithm_version: stringValue(request.algorithm_version, plan?.algorithm_version, "allocation-v1"),
    min_submitted: String(firstDefined(request.min_submitted, plan?.min_submitted, 1)),
    overlap_required_submissions: String(firstDefined(request.overlap_rules?.[0]?.required_submissions, "")),
    calibration_enabled: Boolean(request.calibration || plan?.calibration),
    calibration_count: String(firstDefined(calibration.count, "")),
    calibration_annotator_ids: Array.isArray(calibration.expected_annotator_ids)
      ? calibration.expected_annotator_ids.join("\n")
      : "",
    annotator_source_available: false,
    trusted_source_key: stringValue(plan.trusted_source?.key, plan.trusted_source_key, plan.source_ref?.key),
    trusted_source: plan.trusted_source || null,
    annotators,
  };
}

export function normalizeAssignments(value) {
  const source = Array.isArray(value) ? value : value?.assignments || value?.items || [];
  return source.map((item) => ({
    ...item,
    assignment_id: stringValue(item.assignment_id, item.id),
    phase: stringValue(item.phase),
    record_id: stringValue(item.record_id, item.source_record_id),
    batch_id: stringValue(item.batch_id),
    assignee_id: stringValue(item.assignee_id, item.annotator_id, item.assignee),
    workspace_uuid: stringValue(item.workspace_uuid, item.argilla_workspace_id, item.workspace_id),
    dataset_uuid: stringValue(item.dataset_uuid, item.argilla_dataset_id, item.dataset_id),
    collection_status: stringValue(item.collection_status, item.status, "pending").toLowerCase(),
  }));
}

export function progressPercent(progress) {
  const value = Number(firstDefined(progress?.percent, progress?.percentage, progress?.completed_percent));
  if (Number.isFinite(value)) return Math.min(100, Math.max(0, value));
  const completed = Number(firstDefined(progress?.completed, progress?.accepted, 0));
  const total = Number(firstDefined(progress?.total, progress?.expected, 0));
  return total > 0 ? Math.min(100, Math.max(0, (completed / total) * 100)) : 0;
}

export function statusBadgeClass(label) {
  if (["已完成", "已确认", "已接收", "预览通过"].includes(label)) return "badge-green";
  if (["分发中", "回收中", "资源准备中", "已预览"].includes(label)) return "badge-blue";
  if (["失败", "已阻塞", "已隔离"].includes(label)) return "badge-red";
  if (["草稿", "待校验", "待回收"].includes(label)) return "badge-yellow";
  return "badge-gray";
}
