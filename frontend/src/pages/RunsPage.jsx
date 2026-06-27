import React, { useEffect, useMemo, useState, useCallback } from "react";
import * as api from "./../api.js";
import { Link } from "./../router.jsx";
import {
  annotationJobBatchSummary,
  annotationJobDebugFields,
  batchPlanDebugFields,
  batchPlanOptionLabel,
  defaultDatasetName,
  displayPlanValue,
  firstDefined,
  formatBatchPlanSummary,
  getBatchPlans,
} from "./batchPlanDisplay.js";

export default function RunsPage({ task, taskId, onError }) {
  const [runs, setRuns] = useState([]);
  const [samples, setSamples] = useState([]);
  const [annotationJobs, setAnnotationJobs] = useState([]);
  const [decisions, setDecisions] = useState([]);
  const [agreementAudits, setAgreementAudits] = useState([]);
  const [sample, setSample] = useState("");
  const [batchPlanKey, setBatchPlanKey] = useState("");
  const [decisionPath, setDecisionPath] = useState("");
  const [agreementAuditId, setAgreementAuditId] = useState("");
  const [annotationId, setAnnotationId] = useState("");
  const [runId, setRunId] = useState("");
  const [provider, setProvider] = useState("local_stub");
  const [batchSize, setBatchSize] = useState(5);
  const [argillaDataset, setArgillaDataset] = useState("");
  const [argillaMinSubmitted, setArgillaMinSubmitted] = useState(1);
  const [argillaIfExists, setArgillaIfExists] = useState("fail");
  const [argillaStatus, setArgillaStatus] = useState(null);
  const [datasetAuto, setDatasetAuto] = useState(true);
  const [busy, setBusy] = useState(false);

  const reload = useCallback(async () => {
    if (!taskId) return;
    try {
      const [r, s, a, d, q] = await Promise.all([
        api.getTaskRuns(taskId),
        api.getTaskSamples(taskId),
        api.getAnnotationJobs(taskId),
        api.getDecisionArtifacts(taskId),
        api.getAgreementAudits(taskId),
      ]);
      setRuns(r.runs || []);
      setSamples(s.samples || []);
      setAnnotationJobs(a.annotation_jobs || []);
      setDecisions(d.decision_artifacts || []);
      setAgreementAudits(q.agreement_audits || []);
    } catch (e) { onError(String(e)); }
  }, [taskId, onError]);

  useEffect(() => { reload(); }, [reload]);

  const selectedSample = samples.find((item) => item.path === sample);
  const batchPlans = useMemo(() => getBatchPlans(selectedSample), [selectedSample]);
  const selectedBatchPlan = batchPlans.find((item) => item.key === batchPlanKey) || null;
  const selectedDecision = decisions.find((item) => item.path === decisionPath);
  const generatedDataset = defaultDatasetName(taskId, selectedSample?.sample_id, selectedBatchPlan?.plan_id);
  const pushDisabledReason = !sample
    ? "请选择样本集。"
    : !batchPlans.length
      ? "该样本集还没有批次计划，请回样本管理生成批次计划。"
      : !selectedBatchPlan
        ? "请选择批次计划。"
        : "";

  useEffect(() => {
    if (datasetAuto) setArgillaDataset(generatedDataset);
  }, [datasetAuto, generatedDataset]);

  useEffect(() => {
    if (!selectedSample || !batchPlans.length) {
      if (batchPlanKey) setBatchPlanKey("");
      return;
    }
    if (!batchPlans.some((item) => item.key === batchPlanKey)) {
      setBatchPlanKey(batchPlans[0].key);
    }
  }, [selectedSample, batchPlans, batchPlanKey]);

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
    const dataset = argillaDataset.trim() || generatedDataset;
    if (!sample || !dataset) { onError("请选择样本集"); return; }
    if (!selectedBatchPlan) { onError("请先选择批次计划；没有计划时请回样本管理生成批次计划"); return; }
    await action("argilla_push", {
      dispatch_mode: "batch_plan",
      batch_plan_id: selectedBatchPlan.plan_id,
      batch_manifest_path: selectedBatchPlan.manifest_path,
      sample,
      dataset,
      annotation_id: annotationId || dataset,
      sample_id: selectedSample?.sample_id,
      argilla: {
        min_submitted: Number(argillaMinSubmitted),
        if_exists: argillaIfExists,
        record_id_strategy: "batch_scoped",
      },
    }, "推送 Argilla");
  }

  async function pushArgillaDirect() {
    const dataset = argillaDataset.trim() || generatedDataset;
    if (!sample || !dataset) { onError("请选择样本集"); return; }
    await action("argilla_push", {
      sample,
      dataset,
      annotation_id: annotationId || dataset,
      sample_id: selectedSample?.sample_id,
      argilla: { min_submitted: Number(argillaMinSubmitted), if_exists: argillaIfExists },
    }, "直接推送 Argilla");
  }

  async function pullArgilla() {
    const dataset = argillaDataset.trim() || generatedDataset;
    if (!sample || !dataset) { onError("请选择样本"); return; }
    const annotation = annotationId.trim();
    await action("argilla_pull", {
      sample,
      sample_id: selectedSample?.sample_id,
      dataset,
      annotation_id: annotation || undefined,
      decision_id: annotation || dataset,
    }, "拉回标注结果");
  }

  async function runAgreementAudit() {
    const samplePath = selectedDecision?.sample_path || sample;
    const auditId = agreementAuditId.trim() || selectedDecision?.decision_id || annotationId || "agreement_v001";
    if (!samplePath || !decisionPath || !auditId) {
      onError("请选择样本和标注结果产物");
      return;
    }
    await action("agreement_audit", {
      sample: samplePath,
      decisions: decisionPath,
      audit_id: auditId,
      min_submitted: Number(argillaMinSubmitted),
    }, "一致性检查");
  }

  function useAnnotationJob(job) {
    if (!job) return;
    const samplePath = job.sample_path || samples.find((item) => item.sample_id === job.sample_id)?.path || "";
    if (samplePath) setSample(samplePath);
    setBatchPlanKey(String(firstDefined(job.batch_plan_id, job.batch_plan?.plan_id, job.batch_manifest_path, "")));
    setAnnotationId(job.annotation_id || job.argilla_dataset || "");
    setArgillaDataset(job.argilla_dataset || "");
    setDatasetAuto(false);
  }

  function useDecisionArtifact(decision) {
    if (!decision) return;
    setDecisionPath(decision.path || "");
    if (decision.sample_path) setSample(decision.sample_path);
    setAnnotationId(decision.decision_id || decision.argilla_dataset || "");
    setAgreementAuditId(decision.decision_id || decision.argilla_dataset || "");
  }

  async function testArgilla() {
    setBusy(true);
    try {
      const status = await api.getArgillaStatus();
      setArgillaStatus(status);
    } catch (error) {
      setArgillaStatus(null);
      onError(`测试 Argilla 连接: ${error}`);
    } finally {
      setBusy(false);
    }
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
            <label>样本集</label>
            <select value={sample} onChange={(e) => { setSample(e.target.value); setBatchPlanKey(""); }}>
              <option value="">选择样本集</option>
              {samples.map((s) => {
                const planCount = getBatchPlans(s).length;
                return (
                  <option key={s.sample_id} value={s.path}>
                    {s.sample_id}{planCount ? ` · ${planCount} 个批次计划` : " · 无批次计划"}
                  </option>
                );
              })}
            </select>
          </div>
          <div className="field">
            <label>批次计划</label>
            <select
              value={batchPlanKey}
              disabled={!selectedSample || !batchPlans.length}
              onChange={(e) => setBatchPlanKey(e.target.value)}
            >
              <option value="">{selectedSample ? "选择批次计划" : "请先选择样本集"}</option>
              {batchPlans.map((plan, index) => (
                <option key={plan.key} value={plan.key}>
                  {batchPlanOptionLabel(plan, index)}
                </option>
              ))}
            </select>
            <span className="hint">有批次计划时默认选择最新计划。</span>
          </div>
          <div className="field">
            <label>标注任务编号</label>
            <input value={annotationId} onChange={(e) => setAnnotationId(e.target.value)} placeholder={`${taskId}_label_v1`} />
            <span className="hint">用于本地记录标注结果产物；不填时使用 Argilla 数据集名</span>
          </div>
          <div className="field">
            <label>Argilla 数据集名</label>
            <input
              value={argillaDataset}
              onChange={(e) => { setDatasetAuto(false); setArgillaDataset(e.target.value); }}
              placeholder={generatedDataset}
            />
            <span className="hint">默认自动生成：{generatedDataset}</span>
          </div>
          <div className="field">
            <label>单条记录所需提交数</label>
            <input type="number" min="1" value={argillaMinSubmitted} onChange={(e) => setArgillaMinSubmitted(e.target.value)} />
          </div>
          <div className="field">
            <label>同名数据集策略</label>
            <select value={argillaIfExists} onChange={(e) => setArgillaIfExists(e.target.value)}>
              <option value="fail">已存在时报错</option>
              <option value="append">追加记录</option>
              <option value="replace">删除后重建</option>
            </select>
          </div>
        </div>
        {selectedBatchPlan && (
          <>
            <div className="batch-summary-callout">
              <span>执行摘要</span>
              <strong>当前批次方案：{formatBatchPlanSummary(selectedBatchPlan)}</strong>
            </div>
            <details className="advanced-panel compact-details">
              <summary>高级详情 / 调试信息</summary>
              <div className="plan-summary-grid">
                {batchPlanDebugFields(selectedBatchPlan).map(([key, value]) => (
                  <div key={key}><span>{key}</span><strong>{displayPlanValue(value)}</strong></div>
                ))}
              </div>
            </details>
          </>
        )}
        {selectedSample && !selectedBatchPlan && (
          <div className="stage-tip">
            该样本集还没有批次计划，请先<Link to={`/task/${encodeURIComponent(taskId)}/samples`}>回样本管理</Link>生成批次计划。
          </div>
        )}
        <div className="action-row">
          <button className="btn btn-primary" disabled={busy || !selectedBatchPlan} onClick={pushArgilla}>推送到 Argilla</button>
          <button className="btn" disabled={busy} onClick={pullArgilla}>拉回标注结果</button>
          <button className="btn" disabled={busy} onClick={testArgilla}>测试连接</button>
          <button className="btn" disabled={busy} onClick={() => { setDatasetAuto(true); setArgillaDataset(generatedDataset); }}>恢复自动命名</button>
        </div>
        {pushDisabledReason && <div className="status-line danger-line">{pushDisabledReason}</div>}
        {argillaStatus && (
          <div className="status-line">
            Argilla 连接正常：用户 {argillaStatus.user?.username || "-"}，workspace {argillaStatus.workspace}
            {argillaStatus.workspace_exists ? " 已存在" : " 不存在"}；可见 workspace：
            {(argillaStatus.workspaces || []).join(", ") || "-"}
          </div>
        )}
        <details className="advanced-panel">
          <summary>高级选项：直接推送整个样本集</summary>
          <p className="muted">默认推送批次计划；仅在需要兼容旧流程时使用整样本直推。</p>
          <button className="btn" disabled={busy || !sample} onClick={pushArgillaDirect}>直接推送整个样本集</button>
        </details>
      </div>
      <div className="card section-card">
        <div className="toolbar"><h3>已推送标注任务（{annotationJobs.length}）</h3><button className="btn btn-sm" onClick={reload}>刷新</button></div>
        {!annotationJobs.length && <div className="empty">暂无已推送的 Argilla 标注任务</div>}
        {annotationJobs.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead><tr><th>标注任务编号</th><th>Argilla 数据集</th><th>样本</th><th>批次计划</th><th>行数</th><th>状态</th><th>创建时间</th><th>操作</th></tr></thead>
              <tbody>
                {annotationJobs.map((job) => (
                  <tr key={job.annotation_id || job.argilla_dataset}>
                    <td><span className="badge badge-blue">{job.annotation_id || "-"}</span></td>
                    <td>{job.argilla_dataset || "-"}</td>
                    <td>{job.sample_id || "-"}</td>
                    <td className="text-cell dispatch-cell">
                      <span>{annotationJobBatchSummary(job)}</span>
                      <details className="inline-details">
                        <summary>详情</summary>
                        <div className="debug-field-list">
                          {annotationJobDebugFields(job).map(([key, value]) => (
                            <div key={key}><span>{key}</span><strong>{displayPlanValue(value)}</strong></div>
                          ))}
                        </div>
                      </details>
                    </td>
                    <td>{job.rows ?? job.result?.records ?? "-"}</td>
                    <td>{job.status || "-"}</td>
                    <td className="muted">{(job.created_at || "").slice(0, 19)}</td>
                    <td><button className="btn btn-sm" disabled={busy} onClick={() => useAnnotationJob(job)}>用于回收</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
      <div className="card section-card">
        <div className="toolbar"><h3>标注结果产物（{decisions.length}）</h3><button className="btn btn-sm" onClick={reload}>刷新</button></div>
        {!decisions.length && <div className="empty">暂无标注结果产物</div>}
        {decisions.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead><tr><th>产物编号</th><th>来源</th><th>Argilla 数据集</th><th>样本</th><th>行数</th><th>存储路径</th><th>操作</th></tr></thead>
              <tbody>
                {decisions.map((d) => (
                  <tr key={d.decision_id || d.path}>
                    <td><span className="badge badge-blue">{d.decision_id || "-"}</span></td>
                    <td>{d.source === "argilla" ? "Argilla" : (d.source || "-")}</td>
                    <td>{d.argilla_dataset || "-"}</td>
                    <td>{d.sample_id || "-"}</td>
                    <td>{d.rows ?? d.result?.responses ?? "-"}</td>
                    <td className="muted path-cell">{d.path}</td>
                    <td><button className="btn btn-sm" disabled={busy} onClick={() => useDecisionArtifact(d)}>用于检查</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
      <div className="card section-card">
        <h3>一致性检查</h3>
        <div className="form-grid">
          <div className="field">
            <label>样本</label>
            <select value={sample} onChange={(e) => setSample(e.target.value)}>
              <option value="">选择样本</option>
              {samples.map((s) => <option key={s.sample_id} value={s.path}>{s.sample_id}</option>)}
            </select>
          </div>
          <div className="field">
            <label>标注结果产物</label>
            <select value={decisionPath} onChange={(e) => setDecisionPath(e.target.value)}>
              <option value="">选择标注结果</option>
              {decisions.map((d) => (
                <option key={d.decision_id || d.path} value={d.path}>
                  {(d.decision_id || d.argilla_dataset || "未命名")} · {d.rows ?? d.result?.responses ?? "-"} 行
                </option>
              ))}
            </select>
            {selectedDecision?.sample_path && <span className="hint">所选标注结果已记录样本路径。</span>}
          </div>
          <div className="field">
            <label>检查编号</label>
            <input value={agreementAuditId} onChange={(e) => setAgreementAuditId(e.target.value)} placeholder={selectedDecision?.decision_id || "agreement_v001"} />
          </div>
        </div>
        <button className="btn btn-primary" disabled={busy || !decisions.length} onClick={runAgreementAudit}>运行一致性检查</button>
        <div className="toolbar debug-toolbar"><h3>检查记录（{agreementAudits.length}）</h3><button className="btn btn-sm" onClick={reload}>刷新</button></div>
        {!agreementAudits.length && <div className="empty">暂无一致性检查记录</div>}
        {agreementAudits.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead><tr><th>检查编号</th><th>结果</th><th>样本数</th><th>覆盖数</th><th>低于提交数</th><th>缺主标签</th><th>标签分布</th><th>摘要路径</th></tr></thead>
              <tbody>
                {agreementAudits.map((item) => (
                  <tr key={item.audit_id || item.summary_path}>
                    <td><span className="badge badge-blue">{item.audit_id || "-"}</span></td>
                    <td>{item.passed ? <span className="badge badge-green">通过</span> : <span className="badge badge-red">未通过</span>}</td>
                    <td>{item.sample_unique_ids ?? "-"}</td>
                    <td>{item.sample_coverage?.covered_ids ?? "-"}</td>
                    <td>{item.issue_counts?.below_min_submitted_ids ?? "-"}</td>
                    <td>{item.issue_counts?.primary_label_missing ?? "-"}</td>
                    <td className="muted text-cell">{JSON.stringify(item.label_distribution || {})}</td>
                    <td className="muted path-cell">{item.summary_path}</td>
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
