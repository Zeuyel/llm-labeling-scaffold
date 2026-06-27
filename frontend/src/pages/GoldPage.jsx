import React, { useEffect, useMemo, useState, useCallback } from "react";
import * as api from "./../api.js";
import { Link } from "./../router.jsx";
import {
  displayResourceValue,
  goldResourceKey,
  goldSummary,
  goldTrainAction,
} from "./trainingResourceDisplay.js";

function DetailField({ label, value }) {
  return (
    <div>
      <span>{label}</span>
      <strong>{displayResourceValue(value)}</strong>
    </div>
  );
}

function decisionLabel(decision) {
  return decision?.decision_id || decision?.argilla_dataset || "未命名标注结果";
}

function sampleLabel(sample) {
  return sample?.sample_id || sample?.manifest?.sample_id || "未命名样本";
}

export default function GoldPage({ task, taskId, onError }) {
  const [versions, setVersions] = useState([]);
  const [samples, setSamples] = useState([]);
  const [decisions, setDecisions] = useState([]);
  const [sample, setSample] = useState("");
  const [decision, setDecision] = useState("");
  const [version, setVersion] = useState("");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [drawer, setDrawer] = useState("");
  const [selectedGoldKey, setSelectedGoldKey] = useState("");

  const reload = useCallback(async () => {
    if (!taskId) return;
    setLoading(true);
    setLoadError("");
    try {
      const [g, s, d] = await Promise.all([
        api.getTaskGoldVersions(taskId),
        api.getTaskSamples(taskId),
        api.getDecisionArtifacts(taskId),
      ]);
      setVersions(g.gold_versions || []);
      setSamples(s.samples || []);
      setDecisions(d.decision_artifacts || []);
    } catch (e) {
      const message = String(e);
      setLoadError(message);
      onError(message);
    } finally {
      setLoading(false);
    }
  }, [taskId, onError]);

  useEffect(() => { reload(); }, [reload]);

  const selectedDecision = decisions.find((item) => item.path === decision);
  const selectedGold = useMemo(
    () => versions.find((item) => goldResourceKey(item, taskId) === selectedGoldKey),
    [versions, selectedGoldKey, taskId],
  );
  const selectedSummary = selectedGold ? goldSummary(selectedGold, taskId) : null;
  const selectedTrainAction = selectedGold ? goldTrainAction(selectedGold, taskId) : null;
  const selectedSourceSample = selectedGold
    ? samples.find((item) => item.path && item.path === selectedGold.sample_path)
    : null;
  const selectedSourceDecision = selectedGold
    ? decisions.find((item) => item.path && item.path === selectedGold.decisions)
    : null;

  function openBuildDrawer() {
    setDrawer("build");
  }

  function openGoldDetail(item) {
    setSelectedGoldKey(goldResourceKey(item, taskId));
    setDrawer("detail");
  }

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
      setDrawer("");
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

      <div className="card">
        <div className="toolbar">
          <div className="toolbar-stack">
            <h3>Gold versions（{versions.length}）</h3>
            <span className="status-line">点击行查看 manifest、来源血缘和后续动作。</span>
          </div>
          <div className="action-row">
            <button className="btn btn-primary" type="button" disabled={busy} onClick={openBuildDrawer}>构建训练集版本</button>
            <button className="btn btn-sm" type="button" disabled={loading} onClick={reload}>刷新</button>
          </div>
        </div>
        {loading && <div className="status-line">正在读取训练集版本...</div>}
        {!loading && loadError && <div className="empty">读取训练集版本失败，请查看页面错误信息后重试。</div>}
        {!loading && !loadError && !versions.length && (
          <div className="empty action-empty">
            <span>暂无训练集版本</span>
            <button className="btn btn-primary" type="button" onClick={openBuildDrawer}>构建训练集版本</button>
          </div>
        )}
        {!loading && !loadError && versions.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead><tr><th>版本</th><th>行数</th><th>主标签</th><th>标签分布</th><th>来源</th><th>创建时间</th><th>路径</th></tr></thead>
              <tbody>
                {versions.map((g) => {
                  const summary = goldSummary(g, taskId);
                  return (
                    <tr className="clickable-row" key={summary.key} onClick={() => openGoldDetail(g)}>
                      <td><span className="badge badge-blue">{summary.version}</span></td>
                      <td>{summary.rows}</td>
                      <td>{summary.primaryLabel}</td>
                      <td className="muted text-cell">{summary.labelDistribution}</td>
                      <td>{summary.source}</td>
                      <td className="muted">{summary.createdAt}</td>
                      <td className="muted path-cell">{summary.path}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {drawer === "build" && (
        <div className="drawer-backdrop" onClick={() => setDrawer("")}>
          <aside className="drawer-panel" onClick={(event) => event.stopPropagation()}>
            <div className="drawer-head">
              <div>
                <h3>构建训练集版本</h3>
                <p>选择样本和标注结果产物，任务会通过现有 job 流程执行。</p>
              </div>
              <button className="btn btn-sm" type="button" onClick={() => setDrawer("")}>关闭</button>
            </div>
            <div className="form-grid drawer-form-grid">
              <div className="field field-half">
                <label>样本</label>
                <select value={sample} onChange={(e) => setSample(e.target.value)}>
                  <option value="">选择样本</option>
                  {samples.map((s) => <option key={s.sample_id} value={s.path}>{s.sample_id}</option>)}
                </select>
                {selectedDecision?.sample_path && <span className="hint">所选标注结果已记录样本路径，可不重复选择。</span>}
              </div>
              <div className="field field-half">
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
              <div className="field field-half">
                <label>版本号</label>
                <input value={version} onChange={(e) => setVersion(e.target.value)} placeholder="例如 v001" />
              </div>
            </div>
            <div className="drawer-actions">
              <button className="btn btn-primary" disabled={busy} onClick={buildGold}>构建训练集版本</button>
            </div>
          </aside>
        </div>
      )}

      {drawer === "detail" && selectedGold && selectedSummary && (
        <div className="drawer-backdrop" onClick={() => setDrawer("")}>
          <aside className="drawer-panel" onClick={(event) => event.stopPropagation()}>
            <div className="drawer-head">
              <div>
                <h3>Gold {selectedSummary.version}</h3>
                <p>训练集详情、来源血缘和后续训练动作。</p>
              </div>
              <button className="btn btn-sm" type="button" onClick={() => setDrawer("")}>关闭</button>
            </div>
            <div className="drawer-detail-grid">
              <DetailField label="版本" value={selectedSummary.version} />
              <DetailField label="行数" value={selectedSummary.rows} />
              <DetailField label="唯一记录" value={selectedSummary.uniqueIds} />
              <DetailField label="主标签" value={selectedSummary.primaryLabel} />
              <DetailField label="标签分布" value={selectedSummary.labelDistribution} />
              <DetailField label="来源" value={selectedSummary.source} />
              <DetailField label="创建时间" value={selectedSummary.createdAt} />
              <DetailField label="manifest" value={selectedSummary.manifestPath} />
              <DetailField label="路径" value={selectedSummary.path} />
            </div>
            <div className="drawer-actions">
              {selectedTrainAction?.enabled ? (
                <Link className="btn btn-primary" to={`/task/${encodeURIComponent(taskId)}/models`}>去模型训练</Link>
              ) : (
                <button className="btn btn-primary" type="button" disabled>{selectedTrainAction?.reason || "不可训练"}</button>
              )}
              <button className="btn" type="button" onClick={openBuildDrawer}>构建新版本</button>
            </div>
            <div className="secondary-panel">
              <div className="toolbar"><h3>来源</h3></div>
              <div className="resource-mini-row">
                <div>
                  <strong>样本</strong>
                  <span>{selectedSourceSample ? sampleLabel(selectedSourceSample) : "未匹配到样本记录"} · {displayResourceValue(selectedGold.sample_path)}</span>
                </div>
                <Link className="btn btn-sm" to={`/task/${encodeURIComponent(taskId)}/samples`}>查看样本</Link>
              </div>
              <div className="resource-mini-row">
                <div>
                  <strong>标注结果</strong>
                  <span>{selectedSourceDecision ? decisionLabel(selectedSourceDecision) : "未匹配到标注结果记录"} · {displayResourceValue(selectedGold.decisions)}</span>
                </div>
                <Link className="btn btn-sm" to={`/task/${encodeURIComponent(taskId)}/annotations`}>查看标注</Link>
              </div>
              {selectedGold.run_dir && (
                <div className="resource-mini-row">
                  <div>
                    <strong>运行目录</strong>
                    <span>{selectedGold.run_dir}</span>
                  </div>
                  <Link className="btn btn-sm" to={`/task/${encodeURIComponent(taskId)}/runs`}>查看运行</Link>
                </div>
              )}
            </div>
            <details className="advanced-panel">
              <summary>manifest / 调试信息</summary>
              <pre className="log-box">{JSON.stringify(selectedGold, null, 2)}</pre>
            </details>
          </aside>
        </div>
      )}
    </div>
  );
}
