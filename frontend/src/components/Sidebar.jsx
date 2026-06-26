import React from "react";
import { Link } from "./../router.jsx";

const TASK_PAGES = [
  { key: "overview", label: "概览", short: "概", suffix: "" },
  { key: "imports", label: "数据导入", short: "导", suffix: "/imports" },
  { key: "samples", label: "样本管理", short: "样", suffix: "/samples" },
  { key: "annotations", label: "标注分发", short: "标", suffix: "/annotations" },
  { key: "jobs", label: "执行记录", short: "记", suffix: "/jobs" },
  { key: "gold", label: "训练集版本", short: "集", suffix: "/gold" },
  { key: "models", label: "模型管理", short: "模", suffix: "/models" },
];

export default function Sidebar({ tasks, activeTaskId, activePage, collapsed, onToggle }) {
  return (
    <aside className={collapsed ? "sidebar is-collapsed" : "sidebar"}>
      <div className="sidebar-head">
        <div className="brand-mark" title="实验控制台">实</div>
        <div className="brand-copy">
          <h1>实验控制台</h1>
          <div className="sub">标注数据流管理</div>
        </div>
        <button className="sidebar-toggle" type="button" onClick={onToggle} title={collapsed ? "展开侧栏" : "收起侧栏"}>
          {collapsed ? "›" : "‹"}
        </button>
      </div>
      <Link to="/" className={!activeTaskId ? "nav-item active" : "nav-item"} title="全部任务">
        <span className="nav-short">全</span>
        <span className="nav-label">全部任务</span>
      </Link>
      {activeTaskId && (
        <>
          <div className="nav-group">{activeTaskId}</div>
          {TASK_PAGES.map((p) => (
            <Link
              key={p.key}
              to={`/task/${encodeURIComponent(activeTaskId)}${p.suffix}`}
              className={activePage === p.key ? "nav-item active" : "nav-item"}
              title={p.label}
            >
              <span className="nav-short">{p.short}</span>
              <span className="nav-label">{p.label}</span>
            </Link>
          ))}
        </>
      )}
      {!activeTaskId && tasks && tasks.length > 0 && (
        <>
          <div className="nav-group">任务列表</div>
          {tasks.map((t) => (
            <Link key={t.path} to={`/task/${encodeURIComponent(t.task_id)}`} className="nav-item" title={t.task_id || "(无效)"}>
              <span className="nav-short">任</span>
              <span className="nav-label">{t.task_id || "(无效)"}</span>
            </Link>
          ))}
        </>
      )}
    </aside>
  );
}
