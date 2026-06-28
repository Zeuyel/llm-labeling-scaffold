export const DATA_LAKE_SOURCE_FIELDS = [
  "source_dataset_id",
  "source_object_path",
  "source_manifest_uri",
  "source_object_uri",
  "lake_registry_uri",
];

export function shortHash(value) {
  return value ? `${String(value).slice(0, 12)}...` : "-";
}

export function displayValue(value) {
  if (value === null || value === undefined || value === "") return "-";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

export function stateLabel(value) {
  if (value === "active") return "可用";
  if (value === "archived") return "已归档";
  return value || "-";
}

export function hasEffectiveDataLakeConfig(dataLake) {
  if (!dataLake || typeof dataLake !== "object" || Array.isArray(dataLake)) return false;
  return DATA_LAKE_SOURCE_FIELDS.some((field) => typeof dataLake[field] === "string" && dataLake[field].trim() !== "");
}

export function truthyFlag(value) {
  if (value === true) return true;
  if (typeof value === "number") return value === 1;
  if (typeof value === "string") return ["1", "true", "yes", "on", "enabled"].includes(value.trim().toLowerCase());
  return false;
}

export function usesR2TaskSource(taskSource, task) {
  const sources = [taskSource, task?.task_source, task?.source_type, task?.source];
  return sources.some((value) => {
    const normalized = String(value || "").trim().toLowerCase();
    return normalized === "r2" || normalized.startsWith("r2:");
  });
}

export function usesLocalTaskSource(taskSource, task) {
  const sources = [taskSource, task?.task_source, task?.source_type, task?.source];
  return sources.some((value) => {
    const normalized = String(value || "").trim().toLowerCase();
    return normalized === "local" || normalized.startsWith("local:");
  });
}

export function backendAllowsManualImports(task, allowManualImports) {
  return [
    allowManualImports,
    task?.allow_manual_imports,
    task?.allow_manual_import,
    task?.manual_imports_enabled,
    task?.features?.allow_manual_imports,
    task?.capabilities?.allow_manual_imports,
    task?.permissions?.allow_manual_imports,
  ].some(truthyFlag);
}

export function sourceLabel(item) {
  const source = String(item?.source || "").trim().toLowerCase();
  const hasDataLakeSource = DATA_LAKE_SOURCE_FIELDS.some((field) => {
    const value = item?.[field];
    return typeof value === "string" ? value.trim() !== "" : Boolean(value);
  });
  if (source === "data_lake" || hasDataLakeSource) {
    return "数据湖";
  }
  if (source === "upload") return "手动上传";
  if (source === "manual") return "手动导入";
  return source || "-";
}

export function linkedSamplesLabel(item) {
  return (item?.linked_samples || []).map((sample) => sample.sample_id).filter(Boolean).join(", ") || "-";
}

export function importQualityLabel(item) {
  const missing = item?.missing_ids ?? 0;
  const duplicate = item?.duplicate_ids ?? 0;
  if (missing === 0 && duplicate === 0) return "无缺失/重复";
  return `${missing || 0} 缺失 / ${duplicate || 0} 重复`;
}

export function summarizeImportAsset(item = {}) {
  item = item || {};
  return {
    importId: item.import_id || "-",
    state: stateLabel(item.state || "active"),
    rows: displayValue(item.rows),
    uniqueIds: displayValue(item.unique_ids),
    idQuality: importQualityLabel(item),
    source: sourceLabel(item),
    linkedSamples: linkedSamplesLabel(item),
    contentHash: shortHash(item.content_sha256),
    storagePath: item.path || "-",
    manifestPath: item.manifest_path || "-",
  };
}

export function importActionState(item, { busy = false } = {}) {
  const linkedSamples = item?.linked_samples || [];
  const canArchive = Boolean(item?.import_id) && !busy && linkedSamples.length === 0 && item?.state !== "archived";
  return {
    canViewRows: Boolean(item?.import_id),
    canDownload: Boolean(item?.import_id),
    canArchive,
    archiveDisabledReason: canArchive
      ? ""
      : linkedSamples.length
        ? `导入数据已被样本使用：${linkedSamples.map((sample) => sample.sample_id).filter(Boolean).join(", ")}`
        : item?.state === "archived"
          ? "导入数据已归档"
          : busy
            ? "当前有操作正在执行"
            : "缺少导入编号",
  };
}

export function createImportActions({ hasDataLakeConfig = false, showManualImports = false } = {}) {
  return [
    {
      key: "data_lake",
      label: "从数据湖导入",
      visible: hasDataLakeConfig,
      primary: hasDataLakeConfig,
    },
    {
      key: "manual",
      label: "手动上传",
      visible: showManualImports,
      primary: !hasDataLakeConfig && showManualImports,
    },
  ].filter((action) => action.visible);
}

export function filterImportAuditEvents(events = [], importId = "") {
  const target = String(importId || "");
  if (!target) return [];
  return events.filter((event) => event?.asset_type === "import" && String(event.asset_id || "") === target);
}
