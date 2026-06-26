import React, { useEffect, useState, useCallback } from "react";
import * as api from "./../api.js";
import { Link } from "./../router.jsx";

export default function SamplesPage({ task, taskId, onError }) {
  const [samples, setSamples] = useState([]);
  const [imports, setImports] = useState([]);
  const [sampleId, setSampleId] = useState("");
  const [source, setSource] = useState("");
  const [rows, setRows] = useState(6);
  const [strategy, setStrategy] = useState("head");
  const [batchSize, setBatchSize] = useState(5);
  const [batchSample, setBatchSample] = useState("");
  const [busy, setBusy] = useState(false);

  const reload = useCallback(async () => {
    if (!taskId) return;
    try {
      const [s, i] = await Promise.all([api.getTaskSamples(taskId), api.getImports(taskId)]);
      setSamples(s.samples || []);
      setImports(i.imports || []);
    } catch (e) { onError(String(e)); }
  }, [taskId, onError]);

  useEffect(() => { reload(); }, [reload]);

  async function runAction(action, params, label) {
    if (!task) return false;
    setBusy(true);
    try {
      const job = await api.startAction(task.path, action, params);
      const finished = job?.id ? await api.waitForJob(taskId, job.id) : null;
      if (finished?.status === "failed") {
        throw new Error(finished.error || "执行失败");
      }
      await reload();
      return true;
    } catch (e) {
      onError(`${label}: ${e}`);
      return false;
    } finally { setBusy(false); }
  }

  async function createSample() {
    if (!task || !sampleId) { onError("请填写样本编号"); return; }
    const ok = await runAction("sample", { sample_id: sampleId, rows: Number(rows), strategy, source }, "创建样本");
    if (ok) setSampleId("");
  }

  async function runBatch() {
    if (!task || !batchSample) { onError("请选择要切分的样本"); return; }
    await runAction("batch", { sample: batchSample, batch_size: Number(batchSize) }, "切分批次");
  }

  return (
    <div>
      <div className="crumbs"><Link to="/">全部任务</Link> / <Link to={`/task/${encodeURIComponent(taskId)}`}>{taskId}</Link> / 样本管理</div>
      <div className="page-header">
        <h2>样本管理</h2>
        <p>从原始语料抽样生成标注样本，并按需要切分为批次</p>
      </div>
      <div className="card section-card">
        <h3>创建样本</h3>
        <div className="form-grid">
          <div className="field"><label>样本编号</label><input value={sampleId} onChange={(e) => setSampleId(e.target.value)} placeholder="例如 seed_v1" /></div>
          <div className="field">
            <label>数据来源</label>
            <select value={source} onChange={(e) => setSource(e.target.value)}>
              <option value="">任务配置中的原始语料</option>
              {imports.map((item) => (
                <option key={item.import_id} value={item.path}>{item.import_id} · {item.rows} 行</option>
              ))}
            </select>
          </div>
          <div className="field"><label>抽样行数</label><input type="number" min="1" value={rows} onChange={(e) => setRows(e.target.value)} /></div>
          <div className="field"><label>抽样策略</label><select value={strategy} onChange={(e) => setStrategy(e.target.value)}><option value="head">前 N 行</option><option value="random">随机抽样</option></select></div>
        </div>
        <button className="btn btn-primary" disabled={busy} onClick={createSample}>创建样本</button>
      </div>
      <div className="card section-card">
        <h3>切分批次</h3>
        <div className="form-grid">
          <div className="field"><label>样本</label><select value={batchSample} onChange={(e) => setBatchSample(e.target.value)}><option value="">选择样本</option>{samples.map((s) => <option key={s.sample_id} value={s.path}>{s.sample_id}</option>)}</select></div>
          <div className="field"><label>每批行数</label><input type="number" min="1" value={batchSize} onChange={(e) => setBatchSize(e.target.value)} /></div>
        </div>
        <button className="btn" disabled={busy} onClick={runBatch}>切分批次</button>
      </div>
      <div className="card">
        <div className="toolbar"><h3>已有样本（{samples.length}）</h3><button className="btn btn-sm" onClick={reload}>刷新</button></div>
        {!samples.length && <div className="empty">暂无样本</div>}
        {samples.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead><tr><th>样本编号</th><th>行数</th><th>抽样策略</th><th>存储路径</th></tr></thead>
              <tbody>
                {samples.map((s) => (
                  <tr key={s.sample_id}>
                    <td>{s.sample_id}</td>
                    <td>{s.manifest ? s.manifest.rows : "-"}</td>
                    <td>{s.manifest?.strategy === "random" ? "随机抽样" : s.manifest?.strategy === "head" ? "前 N 行" : "-"}</td>
                    <td className="muted path-cell">{s.path}</td>
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
