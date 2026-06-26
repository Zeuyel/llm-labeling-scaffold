import React, { useEffect, useState, useCallback } from "react";
import * as api from "./../api.js";
import { Link } from "./../router.jsx";

export default function RunsPage({ task, taskId, onError }) {
  const [runs, setRuns] = useState([]);
  const [samples, setSamples] = useState([]);
  const [decisions, setDecisions] = useState([]);
  const [sample, setSample] = useState("");
  const [annotationId, setAnnotationId] = useState("");
  const [runId, setRunId] = useState("");
  const [provider, setProvider] = useState("local_stub");
  const [batchSize, setBatchSize] = useState(5);
  const [argillaDataset, setArgillaDataset] = useState("");
  const [argillaMinSubmitted, setArgillaMinSubmitted] = useState(1);
  const [busy, setBusy] = useState(false);

  const reload = useCallback(async () => {
    if (!taskId) return;
    try {
      const [r, s, d] = await Promise.all([
        api.getTaskRuns(taskId),
        api.getTaskSamples(taskId),
        api.getDecisionArtifacts(taskId),
      ]);
      setRuns(r.runs || []);
      setSamples(s.samples || []);
      setDecisions(d.decision_artifacts || []);
    } catch (e) { onError(String(e)); }
  }, [taskId, onError]);

  useEffect(() => { reload(); }, [reload]);

  const selectedSample = samples.find((item) => item.path === sample);

  async function action(name, params, label) {
    if (!task) return false;
    setBusy(true);
    try {
      const job = await api.startAction(task.path, name, params);
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

  async function annotate() {
    if (!sample || !runId) { onError("请选择样本并填写调试运行编号"); return; }
    const ok = await action("annotate", { sample, run_id: runId, provider, batch_size: Number(batchSize) }, "标注");
    if (ok) setRunId("");
  }

  async function pushArgilla() {
    if (!sample || !argillaDataset) { onError("请选择样本并填写 Argilla 数据集名"); return; }
    await action("argilla_push", {
      sample,
      dataset: argillaDataset,
      annotation_id: annotationId || argillaDataset,
      sample_id: selectedSample?.sample_id,
      argilla: { min_submitted: Number(argillaMinSubmitted) },
    }, "推送 Argilla");
  }

  async function pullArgilla() {
    if (!sample || !argillaDataset) { onError("请选择样本并填写 Argilla 数据集名"); return; }
    await action("argilla_pull", {
      sample,
      sample_id: selectedSample?.sample_id,
      dataset: argillaDataset,
      decision_id: annotationId || argillaDataset,
    }, "拉回标注结果");
  }

  return (
    <div>
      <div className="crumbs"><Link to="/">全部任务</Link> / <Link to={`/task/${encodeURIComponent(taskId)}`}>{taskId}</Link> / 标注分发</div>
      <div className="page-header">
        <h2>标注分发</h2>
        <p>实验人员在这里把样本分发到 Argilla，并拉回人工标注结果产物</p>
      </div>
      <div className="card section-card">
        <h3>Argilla 标注任务</h3>
        <div className="form-grid">
          <div className="field">
            <label>样本</label>
            <select value={sample} onChange={(e) => setSample(e.target.value)}>
              <option value="">选择样本</option>
              {samples.map((s) => <option key={s.sample_id} value={s.path}>{s.sample_id}</option>)}
            </select>
          </div>
          <div className="field">
            <label>标注任务编号</label>
            <input value={annotationId} onChange={(e) => setAnnotationId(e.target.value)} placeholder={`${taskId}_label_v1`} />
            <span className="hint">用于本地记录标注结果产物；不填时使用 Argilla 数据集名</span>
          </div>
          <div className="field">
            <label>Argilla 数据集名</label>
            <input value={argillaDataset} onChange={(e) => setArgillaDataset(e.target.value)} placeholder={`${taskId}_annotation_v1`} />
          </div>
          <div className="field">
            <label>单条记录所需提交数</label>
            <input type="number" min="1" value={argillaMinSubmitted} onChange={(e) => setArgillaMinSubmitted(e.target.value)} />
          </div>
        </div>
        <div className="action-row">
          <button className="btn btn-primary" disabled={busy} onClick={pushArgilla}>推送到 Argilla</button>
          <button className="btn" disabled={busy} onClick={pullArgilla}>拉回标注结果</button>
        </div>
      </div>
      <div className="card section-card">
        <div className="toolbar"><h3>标注结果产物（{decisions.length}）</h3><button className="btn btn-sm" onClick={reload}>刷新</button></div>
        {!decisions.length && <div className="empty">暂无标注结果产物</div>}
        {decisions.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead><tr><th>产物编号</th><th>来源</th><th>Argilla 数据集</th><th>样本</th><th>行数</th><th>存储路径</th></tr></thead>
              <tbody>
                {decisions.map((d) => (
                  <tr key={d.decision_id || d.path}>
                    <td><span className="badge badge-blue">{d.decision_id || "-"}</span></td>
                    <td>{d.source === "argilla" ? "Argilla" : (d.source || "-")}</td>
                    <td>{d.argilla_dataset || "-"}</td>
                    <td>{d.sample_id || "-"}</td>
                    <td>{d.rows ?? d.result?.responses ?? "-"}</td>
                    <td className="muted path-cell">{d.path}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
      <details className="card secondary-panel">
        <summary>本地模型标注调试</summary>
        <p className="muted">这里仅用于快速检查模型输出，不作为正式人工标注入口。</p>
        <div className="form-grid">
          <div className="field"><label>样本</label><select value={sample} onChange={(e) => setSample(e.target.value)}><option value="">选择样本</option>{samples.map((s) => <option key={s.sample_id} value={s.path}>{s.sample_id}</option>)}</select></div>
          <div className="field"><label>调试运行编号</label><input value={runId} onChange={(e) => setRunId(e.target.value)} placeholder="例如 debug_v1" /></div>
          <div className="field"><label>模型来源标识</label><input value={provider} onChange={(e) => setProvider(e.target.value)} /></div>
          <div className="field"><label>批大小</label><input type="number" value={batchSize} onChange={(e) => setBatchSize(e.target.value)} /></div>
        </div>
        <button className="btn" disabled={busy} onClick={annotate}>运行本地调试标注</button>
        <div className="toolbar debug-toolbar"><h3>调试运行记录（{runs.length}）</h3><button className="btn btn-sm" onClick={reload}>刷新</button></div>
        {!runs.length && <div className="empty">暂无调试运行</div>}
        {runs.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead><tr><th>运行编号</th><th>审核摘要</th><th>合并输出</th><th>合并行数</th><th>操作</th></tr></thead>
              <tbody>
                {runs.map((r) => (
                  <tr key={r.run_id}>
                    <td>{r.run_id}</td>
                    <td>{r.has_audit ? <span className="badge badge-green">已生成</span> : <span className="badge badge-gray">未生成</span>}</td>
                    <td>{r.has_merge ? <span className="badge badge-green">已生成</span> : <span className="badge badge-gray">未生成</span>}</td>
                    <td>{r.merge ? r.merge.merged_rows : "-"}</td>
                    <td>
                      <button className="btn btn-sm" disabled={busy} onClick={() => action("audit", { run: r.path }, "生成审核摘要")}>审核摘要</button>{" "}
                      <button className="btn btn-sm" disabled={busy} onClick={() => action("merge", { run: r.path }, "合并调试输出")}>合并输出</button>
                    </td>
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
