import React, { useEffect, useMemo, useState, useCallback } from "react";
import * as api from "./../api.js";
import { Link } from "./../router.jsx";
import {
  annotationJobBatchSummary,
  annotationJobDebugFields,
  annotationJobLabel,
  annotationJobStatusLabel,
  batchPlanDebugFields,
  batchPlanOptionLabel,
  defaultDatasetName,
  displayPlanValue,
  firstDefined,
  formatBatchPlanSummary,
  getBatchPlans,
} from "./batchPlanDisplay.js";

function annotationJobKey(job) {
  return String(firstDefined(job?.annotation_id, job?.argilla_dataset, job?.job_id, job?.id, job?.manifest_path, ""));
}

function statusBadgeClass(label) {
  if (label === "已推送" || label === "已记录") return "badge-green";
  if (label === "执行中") return "badge-blue";
  if (label === "失败") return "badge-red";
  return "badge-gray";
}

function findSamplePathForJob(job, samples) {
  if (!job) return "";
  if (job.sample_path) return job.sample_path;
  const sampleId = String(job.sample_id || "");
  return samples.find((item) => item.sample_id === sampleId)?.path || "";
}

function decisionsForJob(job, decisions) {
  if (!job) return [];
  const annotationId = String(job.annotation_id || "");
  const dataset = String(job.argilla_dataset || "");
  const sampleId = String(job.sample_id || "");
  return decisions.filter((item) => (
    (annotationId && String(item.decision_id || "") === annotationId)
    || (dataset && String(item.argilla_dataset || "") === dataset)
    || (sampleId && String(item.sample_id || "") === sampleId)
  ));
}

function DetailField({ label, value }) {
  const text = value === undefined || value === null || value === "" ? "-" : value;
  return (
    <div>
      <span>{label}</span>
      <strong>{text}</strong>
    </div>
  );
}

