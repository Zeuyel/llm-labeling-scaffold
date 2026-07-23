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

const API_ERROR_MESSAGES = {
  resource_not_found: "资源不存在",
  permission_denied: "权限不足",
  authorization_unavailable: "授权服务暂时不可用",
  repository_unavailable: "标注人员服务暂时不可用",
  external_service_unavailable: "外部服务暂时不可用",
  service_unavailable: "标注人员服务暂时不可用",
  resource_conflict: "资源状态冲突，请刷新后重试",
  workspace_required: "必须选择 Scaffold 工作区",
  invalid_workspace: "Scaffold 工作区参数无效",
  workspace_selector_conflict: "工作区选择参数不一致",
  invalid_identifier: "资源标识无效",
  invalid_principal_id: "成员标识无效",
  invalid_role: "成员角色无效",
  invalid_request: "请求参数无效",
  invalid_field: "请求字段无效",
  unknown_field: "请求包含未允许的字段",
  server_context_forbidden: "请求上下文字段由服务端确定",
  sensitive_input_forbidden: "敏感字段只能通过专用一次性接口提交",
  annotator_not_verified: "未验证的标注人员不能加入人员组",
  owner_account_forbidden: "owner 账号不能加入人员组",
  annotator_role_required: "人员组成员必须是 annotator",
  empty_update: "人员组更新至少需要一个可变字段",
  idempotency_key_required: "写操作缺少幂等键",
  missing_idempotency_key: "写操作缺少幂等键",
  invalid_idempotency_key: "幂等键无效",
  idempotency_conflict: "幂等键与既有请求冲突，请刷新后重试",
  idempotency_key_conflict: "幂等键与请求内容不一致，请重新提交",
  idempotency_replay_mismatch: "幂等重试内容不一致，请重新提交",
  membership_conflict: "成员状态冲突，请刷新后重试",
  last_workspace_admin: "不能移除或降级最后一个管理员",
};

const HTTP_ERROR_MESSAGES = {
  401: "登录已失效，请重新登录",
  403: "权限不足",
  404: "资源不存在",
  409: "资源状态冲突，请刷新后重试",
  422: "请求参数无效",
  500: "服务暂时不可用",
  503: "服务暂时不可用",
};

function requestError(status, text) {
  let payload = null;
  try {
    payload = JSON.parse(text);
  } catch {
    payload = null;
  }
  const code = typeof payload?.code === "string" ? payload.code : `http_${status}`;
  const error = new Error(API_ERROR_MESSAGES[code] || HTTP_ERROR_MESSAGES[status] || `请求失败（HTTP ${status}）`);
  error.code = code;
  error.status = status;
  if (typeof payload?.field === "string") error.field = payload.field;
  return error;
}

function localRequestError(code, status, message) {
  const error = new Error(message);
  error.code = code;
  error.status = status;
  return error;
}

