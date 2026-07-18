const AUTH_TOKEN_KEY = "lls.basicAuthToken";

function storage() {
  if (typeof window === "undefined") return null;
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}

let authToken = storage()?.getItem(AUTH_TOKEN_KEY) || "";

function requestHeaders(headers = {}) {
  const next = {};
  if (typeof Headers !== "undefined" && headers instanceof Headers) {
    headers.forEach((value, key) => { next[key] = value; });
  } else {
    Object.assign(next, headers);
  }
  next.Accept = next.Accept || "application/json";
  if (authToken && !Object.keys(next).some((key) => key.toLowerCase() === "authorization")) {
    next.Authorization = `Basic ${authToken}`;
  }
  return next;
}

function encodeBasicCredentials(username, password) {
  const bytes = new TextEncoder().encode(`${username}:${password}`);
  let binary = "";
  bytes.forEach((byte) => { binary += String.fromCharCode(byte); });
  return btoa(binary);
}

function notifyUnauthorized() {
  if (typeof window !== "undefined") {
    window.dispatchEvent(new CustomEvent("lls:unauthorized"));
  }
}

async function req(path, opts = {}) {
  const res = await fetch(path, { ...opts, headers: requestHeaders(opts.headers) });
  if (res.status === 401) notifyUnauthorized();
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText}: ${text.slice(0, 160)}`);
  }
  return res.json();
}

export const login = async (username, password) => {
  const token = encodeBasicCredentials(username, password);
  const data = await req("/api/session", { headers: { Authorization: `Basic ${token}` } });
  authToken = token;
  storage()?.setItem(AUTH_TOKEN_KEY, token);
  return data;
};

export const logout = () => {
  authToken = "";
  storage()?.removeItem(AUTH_TOKEN_KEY);
};

export const getSession = () => req("/api/session");

const q = (obj) =>
  Object.entries(obj)
    .filter(([, v]) => v !== undefined && v !== null && v !== "")
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`)
    .join("&");

const keyPart = (value, fallback) =>
  String(value || fallback)
    .trim()
    .replace(/[^A-Za-z0-9_.-]+/g, "_")
    .replace(/_+/g, "_")
    .replace(/^[_\-.]+|[_\-.]+$/g, "")
    .slice(0, 64) || fallback;

export const dataLakeImportIdempotencyKey = (taskId, importId = "") => {
  const random =
    globalThis.crypto?.randomUUID?.()
    || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
  return `data-lake-import:${keyPart(taskId, "task")}:${keyPart(importId, "default")}:${random}`;
};

export const dataLakeImportPayload = (taskId, payload = {}) => {
  const body = { task_id: taskId, ...payload };
  if (body.dry_run || body.dryRun) return body;
  return {
    ...body,
    confirm: body.confirm ?? true,
    idempotency_key: body.idempotency_key || dataLakeImportIdempotencyKey(taskId, body.import_id),
  };
};

export const taskPublishIdempotencyKey = (taskId, draftFingerprint) =>
  `task-publish:${keyPart(taskId, "task")}:${keyPart(draftFingerprint, "draft")}`;

export const getTasks = () => req("/api/tasks");
export const syncTasks = () => req("/api/tasks/sync", { method: "POST" });
export const getTaskControl = (taskId) =>
  req(`/api/task/control?${q({ task_id: taskId })}`).then((data) => data.task || data);
const unwrapSettings = (data) => data.settings || data.config || data || {};

export const getSettings = () => req("/api/settings").then(unwrapSettings);
export const getConfig = getSettings;
export const getImports = (taskId) => req(`/api/task/imports?${q({ task_id: taskId })}`);
export const getImportDetail = (taskId, importId) => req(`/api/import/detail?${q({ task_id: taskId, import_id: importId })}`);
export const getImportRows = (taskId, importId, opts = {}) =>
  req(`/api/import/rows?${q({ task_id: taskId, import_id: importId, offset: opts.offset, limit: opts.limit, q: opts.query })}`);
