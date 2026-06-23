async function req(path, opts = {}) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText}: ${text.slice(0, 160)}`);
  }
  return res.json();
}

const q = (obj) =>
  Object.entries(obj)
    .filter(([, v]) => v !== undefined && v !== null && v !== "")
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`)
    .join("&");

// core objects: task
export const getTasks = () => req("/api/tasks");
export const getTaskRuns = (taskId) => req(`/api/task/runs?${q({ task_id: taskId })}`);
export const getTaskSamples = (taskId) => req(`/api/task/samples?${q({ task_id: taskId })}`);
export const getTaskModels = (taskId) => req(`/api/task/models?${q({ task_id: taskId })}`);
export const getTaskGoldVersions = (taskId) => req(`/api/task/gold_versions?${q({ task_id: taskId })}`);
export const getDecisions = (taskId, run) => req(`/api/task/decisions?${q({ task_id: taskId, run })}`);
export const getJobs = (taskId) => req(`/api/jobs?${q({ task_id: taskId })}`);

// legacy run-centric reads (still used by pools view)
export const getRuns = () => req("/api/runs");
export const getRun = (task, run) => req(`/api/run?${q({ task, run })}`);
export const getRows = (task, run, kind) => req(`/api/rows?${q({ task, run, kind })}`);
export const exportUrl = (task, run, kind) => `/api/export?${q({ task, run, kind })}`;

// core object: job (start any pipeline action)
export const startAction = (taskPath, action, params) =>
  req("/api/action", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ task: taskPath, action, params }),
  });

// core object: decision
export const adjudicate = (task, run, payload) =>
  req(`/api/adjudicate?${q({ task, run })}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

// artifact import
export const importJsonl = (task, name, text) =>
  req(`/api/import?${q({ task, name })}`, {
    method: "POST",
    headers: { "Content-Type": "application/x-ndjson" },
    body: text,
  });
