import React from "react";
import {
  Database,
  LayoutList,
  LogOut,
  PanelLeftClose,
  PanelLeftOpen,
  Settings,
  UserRound,
  Users,
  UsersRound,
  Workflow,
} from "lucide-react";
import { Link } from "../router.jsx";
import "./navigation.css";

const GLOBAL_NAV_ITEMS = [
  { key: "tasks", label: "全部任务", to: "/", Icon: LayoutList },
  { key: "data-assets", label: "数据资产", to: "/data-assets", Icon: Database },
  { key: "members", label: "成员管理", to: "/members", Icon: Users },
  { key: "annotators", label: "标注人员", to: "/annotators", Icon: UserRound },
  { key: "cohorts", label: "人员组", to: "/cohorts", Icon: UsersRound },
  { key: "settings", label: "系统设置", to: "/settings", Icon: Settings },
];

function NavIcon({ Icon }) {
  return <Icon className="nav-icon" aria-hidden="true" size={17} strokeWidth={1.8} />;
}

export default function Sidebar({ activePage, collapsed, onToggle, user, onLogout }) {
  return (
    <aside className={collapsed ? "sidebar is-collapsed" : "sidebar"} aria-label="全局导航">
      <div className="sidebar-head">
        <div className="brand-mark" title="实证标注平台">
          <Workflow aria-hidden="true" size={18} strokeWidth={1.8} />
        </div>
        <div className="brand-copy">
          <h1>实证标注平台</h1>
          <div className="sub">任务与数据流管理</div>
        </div>
        <button
          className="sidebar-toggle"
          type="button"
          onClick={onToggle}
          title={collapsed ? "展开侧栏" : "收起侧栏"}
          aria-label={collapsed ? "展开侧栏" : "收起侧栏"}
          aria-expanded={!collapsed}
        >
          {collapsed ? <PanelLeftOpen aria-hidden="true" size={17} /> : <PanelLeftClose aria-hidden="true" size={17} />}
        </button>
      </div>

      <nav className="global-nav" aria-label="全局入口">
        {GLOBAL_NAV_ITEMS.map(({ key, label, to, Icon }) => (
          <Link
            key={key}
            to={to}
            className={activePage === key ? "nav-item active" : "nav-item"}
            title={label}
            aria-current={activePage === key ? "page" : undefined}
          >
            <NavIcon Icon={Icon} />
            <span className="nav-label">{label}</span>
          </Link>
        ))}
      </nav>

      <div className="sidebar-account">
        <div className="sidebar-account-name" title={user?.display_name || user?.email || "当前用户"}>
          {user?.display_name || user?.email || "当前用户"}
        </div>
        <button className="sidebar-logout" type="button" onClick={onLogout} title="退出登录" aria-label="退出登录">
          <LogOut aria-hidden="true" size={15} strokeWidth={1.8} />
          <span className="nav-label">退出登录</span>
        </button>
      </div>
    </aside>
  );
}
