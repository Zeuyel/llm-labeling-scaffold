import React, { useEffect, useState, useCallback } from "react";
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

const ROUTES = [
  { pattern: "/", page: "tasks" },
  { pattern: "/settings", page: "settings" },
  { pattern: "/task/:id", page: "overview" },
  { pattern: "/task/:id/canvas", page: "canvas" },
  { pattern: "/task/:id/imports", page: "imports" },
  { pattern: "/task/:id/samples", page: "samples" },
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

function Shell() {
  const { path } = useRouter();
  const [tasks, setTasks] = useState([]);
  const [settings, setSettings] = useState(DEFAULT_SETTINGS);
  const [settingsReady, setSettingsReady] = useState(false);
  const [settingsError, setSettingsError] = useState("");
  const [err, setErr] = useState("");
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => localStorage.getItem("lls.sidebarCollapsed") === "1");

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
  const activeTaskId = matched.params.id || null;
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
      />
      <div className="content">
        {err && <div className="error">{err} <button className="btn btn-sm" onClick={() => setErr("")}>关闭</button></div>}
        {page}
      </div>
    </div>
  );
}

export default function App() {
  return (
    <RouterProvider>
      <Shell />
    </RouterProvider>
  );
}
