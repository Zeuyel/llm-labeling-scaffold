import React, { useEffect, useState, useCallback } from "react";
import * as api from "./../api.js";
import { Link } from "./../router.jsx";

const POOLS = [
  { key: "merged", label: "合并 Merged" },
  { key: "missing", label: "缺失 Missing" },
  { key: "duplicate", label: "重复 Duplicate" },
  { key: "conflict", label: "冲突 Conflict" },
];

export default function RunDetailPage({ task, taskId, runId, onError }) {
  const [kind, setKind] = useState("merged");
  const [rows, setRows] = useState([]);
  const [decisions, setDecisions] = useState([]);

  const loadRows = useCallback(async (k) => {
    try {
      const d = await api.getRows(taskId, runId, k);
      setRows(d.rows || []);
    } catch (e) { onError(String(e)); }
  }, [taskId, runId, onError]);

  const loadDecisions = useCallback(async () => {
    try {
      const d = await api.getDecisions(taskId, runId);
      setDecisions(d.decisions || []);
    } catch (e) { onError(String(e)); }
  }, [taskId, runId, onError]);

  useEffect(() => { setKind("merged"); loadRows("merged"); loadDecisions(); }, [loadRows, loadDecisions]);

  function pick(k) { setKind(k); loadRows(k); }

  async function adjudicate(row) {
    const idField = Object.keys(row).find((k) => k.includes("id")) || "record_id";
    const id = row[idField];
    const label = prompt(`为 ${id} 输入新标签 (JSON, 例如 {"class_label":"non_target"})`);
    if (!label) return;
    let human;
    try { human = JSON.parse(label); } catch { onError("JSON 格式错误"); return; }
    try {
      const res = await api.adjudicate(taskId, runId, { id, id_field: idField, human_label: human });
      alert(`已保存，共 ${res.decisions} 条裁决`);
      loadDecisions();
    } catch (e) { onError(String(e)); }
  }

  const cols = rows.length ? Object.keys(rows[0]) : [];

  return (
    <div>
      <div className="crumbs"><Link to="/">全部任务</Link> / <Link to={`/task/${encodeURIComponent(taskId)}`}>{taskId}</Link> / <Link to={`/task/${encodeURIComponent(taskId)}/runs`}>标注运行</Link> / {runId}</div>
      <div className="page-header">
        <h2>运行 {runId}</h2>
        <p>查看数据池、逐行人工裁决并导出</p>
      </div>
      <div className="card" style={{ marginBottom: 16 }}>
        <div className="toolbar">
          <div className="tabs">
            {POOLS.map((p) => (
              <button key={p.key} className={kind === p.key ? "tab active" : "tab"} onClick={() => pick(p.key)}>{p.label}</button>
            ))}
          </div>
          <a className="btn btn-sm" href={api.exportUrl(taskId, runId, kind)}>导出 {kind}</a>
        </div>
        {!rows.length && <div className="empty">该池暂无数据</div>}
        {rows.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead><tr>{cols.map((c) => <th key={c}>{c}</th>)}<th>操作</th></tr></thead>
              <tbody>
                {rows.map((row, i) => (
                  <tr key={i}>
                    {cols.map((c) => <td key={c}>{String(row[c] ?? "").slice(0, 80)}</td>)}
                    <td><button className="btn btn-sm" onClick={() => adjudicate(row)}>裁决</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
      <div className="card">
        <div className="toolbar"><h3>裁决记录 Decision（{decisions.length}）</h3><button className="btn btn-sm" onClick={loadDecisions}>刷新</button></div>
        {!decisions.length && <div className="empty">暂无裁决</div>}
        {decisions.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead><tr><th>记录</th><th>human_label</th><th>备注</th></tr></thead>
              <tbody>
                {decisions.map((d, i) => {
                  const idField = Object.keys(d).find((k) => k.includes("id")) || "record_id";
                  return (
                    <tr key={i}>
                      <td>{d[idField]}</td>
                      <td>{JSON.stringify(d.human_label || {})}</td>
                      <td className="muted">{d.note || ""}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
