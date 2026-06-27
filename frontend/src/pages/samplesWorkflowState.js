export const STAGE_STATUS = {
  LOADING: "loading",
  READY: "ready",
  RUNNING: "running",
  COMPLETED: "completed",
  BLOCKED: "blocked",
  FAILED: "failed",
};

export function getBatchManifests(sample) {
  const manifests = [];
  const pushManifest = (value) => {
    if (!value) return;
    if (Array.isArray(value)) {
      value.forEach(pushManifest);
      return;
    }
    if (typeof value === "object") manifests.push(value);
  };
  pushManifest(sample?.latest_batch_manifest);
  pushManifest(sample?.batch_manifest);
  pushManifest(sample?.batch);
  pushManifest(sample?.batches);
  pushManifest(sample?.manifest?.batch_manifest);
  pushManifest(sample?.manifest?.batch);
  if (sample?.manifest?.batch_count || sample?.manifest?.batch_size || sample?.manifest?.batches) {
    pushManifest(sample.manifest);
  }
  return manifests;
}

export function hasBatchPlan(sample) {
  return getBatchManifests(sample).length > 0;
}

export function sampleStateLabel(sample) {
  const state = String(sample?.state || sample?.manifest?.state || "active").trim().toLowerCase();
  if (state === "active") return "可用";
  if (state === "archived") return "已归档";
  if (state === "incomplete") return "记录不完整";
  if (state === "failed") return "失败";
  return state || "-";
}

export function sampleCreatedAt(sample) {
  const value = sample?.created_at || sample?.manifest?.created_at || "";
  return value ? String(value).slice(0, 19) : "-";
}

export function filterSampleAuditEvents(events = [], sampleId = "") {
  const target = String(sampleId || "");
  if (!target) return [];
  return events.filter((event) => event?.asset_type === "sample" && String(event.asset_id || "") === target);
}

export function newestSample(samples = []) {
  if (!samples.length) return null;
  return [...samples].sort((a, b) => {
    const aTime = Date.parse(a?.manifest?.created_at || a?.created_at || "");
    const bTime = Date.parse(b?.manifest?.created_at || b?.created_at || "");
    if (Number.isFinite(aTime) && Number.isFinite(bTime) && aTime !== bTime) return bTime - aTime;
    if (Number.isFinite(bTime)) return 1;
    if (Number.isFinite(aTime)) return -1;
    return String(b?.sample_id || "").localeCompare(String(a?.sample_id || ""));
  })[0];
}

export function sampleCompletionNotice({ existedBefore = false, result = null } = {}) {
  const action = result?.action || result?.result?.action;
  const idempotent = result?.idempotent || result?.result?.idempotent;
  if (action === "reused" || idempotent === true || existedBefore) {
    return "样本已存在且内容一致，已复用。下一步：配置批次与一致性策略。";
  }
  if (action === "created" || action === "create") {
    return "样本集已创建。下一步：配置批次与一致性策略。";
  }
  return "样本任务已完成，可能为新建或幂等复用。下一步：配置批次与一致性策略。";
}

export function computeSamplesListView({
  assetsLoading = false,
  imports = [],
  samples = [],
} = {}) {
  const samplesCount = samples.length;
  const importsCount = imports.length;

  return {
    defaultSurface: "list",
    status: assetsLoading ? STAGE_STATUS.LOADING : samplesCount ? STAGE_STATUS.READY : STAGE_STATUS.BLOCKED,
    showList: samplesCount > 0,
    showEmpty: !assetsLoading && samplesCount === 0,
    canCreate: !assetsLoading && importsCount > 0,
    emptyReason: importsCount > 0 ? "暂无样本集。" : "当前任务没有导入资产，请先完成数据导入。",
  };
}

