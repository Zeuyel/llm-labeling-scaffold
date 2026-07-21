import React, { useEffect, useMemo, useState, useCallback } from "react";
import * as api from "./api.js";
import { RouterProvider, useRouter, matchRoute } from "./router.jsx";
import Sidebar from "./components/Sidebar.jsx";
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
import TaskArchivePage from "./pages/TaskArchivePage.jsx";
import LoginPage from "./pages/LoginPage.jsx";
import DataAssetsPage from "./pages/DataAssetsPage.jsx";
import AllocationPlansPage from "./pages/AllocationPlansPage.jsx";
import AnnotatorsPage from "./pages/AnnotatorsPage.jsx";
import CohortsPage from "./pages/CohortsPage.jsx";
import MembersPage from "./pages/MembersPage.jsx";

const ROUTES = [
  { pattern: "/", page: "tasks" },
  { pattern: "/settings", page: "settings" },
  { pattern: "/members", page: "members" },
  { pattern: "/annotators/:id", page: "annotators" },
  { pattern: "/annotators", page: "annotators" },
  { pattern: "/cohorts/:id", page: "cohorts" },
  { pattern: "/cohorts", page: "cohorts" },
  { pattern: "/data-assets", page: "data-assets" },
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
  { pattern: "/task/:id/archive", page: "archive" },
];

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
  const [settings, setSettings] = useState(DEFAULT_SETTINGS);
  const [settingsReady, setSettingsReady] = useState(false);
  const [settingsError, setSettingsError] = useState("");
  const [err, setErr] = useState("");
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => localStorage.getItem("lls.sidebarCollapsed") === "1");
  const [managementWorkspace, setManagementWorkspace] = useState("");

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

  const loadTasks = useCallback(() =>
    api.getTasks().then((d) => setTasks(d.tasks || [])).catch((e) => setErr(String(e))),
  []);
  const syncTasks = useCallback(() =>
    api.syncTasks().then((d) => setTasks(d.tasks || [])).catch((e) => setErr(String(e))),
  []);
  useEffect(() => { loadTasks(); }, [loadTasks]);
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
  const taskPages = new Set(["overview", "canvas", "imports", "samples", "allocation-plans", "annotations", "jobs", "gold", "models", "archive"]);
  const activeTaskId = taskPages.has(matched.page) ? matched.params.id || null : null;
  const taskOf = (id) => tasks.find((t) => t.task_id === id) || null;

  async function handleSettingsSaved(next) {
    setSettings({ ...DEFAULT_SETTINGS, ...(next || {}) });
    setSettingsError("");
    setSettingsReady(true);
    await loadTasks();
  }

  const common = { onError: setErr };
  const settingsAvailable = settingsReady && !settingsError;
  let page = null;
  if (matched.page === "tasks") page = (
    <TasksPage
      tasks={tasks}
      onReload={loadTasks}
      onSync={syncTasks}
      allowDataLakeOverrides={Boolean(settings.allow_data_lake_overrides)}
      taskSource={settings.task_source || "local"}
      taskRegistryUri={settings.task_registry_uri || ""}
      {...common}
    />
  );
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
  else if (matched.page === "overview") page = <TaskOverviewPage task={taskOf(activeTaskId)} taskId={activeTaskId} {...common} />;
  else if (matched.page === "canvas") page = <TaskCanvasPage task={taskOf(activeTaskId)} taskId={activeTaskId} {...common} />;
  else if (matched.page === "imports") page = (
    <ImportsPage
      task={taskOf(activeTaskId)}
      taskId={activeTaskId}
      taskSource={settingsAvailable ? (settings.task_source || "") : ""}
      allowManualImports={settingsAvailable && Boolean(settings.allow_manual_imports || settings.manual_imports_enabled)}
      settingsReady={settingsReady}
      settingsError={settingsError}
      {...common}
    />
  );
  else if (matched.page === "samples") page = <SamplesPage task={taskOf(activeTaskId)} taskId={activeTaskId} {...common} />;
  else if (matched.page === "allocation-plans") page = <AllocationPlansPage task={taskOf(activeTaskId)} taskId={activeTaskId} {...common} />;
  else if (matched.page === "annotations") page = <RunsPage task={taskOf(activeTaskId)} taskId={activeTaskId} {...common} />;
  else if (matched.page === "jobs") page = <JobsPage task={taskOf(activeTaskId)} taskId={activeTaskId} {...common} />;
  else if (matched.page === "gold") page = <GoldPage task={taskOf(activeTaskId)} taskId={activeTaskId} {...common} />;
  else if (matched.page === "models") page = <ModelsPage task={taskOf(activeTaskId)} taskId={activeTaskId} {...common} />;
  else if (matched.page === "archive") page = <TaskArchivePage taskId={activeTaskId} onReloadTasks={loadTasks} {...common} />;

  function toggleSidebar() {
    setSidebarCollapsed((value) => {
      const next = !value;
      localStorage.setItem("lls.sidebarCollapsed", next ? "1" : "0");
      return next;
    });
  }

  return (
    <div className={sidebarCollapsed ? "app-shell is-sidebar-collapsed" : "app-shell"}>
      <Sidebar
        tasks={tasks}
        activeTaskId={activeTaskId}
        activePage={matched.page}
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
