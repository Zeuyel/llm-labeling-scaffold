import React, { useEffect, useState, useCallback } from "react";
import * as api from "./../api.js";
import { Link } from "./../router.jsx";

const BADGE = { succeeded: "badge-green", failed: "badge-red", running: "badge-blue", pending: "badge-gray" };

export default function JobsPage({ taskId, onError }) {
  const [jobs, setJobs] = useState([]);
  const [active, setActive] = useState(null);

  const reload = useCallback(async () => {
    if (!taskId) return;
    try {
      const d = await api.getJobs(taskId);
      setJobs(d.jobs || []);
    } catch (e) { onError(String(e)); }
  }, [taskId, onError]);

  useEffect(() => { reload(); }, [reload]);

  return (
    <div>
      <div className="crumbs"><Link to="/">全部任务</Link> / <Link to={`/task/${encodeURIComponent(taskId)}`}>{taskId}</Link> / 任务</div>
      <div className="page-header">
        <h2>任务 Job</h2>
        <p>流水线动作的执行记录与状态</p>
      </div>
      <div className="card">
        <div className="toolbar"><h3>Job 列表（{jobs.length}）</h3><button className="btn btn-sm" onClick={reload}>刷新</button></div>
        {!jobs.length && <div className="empty">暂无任务</div>}
        {jobs.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead><tr><th>id</th><th>类型</th><th>状态</th><th>创建时间</th><th>结果/错误</th><th>操作</th></tr></thead>
              <tbody>
                {jobs.map((j) => (
                  <tr key={j.id}>
                    <td>{j.id}</td>
                    <td>{j.kind}</td>
                    <td><span className={`badge ${BADGE[j.status] || "badge-gray"}`}>{j.status}</span></td>
                    <td className="muted">{(j.created_at || "").slice(0, 19)}</td>
                    <td className="muted">{j.error ? j.error.slice(0, 80) : JSON.stringify(j.result || {}).slice(0, 80)}</td>
                    <td><button className="btn btn-sm" onClick={() => setActive(j)}>详情</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
      {active && (
        <div className="card" style={{ marginTop: 16 }}>
          <div className="toolbar"><h3>Job {active.id} 详情</h3><button className="btn btn-sm" onClick={() => setActive(null)}>关闭</button></div>
          <p className="muted">类型 {active.kind} · 状态 {active.status}</p>
          <pre style={{ whiteSpace: "pre-wrap", fontSize: 12, background: "var(--muted-bg)", padding: 12, borderRadius: 8, overflowX: "auto" }}>{(active.logs || []).join("\n") || "(无日志)"}</pre>
        </div>
      )}
    </div>
  );
}