async function req(path, opts = {}) {
  let res;
  try {
    res = await fetch(path, { ...opts, headers: requestHeaders(opts.headers) });
  } catch {
    throw localRequestError("network_error", 0, "无法连接服务，请稍后重试");
  }
  if (res.status === 401) notifyUnauthorized();
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw requestError(res.status, text);
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

export const managementIdempotencyKey = (operation, workspace, resource = "") => {
  const random =
    globalThis.crypto?.randomUUID?.()
    || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
  return `annotator:${keyPart(operation, "operation").slice(0, 32)}:${keyPart(workspace, "workspace").slice(0, 48)}:${keyPart(resource, "resource").slice(0, 48)}:${random}`;
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
export const getTaskControl = (taskId, workspace = "") =>
  req(`/api/task/control?${q({ task_id: taskId, workspace })}`).then((data) => data.task || data);
const unwrapSettings = (data) => data.settings || data.config || data || {};

export const getSettings = () => req("/api/settings").then(unwrapSettings);
export const getConfig = getSettings;
export const getImports = (taskId) => req(`/api/task/imports?${q({ task_id: taskId })}`);
export const getImportDetail = (taskId, importId) => req(`/api/import/detail?${q({ task_id: taskId, import_id: importId })}`);
export const getImportRows = (taskId, importId, opts = {}) =>
  req(`/api/import/rows?${q({ task_id: taskId, import_id: importId, offset: opts.offset, limit: opts.limit, q: opts.query })}`);
export const getTaskRuns = (taskId) => req(`/api/task/runs?${q({ task_id: taskId })}`);
export const getTaskSamples = (taskId, workspace = "") => req(`/api/task/samples?${q({ task_id: taskId, workspace })}`);
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

const ANNOTATORS_PATH = "/api/annotators";
const COHORTS_PATH = "/api/cohorts";
const MEMBERS_PATH = "/api/members";

function managementWorkspace(workspace) {
  const value = String(workspace || "").trim();
  if (!value) throw localRequestError("workspace_required", 422, "必须选择 Scaffold 工作区");
  return value;
}

function managementPath(path, workspace) {
  return `${path}?${q({ workspace: managementWorkspace(workspace) })}`;
}

function jsonRequest(path, method, payload, options = {}) {
  const idempotencyKey = String(options?.idempotencyKey || "").trim();
  if (!idempotencyKey) {
    throw localRequestError("missing_idempotency_key", 422, API_ERROR_MESSAGES.missing_idempotency_key);
  }
  return req(path, {
    method,
    headers: { "Content-Type": "application/json", "Idempotency-Key": idempotencyKey },
    body: JSON.stringify(payload),
  });
}

const annotatorPath = (annotatorId) =>
  `${ANNOTATORS_PATH}/${encodeURIComponent(annotatorId)}`;

const cohortPath = (cohortId) =>
  `${COHORTS_PATH}/${encodeURIComponent(cohortId)}`;

const memberPath = (principalId) =>
  `${MEMBERS_PATH}/${encodeURIComponent(principalId)}`;

export const getAnnotators = (workspace) =>
  req(managementPath(ANNOTATORS_PATH, workspace));

export const getAnnotator = (annotatorId, workspace) =>
  req(managementPath(annotatorPath(annotatorId), workspace));

export const provisionAnnotator = (payload = {}, options = {}) =>
  jsonRequest(`${ANNOTATORS_PATH}/provision`, "POST", {
    workspace: managementWorkspace(payload.workspace),
    principal_id: payload.principal_id,
    personal_workspace_name: payload.personal_workspace_name,
    initial_password: payload.initial_password,
  }, options);

export const createAnnotator = provisionAnnotator;

export const bindAnnotator = (payload = {}, options = {}) =>
  jsonRequest(`${ANNOTATORS_PATH}/bind`, "POST", {
    workspace: managementWorkspace(payload.workspace),
    principal_id: payload.principal_id,
    argilla_user_id: payload.argilla_user_id,
    argilla_username: payload.argilla_username,
    personal_workspace_id: payload.personal_workspace_id,
  }, options);

export const verifyAnnotator = (annotatorId, workspace, options = {}) => {
  const resolvedWorkspace = managementWorkspace(workspace);
  return jsonRequest(`${annotatorPath(annotatorId)}/verify`, "POST", {
    workspace: resolvedWorkspace,
    annotator_id: annotatorId,
  }, options);
};

export const getCohorts = (workspace) =>
  req(managementPath(COHORTS_PATH, workspace));

export const getCohort = (cohortId, workspace) =>
  req(managementPath(cohortPath(cohortId), workspace));

export const createCohort = (payload = {}, options = {}) =>
  jsonRequest(COHORTS_PATH, "POST", {
    ...payload,
    workspace: managementWorkspace(payload.workspace),
  }, options);

export const updateCohort = (cohortId, payload = {}, options = {}) =>
  jsonRequest(cohortPath(cohortId), "PUT", {
    ...payload,
    workspace: managementWorkspace(payload.workspace),
    cohort_id: payload.cohort_id || cohortId,
  }, options);

export const replaceCohortMembers = (cohortId, payload = {}, options = {}) =>
  jsonRequest(`${cohortPath(cohortId)}/members`, "PUT", {
    ...payload,
    workspace: managementWorkspace(payload.workspace),
    cohort_id: payload.cohort_id || cohortId,
  }, options);

export const getMembers = (workspace) =>
  req(managementPath(MEMBERS_PATH, workspace));

export const createMemberInvitation = (payload = {}, options = {}) =>
  jsonRequest(`${MEMBERS_PATH}/invitations`, "POST", {
    ...payload,
    workspace: managementWorkspace(payload.workspace),
  }, options);

export const revokeMemberInvitation = (invitationId, payload = {}, options = {}) =>
  jsonRequest(`${MEMBERS_PATH}/invitations/${encodeURIComponent(invitationId)}/revoke`, "POST", {
    workspace: managementWorkspace(payload.workspace),
  }, options);

export const updateMemberRole = (principalId, payload = {}, options = {}) =>
  jsonRequest(`${memberPath(principalId)}/role`, "PUT", {
    ...payload,
    workspace: managementWorkspace(payload.workspace),
  }, options);

export const revokeMember = (principalId, payload = {}, options = {}) =>
  jsonRequest(`${memberPath(principalId)}/revoke`, "POST", {
    workspace: managementWorkspace(payload.workspace),
  }, options);

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
  workspace: scope?.workspace,
  task_id: scope?.taskId ?? scope?.task_id,
  revision_id: scope?.revisionId ?? scope?.revision_id,
  phase: scope?.phase,
  status: scope?.status,
  limit: scope?.limit,
  cursor: scope?.cursor,
});