export default function RunsPage({ task, taskId, onError }) {
  const [runs, setRuns] = useState([]);
  const [samples, setSamples] = useState([]);
  const [annotationJobs, setAnnotationJobs] = useState([]);
  const [decisions, setDecisions] = useState([]);
  const [agreementAudits, setAgreementAudits] = useState([]);
  const [sample, setSample] = useState("");
  const [batchPlanKey, setBatchPlanKey] = useState("");
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
  const [showCreatePanel, setShowCreatePanel] = useState(false);
  const [selectedJobKey, setSelectedJobKey] = useState("");

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
  const selectedAnnotationJob = annotationJobs.find((item) => annotationJobKey(item) === selectedJobKey) || null;
  const selectedJobDecisions = useMemo(
    () => decisionsForJob(selectedAnnotationJob, decisions),
    [selectedAnnotationJob, decisions],
  );
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

  function openCreatePanel() {
    setSelectedJobKey("");
    if (!sample && samples.length) {
      setSample(samples[samples.length - 1].path);
    }
    setShowCreatePanel(true);
  }

  function openJobDetail(job) {
    setShowCreatePanel(false);
    setSelectedJobKey(annotationJobKey(job));
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
    const ok = await action("argilla_push", {
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
    if (ok) {
      setShowCreatePanel(false);
      setSelectedJobKey(annotationId || dataset);
    }
  }

  async function pushArgillaDirect() {
    const dataset = argillaDataset.trim() || generatedDataset;
    if (!sample || !dataset) { onError("请选择样本集"); return; }
    const ok = await action("argilla_push", {
      sample,
      dataset,
      annotation_id: annotationId || dataset,
      sample_id: selectedSample?.sample_id,
      argilla: { min_submitted: Number(argillaMinSubmitted), if_exists: argillaIfExists },
    }, "直接推送 Argilla");
    if (ok) {
      setShowCreatePanel(false);
      setSelectedJobKey(annotationId || dataset);
    }
  }

  async function pullArgillaForJob(job) {
    const samplePath = findSamplePathForJob(job, samples);
    const dataset = String(job?.argilla_dataset || "").trim();
    const annotation = String(job?.annotation_id || "").trim();
    if (!samplePath || !dataset) {
      onError("该标注任务缺少样本或 Argilla 数据集信息，不能拉回结果");
      return;
    }
    await action("argilla_pull", {
      sample: samplePath,
      sample_id: job.sample_id,
      dataset,
      annotation_id: annotation || undefined,
      decision_id: annotation || dataset,
    }, "拉回标注结果");
  }

  async function runAgreementAuditForDecision(decision, job = selectedAnnotationJob) {
    const samplePath = decision?.sample_path || findSamplePathForJob(job, samples);
    const decisionPath = decision?.path;
    const auditId = String(decision?.decision_id || job?.annotation_id || job?.argilla_dataset || "agreement_v001").trim();
    if (!samplePath || !decisionPath || !auditId) {
      onError("该标注结果缺少样本路径或产物路径，不能运行一致性检查");
      return;
    }
    await action("agreement_audit", {
      sample: samplePath,
      decisions: decisionPath,
      audit_id: auditId,
      min_submitted: Number(firstDefined(job?.min_submitted, job?.argilla?.min_submitted, argillaMinSubmitted, 1)),
    }, "一致性检查");
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
        <p>主视图展示标注任务，新增、拉回和检查动作在任务详情中完成</p>
      </div>

      <div className="card section-card">
        <div className="toolbar">
          <div className="toolbar-stack">
            <h3>标注任务（{annotationJobs.length}）</h3>
            <div className="status-line">每一行是一条 Argilla 标注任务；点击行查看推送状态、批次血缘和后续动作。</div>
          </div>
          <div className="action-row">
            <button className="btn btn-sm" type="button" onClick={reload} disabled={busy}>刷新</button>
            <button className="btn btn-sm btn-primary" type="button" onClick={openCreatePanel}>新增标注任务</button>
          </div>
        </div>
        {!annotationJobs.length && (
          <div className="empty action-empty">
            暂无已推送的 Argilla 标注任务
            <button className="btn btn-primary" type="button" onClick={openCreatePanel}>新增标注任务</button>
          </div>
        )}
        {annotationJobs.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>标注任务</th>
                  <th>Argilla 数据集</th>
                  <th>样本</th>
                  <th>批次方案</th>
                  <th>行数</th>
                  <th>状态</th>
                  <th>创建时间</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {annotationJobs.map((job) => {
                  const key = annotationJobKey(job);
                  const status = annotationJobStatusLabel(job);
                  return (
                    <tr
                      key={key}
                      className={selectedJobKey === key ? "row-selected clickable-row" : "clickable-row"}
                      onClick={() => openJobDetail(job)}
                    >
                      <td><span className="badge badge-blue">{annotationJobLabel(job)}</span></td>
                      <td>{job.argilla_dataset || "-"}</td>
                      <td>{job.sample_id || "-"}</td>
                      <td className="text-cell dispatch-cell">{annotationJobBatchSummary(job)}</td>
                      <td>{job.rows ?? job.result?.records ?? "-"}</td>
                      <td><span className={`badge ${statusBadgeClass(status)}`}>{status}</span></td>
                      <td className="muted">{(job.created_at || "").slice(0, 19)}</td>
                      <td>
                        <button
                          className="btn btn-sm"
                          type="button"
                          onClick={(event) => {
                            event.stopPropagation();
                            openJobDetail(job);
                          }}
                        >
                          详情
                        </button>
                      </td>
                    </tr>
                  );
                })}
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
                    <td><button className="btn btn-sm" disabled={busy} onClick={() => runAgreementAuditForDecision(d)}>运行检查</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="card section-card">
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

      {showCreatePanel && (
        <div className="drawer-backdrop" onClick={() => setShowCreatePanel(false)}>
          <aside className="drawer-panel" onClick={(event) => event.stopPropagation()}>
            <div className="drawer-head">
              <div>
                <h3>新增 Argilla 标注任务</h3>
                <p>选择样本集和批次方案后推送，任务会回到列表中管理。</p>
              </div>
              <button className="btn btn-sm" type="button" onClick={() => setShowCreatePanel(false)}>关闭</button>
            </div>
            <div className="form-grid drawer-form-grid">
              <div className="field field-half">
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
              <div className="field field-half">
                <label>批次方案</label>
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
              <div className="field field-half">
                <label>标注任务编号</label>
                <input value={annotationId} onChange={(e) => setAnnotationId(e.target.value)} placeholder={`${taskId}_label_v1`} />
                <span className="hint">用于本地记录标注结果产物；不填时使用 Argilla 数据集名。</span>
              </div>
              <div className="field field-half">
                <label>Argilla 数据集名</label>
                <input
                  value={argillaDataset}
                  onChange={(e) => { setDatasetAuto(false); setArgillaDataset(e.target.value); }}
                  placeholder={generatedDataset}
                />
                <span className="hint">默认自动生成：{generatedDataset}</span>
              </div>
              <div className="field field-half">
                <label>单条记录所需提交数</label>
                <input type="number" min="1" value={argillaMinSubmitted} onChange={(e) => setArgillaMinSubmitted(e.target.value)} />
              </div>
              <div className="field field-half">
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
            {pushDisabledReason && <div className="status-line danger-line">{pushDisabledReason}</div>}
            {argillaStatus && (
              <div className="status-line">
                Argilla 连接正常：用户 {argillaStatus.user?.username || "-"}，workspace {argillaStatus.workspace}
                {argillaStatus.workspace_exists ? " 已存在" : " 不存在"}；可见 workspace：
                {(argillaStatus.workspaces || []).join(", ") || "-"}
              </div>
            )}
            <div className="drawer-actions">
              <button className="btn btn-primary" disabled={busy || !selectedBatchPlan} onClick={pushArgilla}>推送到 Argilla</button>
              <button className="btn" disabled={busy} onClick={testArgilla}>测试连接</button>
              <button className="btn" disabled={busy} onClick={() => { setDatasetAuto(true); setArgillaDataset(generatedDataset); }}>恢复自动命名</button>
            </div>
            <details className="advanced-panel">
              <summary>高级选项：直接推送整个样本集</summary>
              <p className="muted">默认推送批次计划；仅在需要兼容旧流程时使用整样本直推。</p>
              <button className="btn" disabled={busy || !sample} onClick={pushArgillaDirect}>直接推送整个样本集</button>
            </details>
          </aside>
        </div>
      )}

      {selectedAnnotationJob && (
        <div className="drawer-backdrop" onClick={() => setSelectedJobKey("")}>
          <aside className="drawer-panel" onClick={(event) => event.stopPropagation()}>
            <div className="drawer-head">
              <div>
                <h3>{annotationJobLabel(selectedAnnotationJob)}</h3>
                <p>标注任务详情、批次血缘和后续操作。</p>
              </div>
              <button className="btn btn-sm" type="button" onClick={() => setSelectedJobKey("")}>关闭</button>
            </div>
            <div className="drawer-detail-grid">
              <DetailField label="状态" value={annotationJobStatusLabel(selectedAnnotationJob)} />
              <DetailField label="Argilla 数据集" value={selectedAnnotationJob.argilla_dataset} />
              <DetailField label="样本集" value={selectedAnnotationJob.sample_id} />
              <DetailField label="记录数" value={selectedAnnotationJob.rows ?? selectedAnnotationJob.result?.records} />
              <DetailField label="样本路径" value={findSamplePathForJob(selectedAnnotationJob, samples)} />
              <DetailField label="批次摘要" value={annotationJobBatchSummary(selectedAnnotationJob)} />
            </div>
            <div className="drawer-actions">
              <button className="btn btn-primary" disabled={busy} onClick={() => pullArgillaForJob(selectedAnnotationJob)}>拉回标注结果</button>
              {selectedJobDecisions[0] ? (
                <button className="btn" disabled={busy} onClick={() => runAgreementAuditForDecision(selectedJobDecisions[0], selectedAnnotationJob)}>运行一致性检查</button>
              ) : (
                <button className="btn" disabled>暂无可检查结果</button>
              )}
            </div>
            <div className="info-callout">
              <strong>批次血缘</strong>
              <p>{selectedAnnotationJob.batch_manifest_path || selectedAnnotationJob.batch_plan_id || "该任务未记录批次计划路径。"}</p>
            </div>
            <div className="secondary-panel">
              <div className="toolbar"><h3>关联标注结果（{selectedJobDecisions.length}）</h3></div>
              {!selectedJobDecisions.length && <div className="empty">暂无从该任务拉回的结果</div>}
              {selectedJobDecisions.map((decision) => (
                <div className="resource-mini-row" key={decision.decision_id || decision.path}>
                  <div>
                    <strong>{decision.decision_id || decision.argilla_dataset || "未命名结果"}</strong>
                    <span>{decision.rows ?? decision.result?.responses ?? "-"} 行 · {decision.path}</span>
                  </div>
                  <button className="btn btn-sm" disabled={busy} onClick={() => runAgreementAuditForDecision(decision, selectedAnnotationJob)}>运行检查</button>
                </div>
              ))}
            </div>
            <details className="advanced-panel">
              <summary>高级详情 / 调试信息</summary>
              <div className="debug-field-list">
                {annotationJobDebugFields(selectedAnnotationJob).map(([key, value]) => (
                  <div key={key}><span>{key}</span><strong>{displayPlanValue(value)}</strong></div>
                ))}
              </div>
            </details>
          </aside>
        </div>
      )}
    </div>
  );
}
