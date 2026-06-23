import React, { useEffect, useState, useCallback } from "react";
import * as api from "./../api.js";
import { Link, useRouter } from "./../router.jsx";

export default function RunsPage({ task, taskId, onError }) {
  const { navigate } = useRouter();
  const [runs, setRuns] = useState([]);
  const [samples, setSamples] = useState([]);
  const [sample, setSample] = useState("");
  const [runId, setRunId] = useState("");
  const [provider, setProvider] = useState("local_stub");
  const [batchSize, setBatchSize] = useState(5);
  const [argillaDataset, setArgillaDataset] = useState("");
  const [argillaMinSubmitted, setArgillaMinSubmitted] = useState(1);
  const [busy, setBusy] = useState(false);

  const reload = useCallback(async () => {
    if (!taskId) return;
    try {
      const [r, s] = await Promise.all([api.getTaskRuns(taskId), api.getTaskSamples(taskId)]);
      setRuns(r.runs || []);
      setSamples(s.samples || []);
    } catch (e) { onError(String(e)); }
  }, [taskId, onError]);

  useEffect(() => { reload(); }, [reload]);

  async function action(name, params, label) {
    if (!task) return;
    setBusy(true);
    try {
      await api.startAction(task.path, name, params);
      setTimeout(reload, 600);
    } catch (e) { onError(`${label}: ${e}`); } finally { setBusy(false); }
  }

  async function annotate() {
    if (!sample || !runId) { onError("请选择 sample 并填写 run_id"); return; }
    await action("annotate", { sample, run_id: runId, provider, batch_size: Number(batchSize) }, "标注");
    setRunId("");
  }

  async function pushArgilla() {
    if (!sample || !argillaDataset) { onError("请选择 sample 并填写 Argilla dataset"); return; }
    await action("argilla_push", {
      sample,
      dataset: argillaDataset,
      argilla: { min_submitted: Number(argillaMinSubmitted) },
    }, "推送 Argilla");
  }

  async function pullArgilla() {
    if (!argillaDataset) { onError("请填写 Argilla dataset"); return; }
    await action("argilla_pull", { dataset: argillaDataset }, "同步 Argilla");
  }

  return (
    <div>
      <div className="crumbs"><Link to="/">全部任务</Link> / <Link to={`/task/${encodeURIComponent(taskId)}`}>{taskId}</Link> / 标注运行</div>
      <div className="page-header">
        <h2>标注运行 Run</h2>
        <p>对 sample 触发标注、审核、合并，生成数据池</p>
      </div>
      <div className="card" style={{ marginBottom: 16 }}>
        <h3>新建标注运行（annotate）</h3>
        <div className="form-grid">
          <div className="field"><label>sample</label><select value={sample} onChange={(e) => setSample(e.target.value)}><option value="">-- 选择 --</option>{samples.map((s) => <option key={s.sample_id} value={s.path}>{s.sample_id}</option>)}</select></div>
          <div className="field"><label>run_id</label><input value={runId} onChange={(e) => setRunId(e.target.value)} placeholder="例如 run_v1" /></div>
          <div className="field"><label>provider</label><input value={provider} onChange={(e) => setProvider(e.target.value)} /></div>
          <div className="field"><label>batch_size</label><input type="number" value={batchSize} onChange={(e) => setBatchSize(e.target.value)} /></div>
        </div>
        <button className="btn btn-primary" disabled={busy} onClick={annotate}>开始标注任务</button>
      </div>
      <div className="card" style={{ marginBottom: 16 }}>
        <h3>Argilla 标注工作台</h3>
        <div className="form-grid">
          <div className="field"><label>sample</label><select value={sample} onChange={(e) => setSample(e.target.value)}><option value="">-- 选择 --</option>{samples.map((s) => <option key={s.sample_id} value={s.path}>{s.sample_id}</option>)}</select></div>
          <div className="field"><label>dataset</label><input value={argillaDataset} onChange={(e) => setArgillaDataset(e.target.value)} placeholder={`${taskId}_annotation_v1`} /></div>
          <div className="field"><label>每条记录提交数</label><input type="number" value={argillaMinSubmitted} onChange={(e) => setArgillaMinSubmitted(e.target.value)} /></div>
        </div>
        <button className="btn btn-primary" disabled={busy} onClick={pushArgilla}>推送到 Argilla</button>{" "}
        <button className="btn" disabled={busy} onClick={pullArgilla}>从 Argilla 同步标签</button>
      </div>
      <div className="card">
        <div className="toolbar"><h3>运行列表</h3><button className="btn btn-sm" onClick={reload}>刷新</button></div>
        {!runs.length && <div className="empty">暂无运行</div>}
        {runs.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead><tr><th>run_id</th><th>审核</th><th>合并</th><th>merged 行</th><th>裁决数</th><th>操作</th></tr></thead>
              <tbody>
                {runs.map((r) => (
                  <tr key={r.run_id}>
                    <td>{r.run_id}</td>
                    <td>{r.has_audit ? <span className="badge badge-green">已审核</span> : <span className="badge badge-gray">未审核</span>}</td>
                    <td>{r.has_merge ? <span className="badge badge-green">已合并</span> : <span className="badge badge-gray">未合并</span>}</td>
                    <td>{r.merge ? r.merge.merged_rows : "-"}</td>
                    <td>{r.decisions}</td>
                    <td>
                      <button className="btn btn-sm" disabled={busy} onClick={() => action("audit", { run: r.path }, "审核")}>审核</button>{" "}
                      <button className="btn btn-sm" disabled={busy} onClick={() => action("merge", { run: r.path }, "合并")}>合并</button>{" "}
                      <button className="btn btn-sm" onClick={() => navigate(`/task/${encodeURIComponent(taskId)}/runs/${encodeURIComponent(r.run_id)}`)}>打开</button>
                    </td>
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
