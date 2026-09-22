import React, { useEffect, useMemo, useRef, useState, useCallback } from "react";
import * as api from "./api.js";
import { RouterProvider, useRouter, matchRoute } from "./router.jsx";
import Sidebar from "./components/Sidebar.jsx";
import TaskLayout, { LegacyTaskArchiveRedirect } from "./components/TaskLayout.jsx";
import TasksPage from "./pages/TasksPage.jsx";
import TaskOverviewPage from "./pages/TaskOverviewPage.jsx";
import TaskCanvasPage from "./pages/TaskCanvasPage.jsx";
import ImportsPage from "./pages/ImportsPage.jsx";
import SamplesPage from "./pages/SamplesPage.jsx";
import RunsPage from "./pages/RunsPage.jsx";
import JobsPage from "./pages/JobsPage.jsx";
import GoldPage from "./pages/GoldPage.jsx";
import ModelsPage from "./pages/ModelsPage.jsx";
import SettingsPage from "./pages/SettingsPage.jsx";
import LoginPage from "./pages/LoginPage.jsx";
import DataAssetsPage from "./pages/DataAssetsPage.jsx";
import AllocationPlansPage from "./pages/AllocationPlansPage.jsx";
import AnnotatorsPage from "./pages/AnnotatorsPage.jsx";
import CohortsPage from "./pages/CohortsPage.jsx";
import MembersPage from "./pages/MembersPage.jsx";

const ROUTES = [
  { pattern: "/", page: "tasks" },
  { pattern: "/tasks/new", page: "task-create" },
  { pattern: "/settings", page: "settings" },
  { pattern: "/members", page: "members" },
  { pattern: "/annotators/:id", page: "annotators" },
  { pattern: "/annotators", page: "annotators" },
  { pattern: "/cohorts/:id", page: "cohorts" },
  { pattern: "/cohorts", page: "cohorts" },
  { pattern: "/data-assets", page: "data-assets" },
  { pattern: "/task/:id/configuration", page: "configuration" },
  { pattern: "/task/:id/archive", page: "archive-redirect" },
  { pattern: "/task/:id", page: "overview" },
  { pattern: "/task/:id/canvas", page: "canvas" },
  { pattern: "/task/:id/imports", page: "imports" },
  { pattern: "/task/:id/samples", page: "samples" },
  { pattern: "/task/:id/allocation-plans", page: "allocation-plans" },
  { pattern: "/task/:id/annotations", page: "annotations" },
  { pattern: "/task/:id/runs", page: "annotations" },
  { pattern: "/task/:id/jobs", page: "jobs" },
  { pattern: "/task/:id/gold", page: "gold" },
  { pattern: "/task/:id/models", page: "models" },
];

const TASK_SUMMARY_PAGES = new Set([
  "overview",
  "canvas",
  "imports",
  "samples",
  "allocation-plans",
  "annotations",
  "jobs",
  "gold",
  "models",
]);

const TASK_LAYOUT_PAGES = new Set([...TASK_SUMMARY_PAGES, "configuration"]);
const TASK_ROUTE_PAGES = new Set([...TASK_LAYOUT_PAGES, "archive-redirect"]);

const TASKS_ENTRY_PAGES = new Set(["tasks", "task-create", "configuration"]);

const DEFAULT_SETTINGS = {
  allow_data_lake_overrides: false,
  allow_manual_imports: false,
  data_lake_r2_prefix: "",
  rclone_config_path: "",
  task_registry_uri: "",
  task_source: "r2",
};

