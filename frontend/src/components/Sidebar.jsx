import React from "react";
import { Link } from "./../router.jsx";

const TASK_PAGES = [
  { key: "overview", label: "概览", suffix: "" },
  { key: "samples", label: "采样 / Artifact", suffix: "/samples" },
  { key: "runs", label: "标注运行 Run", suffix: "/runs" },
  { key: "jobs", label: "任务 Job", suffix: "/jobs" },
  { key: "gold", label: "Gold 版本", suffix: "/gold" },
  { key: "models", label: "模型版本", suffix: "/models" },
];

export default function Sidebar({ tasks, activeTaskId, activePage }) {
  return (
    <aside className="sidebar">
      <h1>Pipeline 控制台</h1>
      <div className="sub">标注数据流管理</div>
      <Link to="/" className={!activeTaskId ? "nav-item active" : "nav-item"}>
        全部任务
      </Link>
      {activeTaskId && (
        <>
          <div className="nav-group">{activeTaskId}</div>
          {TASK_PAGES.map((p) => (
            <Link
              key={p.key}
              to={`/task/${encodeURIComponent(activeTaskId)}${p.suffix}`}
              className={activePage === p.key || (p.key === "runs" && activePage === "runDetail") ? "nav-item active" : "nav-item"}
            >
              {p.label}
            </Link>
          ))}
        </>
      )}
      {!activeTaskId && tasks && tasks.length > 0 && (
        <>
          <div className="nav-group">任务列表</div>
          {tasks.map((t) => (
            <Link key={t.path} to={`/task/${encodeURIComponent(t.task_id)}`} className="nav-item">
              {t.task_id || "(无效)"}
            </Link>
          ))}
        </>
      )}
    </aside>
  );
}