export const getTaskRuns = (taskId) => req(`/api/task/runs?${q({ task_id: taskId })}`);
export const getTaskSamples = (taskId) => req(`/api/task/samples?${q({ task_id: taskId })}`);
export const getTaskModels = (taskId) => req(`/api/task/models?${q({ task_id: taskId })}`);
export const getTaskGoldVersions = (taskId) => req(`/api/task/gold_versions?${q({ task_id: taskId })}`);
export const getProfilePresets = () => req("/api/profile/presets");
export const getTaskProfile = (taskId, preset) => req(`/api/task/profile?${q({ task_id: taskId, preset })}`);
export const getTaskGraph = (taskId, preset) => req(`/api/task/graph?${q({ task_id: taskId, preset })}`);
export const getAnnotationJobs = (taskId) => req(`/api/task/annotation_jobs?${q({ task_id: taskId })}`);
export const getAgreementAudits = (taskId) => req(`/api/task/agreement_audits?${q({ task_id: taskId })}`);
export const getDecisionArtifacts = (taskId) => req(`/api/task/decision_artifacts?${q({ task_id: taskId })}`);
export const getJobs = (taskId) => req(`/api/jobs?${q({ task_id: taskId })}`);
export const getAuditEvents = (taskId) => req(`/api/task/audit?${q({ task_id: taskId })}`);
export const getDataLakeStatus = (taskId) => req(`/api/task/data_lake?${q({ task_id: taskId })}`);
export const getDataLakeCatalog = (datasetId = "") => req(`/api/data_lake/catalog?${q({ dataset_id: datasetId })}`);
export const getArgillaStatus = () => req("/api/argilla/status");
export const getTaskArchivePlan = (taskId) => req(`/api/task/archive_plan?${q({ task_id: taskId })}`);