const allocationScopedPath = (path, scope = {}) => {
  const query = allocationScopeQuery(scope);
  return query ? `${path}?${query}` : path;
};

const allocationPlanPath = (planId) =>
  `${ALLOCATION_PLANS_PATH}/${encodeURIComponent(planId)}`;

export const allocationPlanConfirmIdempotencyKey = (planId, fingerprint = "") =>
  `allocation-plan-confirm:${keyPart(planId, "plan")}:${keyPart(fingerprint, "preview")}`;

export const allocationPlanCreateIdempotencyKey = (workspace, taskId, fingerprint = "") =>
  `allocation-plan-create:${keyPart(workspace, "workspace")}:${keyPart(taskId, "task")}:${keyPart(fingerprint, "preview")}`;

export const allocationPlanUpdateIdempotencyKey = (planId, fingerprint = "") =>
  `allocation-plan-update:${keyPart(planId, "plan")}:${keyPart(fingerprint, "preview")}`;

export const getAllocationPlans = (scope = {}) =>
  req(allocationScopedPath(ALLOCATION_PLANS_PATH, scope));

export const listAllocationPlans = getAllocationPlans;

export const getAllocationPlan = (planId, scope = {}) =>
  req(allocationScopedPath(allocationPlanPath(planId), scope));

export const createAllocationPlan = (payload, options = {}) => {
  const idempotencyKey = String(options?.idempotencyKey || "").trim();
  if (!idempotencyKey) throw new Error("创建分配计划必须提供 Idempotency-Key");
  return req(ALLOCATION_PLANS_PATH, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Idempotency-Key": idempotencyKey },
    body: JSON.stringify(payload),
  });
};

export const updateAllocationPlan = (planId, payload, options = {}) => {
  const idempotencyKey = String(options?.idempotencyKey || "").trim();
  if (!idempotencyKey) throw new Error("更新分配计划必须提供 Idempotency-Key");
  return req(allocationPlanPath(planId), {
    method: "PUT",
    headers: { "Content-Type": "application/json", "Idempotency-Key": idempotencyKey },
    body: JSON.stringify(payload),
  });
};

export const previewAllocation = (payload) =>
  req("/api/allocation/preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

export const confirmAllocationPlan = (planId, payload = {}, options = {}) => {
  const fingerprint = payload.plan_fingerprint || "";
  const idempotencyKey = String(options?.idempotencyKey || "").trim()
    || allocationPlanConfirmIdempotencyKey(planId, fingerprint);
  const { idempotency_key: _ignoredIdempotencyKey, preview_fingerprint: _ignoredPreviewFingerprint, ...body } = payload;
  return req(`${allocationPlanPath(planId)}/confirm`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": idempotencyKey,
    },
    body: JSON.stringify({
      ...body,
      confirm: true,
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
