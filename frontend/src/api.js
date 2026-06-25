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

export const getTasks = () => req("/api/tasks");
export const getImports = (taskId) => req(`/api/task/imports?${q({ task_id: taskId })}`);
export const getTaskRuns = (taskId) => req(`/api/task/runs?${q({ task_id: taskId })}`);
export const getTaskSamples = (taskId) => req(`/api/task/samples?${q({ task_id: taskId })}`);
export const getTaskModels = (taskId) => req(`/api/task/models?${q({ task_id: taskId })}`);
export const getTaskGoldVersions = (taskId) => req(`/api/task/gold_versions?${q({ task_id: taskId })}`);
export const getAnnotationJobs = (taskId) => req(`/api/task/annotation_jobs?${q({ task_id: taskId })}`);
export const getDecisionArtifacts = (taskId) => req(`/api/task/decision_artifacts?${q({ task_id: taskId })}`);
export const getJobs = (taskId) => req(`/api/jobs?${q({ task_id: taskId })}`);

export const startAction = (taskPath, action, params) =>
  req("/api/action", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ task: taskPath, action, params }),
  }).then((data) => data.job || data);

export async function waitForJob(taskId, jobId, attempts = 30) {
  for (let i = 0; i < attempts; i += 1) {
    const data = await getJobs(taskId);
    const job = (data.jobs || []).find((item) => item.id === jobId);
    if (job && !["pending", "running"].includes(job.status)) return job;
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  return null;
}

export const importJsonl = (task, name, text) =>
  req(`/api/import?${q({ task, name })}`, {
    method: "POST",
    headers: { "Content-Type": "application/x-ndjson" },
    body: text,
  });
