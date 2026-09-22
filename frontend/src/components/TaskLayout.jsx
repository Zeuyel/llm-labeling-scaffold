import React, { useEffect } from "react";
import { ChevronRight, RefreshCw } from "lucide-react";
import { Link, useRouter } from "../router.jsx";
import TaskNavigation, { taskHref } from "./TaskNavigation.jsx";
import "./navigation.css";

export function LegacyTaskArchiveRedirect({ taskId }) {
  const { navigate } = useRouter();
  const destination = taskHref(taskId, "/configuration");

  useEffect(() => {
    navigate(destination, { replace: true });
  }, [destination, navigate]);

  return (
    <div className="task-route-status" role="status" aria-live="polite">
      正在打开任务配置...
    </div>
  );
}

function TaskSummaryState({ loading, error, onRetry }) {
  if (loading) {
    return (
      <div className="task-context-state" role="status" aria-live="polite">
        正在加载任务信息...
      </div>
    );
  }
  return (
    <div className="task-context-state task-context-state-error" role="alert" aria-live="assertive">
      <span>任务信息加载失败：{error || "任务详情暂不可用，请重试。"}</span>
      <button
        className="btn btn-sm"
        type="button"
        onClick={() => Promise.resolve().then(() => onRetry?.()).catch(() => {})}
        title="重试加载任务"
        aria-label="重试加载任务"
      >
        <RefreshCw aria-hidden="true" size={14} strokeWidth={1.8} />
        <span>重试</span>
      </button>
    </div>
  );
}

export default function TaskLayout({
  taskId,
  taskSummary,
  taskSummaryRequired = true,
  activePage,
  taskSummaryLoading = false,
  taskSummaryError = "",
  onRetryTaskSummary = () => Promise.resolve(),
  children,
}) {
  const taskLabel = taskSummary?.task_id || taskId || "当前任务";
  const canRenderChildren = !taskSummaryRequired
    || (!taskSummaryLoading && !taskSummaryError && Boolean(taskSummary));

  return (
    <section className="task-layout" aria-label="任务上下文">
      <div className="task-breadcrumbs" aria-label="当前位置">
        <Link to="/" title="返回全部任务">全部任务</Link>
        <ChevronRight aria-hidden="true" size={14} strokeWidth={1.8} />
        <span aria-current="page" title={taskLabel}>当前任务</span>
      </div>
      <TaskNavigation taskId={taskId} activePage={activePage} />
      <div className="task-layout-content">
        {canRenderChildren ? children : (
          <TaskSummaryState
            loading={taskSummaryLoading}
            error={taskSummaryError}
            onRetry={onRetryTaskSummary}
          />
        )}
      </div>
    </section>
  );
}