function Shell({ session, onLogout }) {
  const { path } = useRouter();
  const [tasks, setTasks] = useState([]);
  const [tasksNextCursor, setTasksNextCursor] = useState("");
  const [tasksLoading, setTasksLoading] = useState(false);
  const [tasksError, setTasksError] = useState("");
  const [settings, setSettings] = useState(DEFAULT_SETTINGS);
  const [settingsReady, setSettingsReady] = useState(false);
  const [settingsError, setSettingsError] = useState("");
  const [err, setErr] = useState("");
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => localStorage.getItem("lls.sidebarCollapsed") === "1");
  const [managementWorkspace, setManagementWorkspace] = useState("");
  const [taskSummary, setTaskSummary] = useState(null);
  const [taskSummaryKey, setTaskSummaryKey] = useState("");
  const [taskSummaryLoading, setTaskSummaryLoading] = useState(false);
  const [taskSummaryError, setTaskSummaryError] = useState("");
  const tasksRequestSeq = useRef(0);
  const taskSummaryRequestSeq = useRef(0);

  const managementWorkspaces = useMemo(() => {
    const values = session?.authorization?.workspaces;
    if (!Array.isArray(values)) return [];
    return values
      .filter((item) => item && typeof item.slug === "string" && item.slug.trim())
      .map((item) => ({
        ...item,
        slug: item.slug.trim(),
        workspace_capabilities: Array.isArray(item.workspace_capabilities) ? item.workspace_capabilities : [],
      }));
  }, [session]);

  useEffect(() => {
    setManagementWorkspace((current) => {
      if (current && managementWorkspaces.some((item) => item.slug === current)) return current;
      return managementWorkspaces.length === 1 ? managementWorkspaces[0].slug : "";
    });
  }, [managementWorkspaces]);

  const selectedManagementWorkspace = managementWorkspaces.find((item) => item.slug === managementWorkspace);
  const canManagePeople = Boolean(
    selectedManagementWorkspace
    && selectedManagementWorkspace.workspace_capabilities.includes("workspace:manage"),
  );

  const loadTasks = useCallback(async (options = {}) => {
    const requestSeq = ++tasksRequestSeq.current;
    const normalizedOptions = typeof options === "string" ? { workspace: options } : (options || {});
    const workspace = String(normalizedOptions.workspace ?? managementWorkspace).trim();
    const requestOptions = {
      ...normalizedOptions,
      workspace,
      include_archived: normalizedOptions.include_archived ?? normalizedOptions.includeArchived ?? false,
      limit: normalizedOptions.limit ?? 100,
    };
    setTasksLoading(true);
    setTasksError("");
    setTasks([]);
    setTasksNextCursor("");
    try {
      if (!workspace) {
        const error = new Error("必须选择 Scaffold 工作区");
        error.code = "workspace_required";
        throw error;
      }
      const data = await api.getTasks(workspace, requestOptions);
      if (requestSeq !== tasksRequestSeq.current) return data;
      setTasks(data.tasks || []);
      setTasksNextCursor(data.next_cursor || "");
      return data;
    } catch (error) {
      if (requestSeq === tasksRequestSeq.current) setTasksError(String(error));
      throw error;
    } finally {
      if (requestSeq === tasksRequestSeq.current) setTasksLoading(false);
    }
  }, [managementWorkspace]);
  const syncTasks = useCallback(async () => {
    try {
      const data = await api.syncTasks();
      setTasks(data.tasks || []);
      setTasksNextCursor(data.next_cursor || "");
      setTasksError("");
      return data;
    } catch (error) {
      const message = String(error);
      setTasksError(message);
      setErr(message);
      throw error;
    }
  }, []);
  useEffect(() => {
    loadTasks().catch(() => {});
  }, [loadTasks]);
  const handleSettingsLoadError = useCallback((error) => {
    const message = `设置读取失败：${String(error)}`;
    setSettings(DEFAULT_SETTINGS);
    setSettingsError(message);
    setSettingsReady(true);
    setErr(message);
  }, []);
  const loadSettings = useCallback(() => {
    setSettingsReady(false);
    return api.getSettings()
      .then((d) => {
        setSettings({ ...DEFAULT_SETTINGS, ...(d || {}) });
        setSettingsError("");
      })
      .catch(handleSettingsLoadError)
      .finally(() => setSettingsReady(true));
  }, [handleSettingsLoadError]);
  useEffect(() => {
    loadSettings();
  }, [loadSettings]);

  let matched = { page: "tasks", params: {} };
  for (const r of ROUTES) {
    const params = matchRoute(r.pattern, path);
    if (params) { matched = { page: r.page, params }; break; }
  }
  const activeTaskId = TASK_ROUTE_PAGES.has(matched.page) ? matched.params.id || null : null;
  const requiresTaskSummary = TASK_SUMMARY_PAGES.has(matched.page);
  const currentTaskSummaryKey = `${managementWorkspace}\u0000${activeTaskId || ""}`;
  const taskSummaryMatches = Boolean(activeTaskId) && taskSummaryKey === currentTaskSummaryKey;
  const resolvedTaskSummary = taskSummaryMatches ? taskSummary : null;
  const activeTask = resolvedTaskSummary;

  const loadTaskSummary = useCallback(async () => {
    const requestSeq = ++taskSummaryRequestSeq.current;
    const taskId = String(activeTaskId || "").trim();
    const workspace = managementWorkspace.trim();
    const summaryKey = `${workspace}\u0000${taskId}`;
    setTaskSummaryKey(summaryKey);
    setTaskSummary(null);
    setTaskSummaryError("");
    if (!taskId || !requiresTaskSummary) {
      setTaskSummaryLoading(false);
      return null;
    }
    setTaskSummaryLoading(true);
    try {
      if (!workspace) {
        const error = new Error("任务详情读取被阻塞：必须先选择 Scaffold 工作区。");
        error.code = "workspace_required";
        throw error;
      }
      if (typeof api.getTaskSummary !== "function") {
        const error = new Error("任务详情接口尚未接入：需要 api.getTaskSummary(taskId, workspace)。");
        error.code = "task_detail_helper_unavailable";
        throw error;
      }
      const data = await api.getTaskSummary(taskId, workspace);
      if (requestSeq !== taskSummaryRequestSeq.current) return data;
      if (!data?.task || typeof data.task !== "object" || Array.isArray(data.task)) {
        const error = new Error("任务详情响应格式无效：缺少 task summary。");
        error.code = "task_summary_invalid_response";
        throw error;
      }
      const summary = data.task;
      setTaskSummary(summary);
      return summary;
    } catch (error) {
      if (requestSeq === taskSummaryRequestSeq.current) setTaskSummaryError(String(error));
      throw error;
    } finally {
      if (requestSeq === taskSummaryRequestSeq.current) setTaskSummaryLoading(false);
    }
  }, [activeTaskId, managementWorkspace, requiresTaskSummary]);

  useEffect(() => {
    loadTaskSummary().catch(() => {});
  }, [loadTaskSummary]);

  async function handleSettingsSaved(next) {
    setSettings({ ...DEFAULT_SETTINGS, ...(next || {}) });
    setSettingsError("");
    setSettingsReady(true);
    await loadTasks().catch(() => {});
  }

  const common = { onError: setErr };
  const settingsAvailable = settingsReady && !settingsError;
  const renderTasksPage = ({ createMode = false, detailTaskId = "" } = {}) => (
    <TasksPage
      tasks={tasks}
      tasksLoading={tasksLoading}
      tasksError={tasksError}
      tasksNextCursor={tasksNextCursor}
      createMode={createMode}
      detailTaskId={detailTaskId}
      workspace={managementWorkspace}
      workspaces={managementWorkspaces}
      onWorkspaceChange={setManagementWorkspace}
      onReload={loadTasks}
      onSync={syncTasks}
      settingsReady={settingsReady}
      settingsError={settingsError}
      allowDataLakeOverrides={settingsAvailable && Boolean(settings.allow_data_lake_overrides)}
      taskSource={settingsAvailable ? (settings.task_source || "") : ""}
      taskRegistryUri={settingsAvailable ? (settings.task_registry_uri || "") : ""}
      {...common}
    />
  );

  let page = null;
  if (TASKS_ENTRY_PAGES.has(matched.page)) page = renderTasksPage({
    createMode: matched.page === "task-create",
    detailTaskId: matched.page === "configuration" ? activeTaskId : "",
  });
  else if (matched.page === "settings") page = (
    <SettingsPage
      settings={settings}
      onSettingsSaved={handleSettingsSaved}
      onSettingsLoadError={handleSettingsLoadError}
      {...common}
    />
  );
  else if (matched.page === "members") page = (
    <MembersPage
      workspace={managementWorkspace}
      workspaces={managementWorkspaces}
      canManage={canManagePeople}
      onWorkspaceChange={setManagementWorkspace}
      {...common}
    />
  );
  else if (matched.page === "annotators") page = (
    <AnnotatorsPage
      workspace={managementWorkspace}
      workspaces={managementWorkspaces}
      canManage={canManagePeople}
      onWorkspaceChange={setManagementWorkspace}
      detailId={matched.params.id || ""}
      {...common}
    />
  );
  else if (matched.page === "cohorts") page = (
    <CohortsPage
      workspace={managementWorkspace}
      workspaces={managementWorkspaces}
      canManage={canManagePeople}
      onWorkspaceChange={setManagementWorkspace}
      detailId={matched.params.id || ""}
      {...common}
    />
  );
  else if (matched.page === "data-assets") page = <DataAssetsPage {...common} />;
  else if (matched.page === "overview") page = <TaskOverviewPage task={activeTask} taskId={activeTaskId} {...common} />;
  else if (matched.page === "canvas") page = <TaskCanvasPage task={activeTask} taskId={activeTaskId} {...common} />;
  else if (matched.page === "imports") page = (
    <ImportsPage
      task={activeTask}
      taskId={activeTaskId}
      taskSource={settingsAvailable ? (settings.task_source || "") : ""}
      allowManualImports={settingsAvailable && Boolean(settings.allow_manual_imports || settings.manual_imports_enabled)}
      settingsReady={settingsReady}
      settingsError={settingsError}
      {...common}
    />
  );
  else if (matched.page === "samples") page = <SamplesPage task={activeTask} taskId={activeTaskId} {...common} />;
  else if (matched.page === "allocation-plans") page = <AllocationPlansPage task={activeTask} taskId={activeTaskId} {...common} />;
  else if (matched.page === "annotations") page = <RunsPage task={activeTask} taskId={activeTaskId} {...common} />;
  else if (matched.page === "jobs") page = <JobsPage task={activeTask} taskId={activeTaskId} {...common} />;
  else if (matched.page === "gold") page = <GoldPage task={activeTask} taskId={activeTaskId} {...common} />;
  else if (matched.page === "models") page = <ModelsPage task={activeTask} taskId={activeTaskId} {...common} />;
  else if (matched.page === "archive-redirect") page = <LegacyTaskArchiveRedirect taskId={activeTaskId} />;

  function toggleSidebar() {
    setSidebarCollapsed((value) => {
      const next = !value;
      localStorage.setItem("lls.sidebarCollapsed", next ? "1" : "0");
      return next;
    });
  }

  if (activeTaskId && TASK_LAYOUT_PAGES.has(matched.page)) {
    page = (
      <TaskLayout
        taskId={activeTaskId}
        taskSummary={resolvedTaskSummary}
        taskSummaryRequired={requiresTaskSummary}
        activePage={matched.page}
        taskSummaryLoading={requiresTaskSummary && (taskSummaryLoading || !taskSummaryMatches)}
        taskSummaryError={taskSummaryError}
        onRetryTaskSummary={() => loadTaskSummary().catch(() => {})}
      >
        {page}
      </TaskLayout>
    );
  }

  const globalPage = matched.page === "task-create" || TASK_ROUTE_PAGES.has(matched.page)
    ? "tasks"
    : matched.page;

  return (
    <div className={sidebarCollapsed ? "app-shell is-sidebar-collapsed" : "app-shell"}>
      <Sidebar
        activePage={globalPage}
        collapsed={sidebarCollapsed}
        onToggle={toggleSidebar}
        user={session?.user}
        onLogout={onLogout}
      />
      <div className="content">
        {err && <div className="error">{err} <button className="btn btn-sm" onClick={() => setErr("")}>关闭</button></div>}
        {page}
      </div>
    </div>
  );
}

