import React from "react";
import { Link } from "./../router.jsx";

const TASK_PAGES = [
  { key: "overview", label: "概览", suffix: "" },
  { key: "imports", label: "数据导入", suffix: "/imports" },
  { key: "samples", label: "样本管理", suffix: "/samples" },
  { key: "annotations", label: "标注分发", suffix: "/annotations" },
  { key: "jobs", label: "执行记录", suffix: "/jobs" },
  { key: "gold", label: "训练集版本", suffix: "/gold" },
  { key: "models", label: "模型管理", suffix: "/models" },
];

export default function Sidebar({ tasks, activeTaskId, activePage }) {
  return (
    <aside className="sidebar">
      <h1>实验控制台</h1>
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
              className={activePage === p.key ? "nav-item active" : "nav-item"}
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
