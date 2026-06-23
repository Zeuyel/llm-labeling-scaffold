import React, { useEffect, useState, useCallback } from "react";
import * as api from "./../api.js";
import { Link } from "./../router.jsx";

export default function SamplesPage({ task, taskId, onError }) {
  const [samples, setSamples] = useState([]);
  const [sampleId, setSampleId] = useState("");
  const [rows, setRows] = useState(6);
  const [strategy, setStrategy] = useState("head");
  const [batchSize, setBatchSize] = useState(5);
  const [batchSample, setBatchSample] = useState("");
  const [busy, setBusy] = useState(false);

  const reload = useCallback(async () => {
    if (!taskId) return;
    try {
      const s = await api.getTaskSamples(taskId);
      setSamples(s.samples || []);
    } catch (e) { onError(String(e)); }
  }, [taskId, onError]);

  useEffect(() => { reload(); }, [reload]);

  async function createSample() {
    if (!task || !sampleId) { onError("请填写 sample_id"); return; }
    setBusy(true);
    try {
      await api.startAction(task.path, "sample", { sample_id: sampleId, rows: Number(rows), strategy });
      setSampleId("");
      setTimeout(reload, 500);
    } catch (e) { onError(String(e)); } finally { setBusy(false); }
  }

  async function runBatch() {
    if (!task || !batchSample) { onError("请选择要分批的 sample"); return; }
    setBusy(true);
    try {
      await api.startAction(task.path, "batch", { sample: batchSample, batch_size: Number(batchSize) });
      setTimeout(reload, 500);
    } catch (e) { onError(String(e)); } finally { setBusy(false); }
  }

  return (
    <div>
      <div className="crumbs"><Link to="/">全部任务</Link> / <Link to={`/task/${encodeURIComponent(taskId)}`}>{taskId}</Link> / 采样</div>
      <div className="page-header">
        <h2>采样 / Artifact</h2>
        <p>从原始语料抽样生成 sample，并切分为标注批次</p>
      </div>
      <div className="card" style={{ marginBottom: 16 }}>
        <h3>新建采样（sample）</h3>
        <div className="form-grid">
          <div className="field"><label>sample_id</label><input value={sampleId} onChange={(e) => setSampleId(e.target.value)} placeholder="例如 seed_v1" /></div>
          <div className="field"><label>行数 rows</label><input type="number" value={rows} onChange={(e) => setRows(e.target.value)} /></div>
          <div className="field"><label>策略 strategy</label><select value={strategy} onChange={(e) => setStrategy(e.target.value)}><option value="head">head</option><option value="random">random</option></select></div>
        </div>
        <button className="btn btn-primary" disabled={busy} onClick={createSample}>创建采样任务</button>
      </div>
      <div className="card" style={{ marginBottom: 16 }}>
        <h3>分批（batch）</h3>
        <div className="form-grid">
          <div className="field"><label>选择 sample</label><select value={batchSample} onChange={(e) => setBatchSample(e.target.value)}><option value="">-- 选择 --</option>{samples.map((s) => <option key={s.sample_id} value={s.path}>{s.sample_id}</option>)}</select></div>
          <div className="field"><label>批大小 batch_size</label><input type="number" value={batchSize} onChange={(e) => setBatchSize(e.target.value)} /></div>
        </div>
        <button className="btn" disabled={busy} onClick={runBatch}>创建分批任务</button>
      </div>
      <div className="card">
        <div className="toolbar"><h3>已有采样</h3><button className="btn btn-sm" onClick={reload}>刷新</button></div>
        {!samples.length && <div className="empty">暂无采样</div>}
        {samples.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead><tr><th>sample_id</th><th>行数</th><th>策略</th><th>路径</th></tr></thead>
              <tbody>
                {samples.map((s) => (
                  <tr key={s.sample_id}>
                    <td>{s.sample_id}</td>
                    <td>{s.manifest ? s.manifest.rows : "-"}</td>
                    <td>{s.manifest ? s.manifest.strategy : "-"}</td>
                    <td className="muted">{s.path}</td>
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
