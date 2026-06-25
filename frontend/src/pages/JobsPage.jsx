import React, { useEffect, useState, useCallback } from "react";
import * as api from "./../api.js";
import { Link } from "./../router.jsx";

const BADGE = { succeeded: "badge-green", failed: "badge-red", running: "badge-blue", pending: "badge-gray" };
const STATUS_LABEL = { succeeded: "成功", failed: "失败", running: "运行中", pending: "等待中" };
const KIND_LABEL = {
  sample: "创建样本",
  batch: "切分批次",
  annotate: "本地调试标注",
  argilla_push: "推送 Argilla",
  argilla_pull: "拉回标注结果",
  audit: "审核摘要",
  merge: "合并输出",
  gold: "构建训练集",
  train: "训练模型",
  infer: "模型推理",
};

function shortResult(job) {
  if (job.error) return job.error.slice(0, 120);
  return JSON.stringify(job.result || {}).slice(0, 120);
}

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

  useEffect(() => {
    reload();
    const timer = setInterval(reload, 3000);
    return () => clearInterval(timer);
  }, [reload]);

  useEffect(() => {
    if (!active) return;
    const latest = jobs.find((job) => job.id === active.id);
    if (latest && latest !== active) setActive(latest);
  }, [jobs, active]);

  return (
    <div>
      <div className="crumbs"><Link to="/">全部任务</Link> / <Link to={`/task/${encodeURIComponent(taskId)}`}>{taskId}</Link> / 执行记录</div>
      <div className="page-header">
        <h2>执行记录</h2>
        <p>流水线动作的执行状态与日志，页面会自动刷新</p>
      </div>
      <div className="card">
        <div className="toolbar">
          <div>
            <h3>执行记录列表（{jobs.length}）</h3>
            <div className="status-line">每 3 秒自动刷新，也可以手动刷新</div>
          </div>
          <button className="btn btn-sm" onClick={reload}>刷新</button>
        </div>
        {!jobs.length && <div className="empty">暂无执行记录</div>}
        {jobs.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead><tr><th>执行编号</th><th>动作</th><th>状态</th><th>创建时间</th><th>结果/错误</th><th>操作</th></tr></thead>
              <tbody>
                {jobs.map((j) => (
                  <tr key={j.id}>
                    <td>{j.id}</td>
                    <td>{KIND_LABEL[j.kind] || j.kind}</td>
                    <td><span className={`badge ${BADGE[j.status] || "badge-gray"}`}>{STATUS_LABEL[j.status] || j.status}</span></td>
                    <td className="muted">{(j.created_at || "").slice(0, 19)}</td>
                    <td className="muted path-cell">{shortResult(j)}</td>
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
          <div className="toolbar"><h3>执行记录 {active.id} 详情</h3><button className="btn btn-sm" onClick={() => setActive(null)}>关闭</button></div>
          <p className="muted">动作 {KIND_LABEL[active.kind] || active.kind} · 状态 {STATUS_LABEL[active.status] || active.status}</p>
          <pre style={{ whiteSpace: "pre-wrap", fontSize: 12, background: "var(--muted-bg)", padding: 12, borderRadius: 8, overflowX: "auto" }}>{(active.logs || []).join("\n") || "(无日志)"}</pre>
        </div>
      )}
    </div>
  );
}
