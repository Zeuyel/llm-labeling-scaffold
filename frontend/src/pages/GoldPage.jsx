import React, { useEffect, useState, useCallback } from "react";
import * as api from "./../api.js";
import { Link } from "./../router.jsx";

export default function GoldPage({ task, taskId, onError }) {
  const [versions, setVersions] = useState([]);
  const [samples, setSamples] = useState([]);
  const [decisions, setDecisions] = useState([]);
  const [sample, setSample] = useState("");
  const [decision, setDecision] = useState("");
  const [version, setVersion] = useState("");
  const [busy, setBusy] = useState(false);

  const reload = useCallback(async () => {
    if (!taskId) return;
    try {
      const [g, s, d] = await Promise.all([
        api.getTaskGoldVersions(taskId),
        api.getTaskSamples(taskId),
        api.getDecisionArtifacts(taskId),
      ]);
      setVersions(g.gold_versions || []);
      setSamples(s.samples || []);
      setDecisions(d.decision_artifacts || []);
    } catch (e) { onError(String(e)); }
  }, [taskId, onError]);

  useEffect(() => { reload(); }, [reload]);

  const selectedDecision = decisions.find((item) => item.path === decision);

  async function buildGold() {
    const samplePath = sample || selectedDecision?.sample_path;
    if (!task || !samplePath || !decision || !version) {
      onError("请选择样本、标注结果产物并填写版本号");
      return;
    }
    setBusy(true);
    try {
      const job = await api.startAction(task.path, "gold", {
        sample: samplePath,
        decisions: decision,
        version,
      });
      const finished = job?.id ? await api.waitForJob(taskId, job.id) : null;
      if (finished?.status === "failed") {
        throw new Error(finished.error || "执行失败");
      }
      setVersion("");
      await reload();
    } catch (e) { onError(String(e)); } finally { setBusy(false); }
  }

  return (
    <div>
      <div className="crumbs"><Link to="/">全部任务</Link> / <Link to={`/task/${encodeURIComponent(taskId)}`}>{taskId}</Link> / 训练集版本</div>
      <div className="page-header">
        <h2>训练集版本</h2>
        <p>使用样本和 Argilla 标注结果构建可追溯的训练数据版本</p>
      </div>
      <div className="card section-card">
        <h3>构建训练集</h3>
        <div className="form-grid">
          <div className="field">
            <label>样本</label>
            <select value={sample} onChange={(e) => setSample(e.target.value)}>
              <option value="">选择样本</option>
              {samples.map((s) => <option key={s.sample_id} value={s.path}>{s.sample_id}</option>)}
            </select>
            {selectedDecision?.sample_path && <span className="hint">所选标注结果已记录样本路径，可不重复选择</span>}
          </div>
          <div className="field">
            <label>标注结果产物</label>
            <select value={decision} onChange={(e) => setDecision(e.target.value)}>
              <option value="">选择标注结果</option>
              {decisions.map((d) => (
                <option key={d.decision_id || d.path} value={d.path}>
                  {(d.decision_id || d.argilla_dataset || "未命名")} · {d.rows ?? d.result?.responses ?? "-"} 行
                </option>
              ))}
            </select>
          </div>
          <div className="field"><label>版本号</label><input value={version} onChange={(e) => setVersion(e.target.value)} placeholder="例如 v001" /></div>
        </div>
        <button className="btn btn-primary" disabled={busy} onClick={buildGold}>构建训练集版本</button>
      </div>
      <div className="card">
        <div className="toolbar"><h3>训练集版本列表（{versions.length}）</h3><button className="btn btn-sm" onClick={reload}>刷新</button></div>
        {!versions.length && <div className="empty">暂无训练集版本</div>}
        {versions.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead><tr><th>版本</th><th>行数</th><th>主标签</th><th>标签分布</th><th>来源</th><th>创建时间</th><th>路径</th></tr></thead>
              <tbody>
                {versions.map((g) => (
                  <tr key={g.version}>
                    <td><span className="badge badge-blue">{g.version}</span></td>
                    <td>{g.rows}</td>
                    <td>{g.primary_label}</td>
                    <td className="muted text-cell">{JSON.stringify(g.label_counts || {})}</td>
                    <td>{g.source === "decision_artifact" ? "标注结果产物" : (g.source || "-")}</td>
                    <td className="muted">{(g.created_at || "").slice(0, 19)}</td>
                    <td className="muted path-cell">{g.path}</td>
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