export default function App() {
  const [session, setSession] = useState(null);
  const [authReady, setAuthReady] = useState(false);
  const [authError, setAuthError] = useState("");

  useEffect(() => {
    let active = true;
    const handleUnauthorized = () => {
      api.logout();
      if (!active) return;
      setSession(null);
      setAuthError("登录已失效，请重新登录");
    };
    window.addEventListener("lls:unauthorized", handleUnauthorized);
    api.getSession()
      .then((data) => {
        if (active) {
          setSession(data);
          setAuthError("");
        }
      })
      .catch(() => {
        if (active) setSession(null);
      })
      .finally(() => {
        if (active) setAuthReady(true);
      });
    return () => {
      active = false;
      window.removeEventListener("lls:unauthorized", handleUnauthorized);
    };
  }, []);

  async function handleLogin(username, password) {
    const nextSession = await api.login(username, password);
    setSession(nextSession);
    setAuthError("");
  }

  function handleLogout() {
    api.logout();
    setSession(null);
    setAuthError("");
  }

  if (!authReady) {
    return <main className="auth-page"><div className="auth-loading">正在检查登录状态...</div></main>;
  }
  if (!session) {
    return <LoginPage onLogin={handleLogin} error={authError} />;
  }
  return (
    <RouterProvider>
      <Shell session={session} onLogout={handleLogout} />
    </RouterProvider>
  );
}
