import React from "react";
import { Link } from "./../router.jsx";

const TASK_PAGES = [
  { key: "overview", label: "概览", short: "概", suffix: "" },
  { key: "canvas", label: "流程画布", short: "流", suffix: "/canvas" },
  { key: "imports", label: "任务输入", short: "入", suffix: "/imports" },
  { key: "samples", label: "样本管理", short: "样", suffix: "/samples" },
  { key: "allocation-plans", label: "分配计划", short: "分", suffix: "/allocation-plans" },
  { key: "annotations", label: "标注分发", short: "标", suffix: "/annotations" },
  { key: "jobs", label: "执行记录", short: "记", suffix: "/jobs" },
  { key: "gold", label: "训练集版本", short: "集", suffix: "/gold" },
  { key: "models", label: "模型管理", short: "模", suffix: "/models" },
];

export default function Sidebar({ tasks, activeTaskId, activePage, collapsed, onToggle, user, onLogout }) {
  return (
    <aside className={collapsed ? "sidebar is-collapsed" : "sidebar"}>
      <div className="sidebar-head">
        <div className="brand-mark" title="标注控制台">标</div>
        <div className="brand-copy">
          <h1>标注控制台</h1>
          <div className="sub">任务与数据流管理</div>
        </div>
        <button className="sidebar-toggle" type="button" onClick={onToggle} title={collapsed ? "展开侧栏" : "收起侧栏"}>
          {collapsed ? "›" : "‹"}
        </button>
      </div>
      <Link to="/settings" className={activePage === "settings" ? "nav-item active" : "nav-item"} title="系统设置">
        <span className="nav-short">设</span>
        <span className="nav-label">系统设置</span>
      </Link>
      <Link to="/data-assets" className={activePage === "data-assets" ? "nav-item active" : "nav-item"} title="数据资产">
        <span className="nav-short">资</span>
        <span className="nav-label">数据资产</span>
      </Link>
      <Link to="/" className={!activeTaskId && activePage === "tasks" ? "nav-item active" : "nav-item"} title="全部任务">
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
      <div className="sidebar-account">
        <div className="sidebar-account-name" title={user?.display_name || user?.email || "当前用户"}>
          {user?.display_name || user?.email || "当前用户"}
        </div>
        <button className="sidebar-logout" type="button" onClick={onLogout} title="退出登录">
          退出登录
        </button>
      </div>
    </aside>
  );
}
