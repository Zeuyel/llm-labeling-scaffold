import React from "react";
import {
  Boxes,
  ClipboardList,
  Database,
  FileInput,
  GitBranch,
  History,
  LayoutDashboard,
  Send,
  Settings2,
  ShieldCheck,
} from "lucide-react";
import { Link } from "../router.jsx";

export const TASK_NAV_ITEMS = [
  { key: "overview", label: "概览", suffix: "", Icon: LayoutDashboard },
  { key: "canvas", label: "流程画布", suffix: "/canvas", Icon: GitBranch },
  { key: "imports", label: "任务输入", suffix: "/imports", Icon: FileInput },
  { key: "samples", label: "样本", suffix: "/samples", Icon: Database },
  { key: "allocation-plans", label: "分配", suffix: "/allocation-plans", Icon: ClipboardList },
  { key: "annotations", label: "分发", suffix: "/annotations", Icon: Send },
  { key: "gold", label: "Gold 版本", suffix: "/gold", Icon: ShieldCheck },
  { key: "jobs", label: "执行记录", suffix: "/jobs", Icon: History },
  { key: "models", label: "训练与推理", suffix: "/models", Icon: Boxes },
  { key: "configuration", label: "任务配置", suffix: "/configuration", Icon: Settings2 },
];

export function taskHref(taskId, suffix = "") {
  return `/task/${encodeURIComponent(taskId)}${suffix}`;
}

function TaskNavIcon({ Icon }) {
  return <Icon className="task-nav-icon" aria-hidden="true" size={16} strokeWidth={1.8} />;
}

export default function TaskNavigation({ taskId, activePage }) {
  if (!taskId) return null;

  return (
    <div className="task-navigation">
      <nav className="task-nav-list" aria-label="当前任务导航">
        {TASK_NAV_ITEMS.map(({ key, label, suffix, Icon }) => {
          const active = activePage === key;
          return (
            <Link
              key={key}
              to={taskHref(taskId, suffix)}
              className={active ? "task-nav-item active" : "task-nav-item"}
              title={label}
              aria-current={active ? "page" : undefined}
            >
              <TaskNavIcon Icon={Icon} />
              <span>{label}</span>
            </Link>
          );
        })}
      </nav>
    </div>
  );
}
