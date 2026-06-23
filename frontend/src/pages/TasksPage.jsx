import React from "react";
import { Link } from "./../router.jsx";

export default function TasksPage({ tasks, onReload }) {
  return (
    <div>
      <div className="page-header">
        <h2>全部任务</h2>
        <p>选择一个标注任务进入其数据流水线</p>
      </div>
      <div className="toolbar">
        <span className="muted">{tasks.length} 个任务</span>
        <button className="btn btn-sm" onClick={onReload}>刷新</button>
      </div>
      {!tasks.length && <div className="empty">未发现任务（检查 --tasks-root 目录下的 task.yaml）</div>}
      <div className="grid grid-cards">
        {tasks.map((t) => (
          <Link key={t.path} to={`/task/${encodeURIComponent(t.task_id)}`} className="card">
            <h3>{t.task_id || "(无效)"}</h3>
            {t.error ? (
              <span className="badge badge-red">{t.error}</span>
            ) : (
              <div className="muted">
                <div>id 字段：{t.id_field}</div>
                <div>主标签：{t.primary_label ? t.primary_label.name : "-"}</div>
              </div>
            )}
          </Link>
        ))}
      </div>
    </div>
  );
}