export const createTask = (payload) =>
  req("/api/tasks", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

export const updateTask = (taskId, payload, draftFingerprint) =>
  req(`/api/tasks/${encodeURIComponent(taskId)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json", "If-Match": draftFingerprint },
    body: JSON.stringify(payload),
  });

export const publishTask = (taskId, draftFingerprint, reason = "面板发布任务") =>
  req(`/api/tasks/${encodeURIComponent(taskId)}/publish`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "If-Match": draftFingerprint },
    body: JSON.stringify({
      confirm: true,
      idempotency_key: taskPublishIdempotencyKey(taskId, draftFingerprint),
      reason,
    }),
  });

export const updateSettings = (payload) =>
  req("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  }).then(unwrapSettings);

export const deleteTask = (taskId, opts = {}) =>
  req(`/api/tasks?${q({ task_id: taskId, delete_runs: opts.deleteRuns ? 1 : undefined })}`, {
    method: "DELETE",
  }).then((data) => data.task || data);

export const archiveTask = (taskId) => deleteTask(taskId);

export const executeTaskArchive = (taskId, reason = "") =>
  req("/api/task/archive", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ task_id: taskId, reason }),
  }).then((data) => data.archive || data);

export const cleanupTaskCache = (taskId) =>
  req("/api/task/cache_cleanup", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ task_id: taskId }),
  }).then((data) => data.cleanup || data);

export const startAction = (taskPath, action, params) =>
  req("/api/action", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ task: taskPath, action, params }),
  }).then((data) => data.job || data);

export const importSuggestions = (taskId, annotationId, suggestionId, text, opts = {}) =>
  req(`/api/suggestions/import?${q({
    task_id: taskId,
    annotation_id: annotationId,
    suggestion_id: suggestionId,
    provider: opts.provider,
    prompt_version: opts.promptVersion,
    publish: opts.publish ? 1 : undefined,
  })}`, {
    method: "POST",
    headers: { "Content-Type": "application/x-ndjson" },
    body: text,
  }).then((data) => data.suggestions || data);

export async function waitForJob(taskId, jobId, attempts = 30) {
  for (let i = 0; i < attempts; i += 1) {
    const data = await getJobs(taskId);
    const job = (data.jobs || []).find((item) => item.id === jobId);
    if (job && !["pending", "running"].includes(job.status)) return job;
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  return null;
}

export const importJsonl = (taskId, name, text) =>
  req(`/api/import?${q({ task_id: taskId, name })}`, {
    method: "POST",
    headers: { "Content-Type": "application/x-ndjson" },
    body: text,
  });

export const importFromDataLake = (taskId, payload = {}) =>
  req("/api/import/data_lake", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(dataLakeImportPayload(taskId, payload)),
  });

export const archiveImport = (taskId, importId, reason = "") =>
  req(`/api/import?${q({ task_id: taskId, import_id: importId, reason })}`, {
    method: "DELETE",
  }).then((data) => data.import || data);

export const archiveSample = (taskId, sampleId, reason = "") =>
  req(`/api/sample?${q({ task_id: taskId, sample_id: sampleId, reason })}`, {
    method: "DELETE",
  }).then((data) => data.sample || data);

export const archiveAnnotationJob = (taskId, annotationId, reason = "") =>
  req(`/api/annotation_job?${q({ task_id: taskId, annotation_id: annotationId, reason })}`, {
    method: "DELETE",
  }).then((data) => data.annotation_job || data);

export const importDownloadUrl = (taskId, importId) =>
  `/api/import/download?${q({ task_id: taskId, import_id: importId })}`;

export const suggestionDownloadUrl = (taskId, annotationId, suggestionId, kind = "template") =>
  `/api/suggestions/download?${q({ task_id: taskId, annotation_id: annotationId, suggestion_id: suggestionId, kind })}`;

const ALLOCATION_PLANS_PATH = "/api/allocation/plans";

const allocationScopeQuery = (scope = {}) => q({
  workspace: scope.workspace,
  task_id: scope.taskId ?? scope.task_id,
  revision_id: scope.revisionId ?? scope.revision_id,
  phase: scope.phase,
  status: scope.status,
  limit: scope.limit,
  cursor: scope.cursor,
});

const allocationScopedPath = (path, scope = {}) => {
  const query = allocationScopeQuery(scope);
  return query ? `${path}?${query}` : path;
};

const allocationPlanPath = (planId) =>
  `${ALLOCATION_PLANS_PATH}/${encodeURIComponent(planId)}`;

export const allocationPlanConfirmIdempotencyKey = (planId, fingerprint = "") =>
  `allocation-plan-confirm:${keyPart(planId, "plan")}:${keyPart(fingerprint, "preview")}`;

export const getAllocationPlans = (scope = {}) =>
  req(allocationScopedPath(ALLOCATION_PLANS_PATH, scope));

export const listAllocationPlans = getAllocationPlans;

export const getAllocationPlan = (planId, scope = {}) =>
  req(allocationScopedPath(allocationPlanPath(planId), scope));

export const createAllocationPlan = (payload) =>
  req(ALLOCATION_PLANS_PATH, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

export const updateAllocationPlan = (planId, payload) =>
  req(allocationPlanPath(planId), {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

export const previewAllocation = (payload) =>
  req("/api/allocation/preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

export const confirmAllocationPlan = (planId, payload = {}) => {
  const fingerprint = payload.plan_fingerprint || payload.preview_fingerprint || "";
  return req(`${allocationPlanPath(planId)}/confirm`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(fingerprint ? { "If-Match": fingerprint } : {}),
    },
    body: JSON.stringify({
      ...payload,
      confirm: true,
      idempotency_key: payload.idempotency_key || allocationPlanConfirmIdempotencyKey(planId, fingerprint),
    }),
  });
};

export const getAllocationPlanProgress = (planId, scope = {}) =>
  req(allocationScopedPath(`${allocationPlanPath(planId)}/progress`, scope));

export const getAllocationProgress = getAllocationPlanProgress;

export const getAllocationPlanAssignments = (planId, scope = {}, filters = {}) =>
  req(allocationScopedPath(`${allocationPlanPath(planId)}/assignments`, {
    ...scope,
    ...filters,
  }));

export const getAllocationAssignments = getAllocationPlanAssignments;

export const getAllocationPlanCollection = (planId, scope = {}, status = "") =>
  req(allocationScopedPath(`${allocationPlanPath(planId)}/collection`, { ...scope, status }));

export const getAllocationAnnotators = (scope = {}) =>
  req(allocationScopedPath("/api/allocation/annotators", scope));

export const getAllocationCohorts = (scope = {}) =>
  req(allocationScopedPath("/api/allocation/cohorts", scope));
