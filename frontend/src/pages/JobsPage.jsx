import React, { useEffect, useState, useCallback } from "react";
import * as api from "./../api.js";
import { Link } from "./../router.jsx";
import { displayPlanValue } from "./batchPlanDisplay.js";
import {
  jobBadgeClass,
  jobDebugFields,
  jobKindLabel,
  jobStatusLabel,
  shortJobResult,
} from "./jobDisplay.js";

const EVENT_LABEL = {
  "import.create": "创建导入",
  "import.reuse": "复用导入",
  "import.save": "保存导入",
  "import.archive": "归档导入",
  "sample.create": "创建样本",
  "sample.reuse": "复用样本",
  "sample.save": "保存样本",
  "sample.archive": "归档样本",
  "task.archive": "归档任务",
};
const ASSET_LABEL = {
  import: "导入数据",
  sample: "样本",
  task: "任务",
};

function DetailField({ label, value }) {
  const text = value === undefined || value === null || value === "" ? "-" : value;
  return (
    <div>
      <span>{label}</span>
      <strong>{text}</strong>
    </div>
  );
}

export default function JobsPage({ taskId, onError }) {
  const [jobs, setJobs] = useState([]);
  const [events, setEvents] = useState([]);
  const [active, setActive] = useState(null);

  const reload = useCallback(async () => {
    if (!taskId) return;
    try {
      const [d, a] = await Promise.all([api.getJobs(taskId), api.getAuditEvents(taskId)]);
      setJobs(d.jobs || []);
      setEvents(a.events || []);
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

  const activeErrorText = active?.error ?? active?.result?.error ?? "";
  const activeHasResult = active?.result !== undefined && active?.result !== null;

  return (
    <div>
      <div className="crumbs"><Link to="/">全部任务</Link> / <Link to={`/task/${encodeURIComponent(taskId)}`}>{taskId}</Link> / 执行记录</div>
      <div className="page-header">
        <h2>执行记录</h2>
        <p>流水线动作的执行状态与日志；点击行查看日志、结果和高级详情。</p>
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
              <thead><tr><th>执行编号</th><th>动作</th><th>状态</th><th>创建时间</th><th>摘要</th><th>操作</th></tr></thead>
              <tbody>
                {jobs.map((j) => (
                  <tr
                    key={j.id}
                    className={active?.id === j.id ? "row-selected clickable-row" : "clickable-row"}
                    onClick={() => setActive(j)}
                  >
                    <td className="mono-cell">{j.id}</td>
                    <td>{jobKindLabel(j.kind)}</td>
                    <td><span className={`badge ${jobBadgeClass(j.status)}`}>{jobStatusLabel(j.status)}</span></td>
                    <td className="muted">{(j.created_at || "").slice(0, 19)}</td>
                    <td className="muted text-cell">{shortJobResult(j)}</td>
                    <td>
                      <button
                        className="btn btn-sm"
                        type="button"
                        onClick={(event) => {
                          event.stopPropagation();
                          setActive(j);
                        }}
                      >
                        详情
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
      {active && (
        <div className="drawer-backdrop" onClick={() => setActive(null)}>
          <aside className="drawer-panel" onClick={(event) => event.stopPropagation()}>
            <div className="drawer-head">
              <div>
                <h3>执行记录 {active.id}</h3>
                <p>{jobKindLabel(active.kind)} · {jobStatusLabel(active.status)}</p>
              </div>
              <button className="btn btn-sm" type="button" onClick={() => setActive(null)}>关闭</button>
            </div>
            <div className="drawer-detail-grid">
              <DetailField label="动作" value={jobKindLabel(active.kind)} />
              <DetailField label="状态" value={jobStatusLabel(active.status)} />
              <DetailField label="创建时间" value={(active.created_at || "").slice(0, 19)} />
              <DetailField label="更新时间" value={(active.updated_at || "").slice(0, 19)} />
            </div>
            {activeErrorText && <div className="status-line danger-line drawer-section">错误：{activeErrorText}</div>}
            <div className="drawer-section">
              <div className="toolbar"><h3>执行日志</h3></div>
              <pre className="log-box">{(active.logs || []).join("\n") || "(无日志)"}</pre>
            </div>
            <div className="drawer-section">
              <div className="toolbar"><h3>动作结果</h3></div>
              <pre className="log-box">{activeHasResult ? JSON.stringify(active.result, null, 2) : "(无结果)"}</pre>
            </div>
            <details className="advanced-panel">
              <summary>高级详情 / 调试信息</summary>
              <div className="debug-field-list">
                {jobDebugFields(active).map(([key, value]) => (
                  <div key={key}><span>{key}</span><strong>{displayPlanValue(value)}</strong></div>
                ))}
              </div>
            </details>
          </aside>
        </div>
      )}
      <details className="card secondary-panel">
        <summary>资产审计日志（{events.length}）</summary>
        <div className="toolbar">
          <div>
            <div className="status-line">记录导入、样本、归档等数据资产操作</div>
          </div>
        </div>
        {!events.length && <div className="empty">暂无审计事件</div>}
        {events.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead><tr><th>时间</th><th>事件</th><th>资产</th><th>状态</th><th>详情</th></tr></thead>
              <tbody>
                {events.map((event, index) => (
                  <tr key={`${event.created_at}-${index}`}>
                    <td className="muted">{(event.created_at || "").slice(0, 19)}</td>
                    <td>{EVENT_LABEL[event.event] || event.event}</td>
                    <td>{ASSET_LABEL[event.asset_type] || event.asset_type}/{event.asset_id}</td>
                    <td><span className={`badge ${event.status === "failed" ? "badge-red" : "badge-green"}`}>{event.status === "failed" ? "失败" : "成功"}</span></td>
                    <td className="muted path-cell">{JSON.stringify(event.details || {}).slice(0, 180)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </details>
    </div>
  );
}
