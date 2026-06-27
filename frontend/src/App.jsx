import React, { useEffect, useState, useCallback } from "react";
import * as api from "./api.js";
import { RouterProvider, useRouter, matchRoute } from "./router.jsx";
import Sidebar from "./components/Sidebar.jsx";
import TasksPage from "./pages/TasksPage.jsx";
import TaskOverviewPage from "./pages/TaskOverviewPage.jsx";
import ImportsPage from "./pages/ImportsPage.jsx";
import SamplesPage from "./pages/SamplesPage.jsx";
import RunsPage from "./pages/RunsPage.jsx";
import JobsPage from "./pages/JobsPage.jsx";
import GoldPage from "./pages/GoldPage.jsx";
import ModelsPage from "./pages/ModelsPage.jsx";
import SettingsPage from "./pages/SettingsPage.jsx";

const ROUTES = [
  { pattern: "/", page: "tasks" },
  { pattern: "/settings", page: "settings" },
  { pattern: "/task/:id", page: "overview" },
  { pattern: "/task/:id/imports", page: "imports" },
  { pattern: "/task/:id/samples", page: "samples" },
  { pattern: "/task/:id/annotations", page: "annotations" },
  { pattern: "/task/:id/runs", page: "annotations" },
  { pattern: "/task/:id/jobs", page: "jobs" },
  { pattern: "/task/:id/gold", page: "gold" },
  { pattern: "/task/:id/models", page: "models" },
];

const DEFAULT_SETTINGS = {
  allow_data_lake_overrides: false,
  data_lake_r2_prefix: "",
  rclone_config_path: "",
  task_registry_uri: "",
  task_source: "local",
};

function Shell() {
  const { path } = useRouter();
  const [tasks, setTasks] = useState([]);
  const [settings, setSettings] = useState(DEFAULT_SETTINGS);
  const [err, setErr] = useState("");
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => localStorage.getItem("lls.sidebarCollapsed") === "1");

  const loadTasks = useCallback(() =>
    api.getTasks().then((d) => setTasks(d.tasks || [])).catch((e) => setErr(String(e))),
  []);
  useEffect(() => { loadTasks(); }, [loadTasks]);
  const loadSettings = useCallback(() =>
    api.getSettings()
      .then((d) => setSettings({ ...DEFAULT_SETTINGS, ...(d || {}) }))
      .catch(() => setSettings(DEFAULT_SETTINGS)),
  []);
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
    await loadTasks();
  }

  const common = { onError: setErr };
  let page = null;
  if (matched.page === "tasks") page = (
    <TasksPage
      tasks={tasks}
      onReload={loadTasks}
      allowDataLakeOverrides={Boolean(settings.allow_data_lake_overrides)}
      taskSource={settings.task_source || "local"}
      taskRegistryUri={settings.task_registry_uri || ""}
      {...common}
    />
  );
  else if (matched.page === "settings") page = <SettingsPage settings={settings} onSettingsSaved={handleSettingsSaved} {...common} />;
  else if (matched.page === "overview") page = <TaskOverviewPage task={taskOf(activeTaskId)} taskId={activeTaskId} {...common} />;
  else if (matched.page === "imports") page = <ImportsPage task={taskOf(activeTaskId)} taskId={activeTaskId} {...common} />;
  else if (matched.page === "samples") page = <SamplesPage task={taskOf(activeTaskId)} taskId={activeTaskId} {...common} />;
  else if (matched.page === "annotations") page = <RunsPage task={taskOf(activeTaskId)} taskId={activeTaskId} {...common} />;
  else if (matched.page === "jobs") page = <JobsPage task={taskOf(activeTaskId)} taskId={activeTaskId} {...common} />;
  else if (matched.page === "gold") page = <GoldPage task={taskOf(activeTaskId)} taskId={activeTaskId} {...common} />;
  else if (matched.page === "models") page = <ModelsPage task={taskOf(activeTaskId)} taskId={activeTaskId} {...common} />;

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
