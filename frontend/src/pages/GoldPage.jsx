import React, { useEffect, useState, useCallback } from "react";
import * as api from "./../api.js";
import { Link } from "./../router.jsx";

export default function GoldPage({ task, taskId, onError }) {
  const [versions, setVersions] = useState([]);
  const [runs, setRuns] = useState([]);
  const [run, setRun] = useState("");
  const [version, setVersion] = useState("");
  const [busy, setBusy] = useState(false);

  const reload = useCallback(async () => {
    if (!taskId) return;
    try {
      const [g, r] = await Promise.all([api.getTaskGoldVersions(taskId), api.getTaskRuns(taskId)]);
      setVersions(g.gold_versions || []);
      setRuns(r.runs || []);
    } catch (e) { onError(String(e)); }
  }, [taskId, onError]);

  useEffect(() => { reload(); }, [reload]);

  async function buildGold() {
    if (!task || !run || !version) { onError("请选择 run 并填写版本号"); return; }
    setBusy(true);
    try {
      const selected = runs.find((x) => x.path === run);
      const params = { run, version };
      if (selected && selected.decisions > 0) {
        params.decisions = `${run}/adjudication/decisions.jsonl`;
      }
      await api.startAction(task.path, "gold", params);
      setVersion("");
      setTimeout(reload, 600);
    } catch (e) { onError(String(e)); } finally { setBusy(false); }
  }

  return (
    <div>
      <div className="crumbs"><Link to="/">全部任务</Link> / <Link to={`/task/${encodeURIComponent(taskId)}`}>{taskId}</Link> / Gold 版本</div>
      <div className="page-header">
        <h2>Gold 版本</h2>
        <p>合并 + 裁决生成版本化 gold 集，作为训练数据</p>
      </div>
      <div className="card" style={{ marginBottom: 16 }}>
        <h3>构建 Gold（gold build）</h3>
        <div className="form-grid">
          <div className="field"><label>来源 run</label><select value={run} onChange={(e) => setRun(e.target.value)}><option value="">-- 选择 --</option>{runs.map((r) => <option key={r.run_id} value={r.path}>{r.run_id}{r.decisions ? ` (裁决 ${r.decisions})` : ""}</option>)}</select></div>
          <div className="field"><label>版本号 version</label><input value={version} onChange={(e) => setVersion(e.target.value)} placeholder="例如 v001" /></div>
        </div>
        <button className="btn btn-primary" disabled={busy} onClick={buildGold}>构建 Gold 任务</button>
      </div>
      <div className="card">
        <div className="toolbar"><h3>Gold 版本列表（{versions.length}）</h3><button className="btn btn-sm" onClick={reload}>刷新</button></div>
        {!versions.length && <div className="empty">暂无 gold 版本</div>}
        {versions.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead><tr><th>版本</th><th>行数</th><th>主标签</th><th>标签分布</th><th>创建时间</th></tr></thead>
              <tbody>
                {versions.map((g) => (
                  <tr key={g.version}>
                    <td><span className="badge badge-blue">{g.version}</span></td>
                    <td>{g.rows}</td>
                    <td>{g.primary_label}</td>
                    <td className="muted">{JSON.stringify(g.label_counts || {})}</td>
                    <td className="muted">{(g.created_at || "").slice(0, 19)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