export function computeSampleDetailActions({
  assetsLoading = false,
  busy = false,
  sample = null,
} = {}) {
  const hasSample = Boolean(sample);
  const hasPlan = hasBatchPlan(sample);
  const dependencies = sample?.dependencies || [];
  const disabledByState = assetsLoading || busy || !hasSample;

  return {
    generateBatch: {
      enabled: !disabledByState,
      disabledReason: !hasSample ? "请选择样本集。" : assetsLoading ? "正在读取导入资产/样本集。" : busy ? "已有动作正在执行。" : "",
    },
    pushArgilla: {
      enabled: !disabledByState && hasPlan,
      disabledReason: !hasSample ? "请选择样本集。" : !hasPlan ? "请先生成批次计划。" : assetsLoading ? "正在读取导入资产/样本集。" : busy ? "已有动作正在执行。" : "",
    },
    archive: {
      enabled: !disabledByState && dependencies.length === 0,
      disabledReason: !hasSample ? "请选择样本集。" : dependencies.length ? "样本已被下游资产使用，不能归档。" : assetsLoading ? "正在读取导入资产/样本集。" : busy ? "已有动作正在执行。" : "",
    },
  };
}

export function computeSampleWorkflow({
  assetsLoading = false,
  imports = [],
  samples = [],
  selectedSample = null,
  action = "",
  actionError = "",
} = {}) {
  const importsCount = imports.length;
  const samplesCount = samples.length;
  const hasImports = importsCount > 0;
  const hasSamples = samplesCount > 0;
  const batchReady = Boolean(selectedSample && hasBatchPlan(selectedSample));
  const creating = action === "sample";
  const batching = action === "batch";
  const failed = Boolean(actionError);

  return {
    imports: {
      status: assetsLoading ? STAGE_STATUS.LOADING : hasImports ? STAGE_STATUS.COMPLETED : STAGE_STATUS.BLOCKED,
      badge: assetsLoading ? "读取中" : hasImports ? "已就绪" : "待导入",
      badgeTone: assetsLoading ? "blue" : hasImports ? "green" : "red",
    },
    sample: {
      status: assetsLoading
        ? STAGE_STATUS.LOADING
        : failed && creating
          ? STAGE_STATUS.FAILED
          : creating
            ? STAGE_STATUS.RUNNING
            : hasSamples
              ? STAGE_STATUS.COMPLETED
              : hasImports
                ? STAGE_STATUS.READY
                : STAGE_STATUS.BLOCKED,
      badge: assetsLoading ? "读取中" : creating ? "创建中" : hasSamples ? "已完成" : hasImports ? "可创建" : "待导入",
      badgeTone: creating || assetsLoading ? "blue" : hasSamples ? "green" : hasImports ? "blue" : "red",
    },
    batchConfig: {
      status: assetsLoading
        ? STAGE_STATUS.LOADING
        : creating
          ? STAGE_STATUS.BLOCKED
          : hasSamples
            ? STAGE_STATUS.READY
            : STAGE_STATUS.BLOCKED,
      badge: assetsLoading ? "读取中" : hasSamples ? "可配置" : "待样本",
      badgeTone: assetsLoading || hasSamples ? "blue" : "red",
    },
    batch: {
      status: assetsLoading
        ? STAGE_STATUS.LOADING
        : failed && batching
          ? STAGE_STATUS.FAILED
        : batching
          ? STAGE_STATUS.RUNNING
          : batchReady
            ? STAGE_STATUS.COMPLETED
            : hasSamples
              ? STAGE_STATUS.READY
              : STAGE_STATUS.BLOCKED,
      badge: assetsLoading ? "读取中" : batching ? "生成中" : batchReady ? "已生成" : hasSamples ? "可生成" : "待样本",
      badgeTone: batching || assetsLoading || hasSamples ? "blue" : "red",
    },
    argilla: {
      status: assetsLoading
        ? STAGE_STATUS.LOADING
        : batchReady
          ? STAGE_STATUS.READY
          : STAGE_STATUS.BLOCKED,
      badge: assetsLoading ? "读取中" : batchReady ? "可推送" : hasSamples ? "待批次" : "待样本",
      badgeTone: assetsLoading || batchReady ? "blue" : "red",
      disabledReason: !hasSamples ? "需要先创建样本集。" : !batchReady ? "请先生成批次计划。" : "",
    },
  };
}
