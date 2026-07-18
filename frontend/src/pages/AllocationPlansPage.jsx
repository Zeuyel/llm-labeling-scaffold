import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as api from "../api.js";
import { Link } from "../router.jsx";
import {
  ALLOCATION_STRATEGIES,
  COLLECTION_LABELS,
  allocationFormAvailability,
  applyTrustedSource,
  collectionLabel,
  confirmAvailability,
  emptyAllocationForm,
  formFromAllocationPlan,
  allocationPlanPayload,
  allocationPreviewPayload,
  editAvailability,
  modeLabel,
  normalizeAssignments,
  normalizePreview,
  phaseLabel,
  planId,
  planStatusLabel,
  previewBlockingMessages,
  progressPercent,
  previewAvailability,
  statusBadgeClass,
  strategyLabel,
  trustedAllocationSourceOptions,
  valueOrDash,
} from "./allocationPlanState.js";

function firstValue(...values) {
  return values.find((value) => value !== undefined && value !== null && value !== "");
}

function extractPlan(data, fallbackId = "") {
  const candidate = data?.plan || data?.allocation_plan || (data?.plan_id || data?.id ? data : null);
  if (!candidate) return null;
  return planId(candidate) ? candidate : { ...candidate, plan_id: fallbackId };
}

function extractPlans(data) {
  if (Array.isArray(data)) return data;
  const values = data?.plans || data?.allocation_plans || data?.items;
  if (Array.isArray(values)) return values;
  throw new Error("服务端返回的分配计划列表格式不受支持");
}

function extractAssignments(data) {
  if (Array.isArray(data)) return normalizeAssignments(data);
  if (Array.isArray(data?.assignments) || Array.isArray(data?.items)) return normalizeAssignments(data);
  throw new Error("服务端返回的分配明细格式不受支持");
}

function taskWorkspace(task) {
  return String(firstValue(task?.workspace, task?.workspace_slug, "") || "").trim();
}

function taskRevision(task) {
  return String(firstValue(task?.revision_id, task?.revision, task?.active_revision_id, "") || "").trim();
}

function planWorkspace(plan) {
  return firstValue(
    plan?.remote_workspace_uuid,
    plan?.workspace_uuid,
    plan?.argilla_workspace_uuid,
    plan?.workspace_id,
    plan?.workspace,
    "",
  );
}

function planDataset(plan) {
  return firstValue(
    plan?.remote_dataset_uuid,
    plan?.dataset_uuid,
    plan?.argilla_dataset_uuid,
    plan?.dataset_id,
    plan?.dataset,
    "",
  );
}

function planProgress(plan) {
  return plan?.progress || plan?.progress_summary || plan?.execution_progress || {};
}

function progressText(progress) {
  const accepted = firstValue(progress?.accepted, progress?.submitted, progress?.completed);
  const total = firstValue(progress?.total, progress?.expected, progress?.assignment_total);
  if (accepted !== undefined || total !== undefined) return `${valueOrDash(accepted)} / ${valueOrDash(total)}`;
  return `${Math.round(progressPercent(progress))}%`;
}

function operationClass(operation) {
  if (!operation?.state) return "";
  return `allocation-operation allocation-operation-${operation.state}`;
}

function DetailField({ label, value }) {
  return (
    <div>
      <span>{label}</span>
      <strong>{valueOrDash(value)}</strong>
    </div>
  );
}

function PreviewIssueList({ title, items, className = "" }) {
  if (!items?.length) return null;
  return (
    <div className={`allocation-issue-list ${className}`}>
      <strong>{title}</strong>
      <ul>
        {items.map((item, index) => (
          <li key={`${item?.code || item?.message || item}-${index}`}>
            {typeof item === "string" ? item : item?.message || item?.code || "未说明的问题"}
          </li>
        ))}
      </ul>
    </div>
  );
}

function PreviewSummary({ preview }) {
  const data = normalizePreview(preview);
  const overlapCells = Array.isArray(data.overlap?.cells) ? data.overlap.cells : [];
  const poolRequirements = Array.isArray(data.overlap?.pool_requirements) ? data.overlap.pool_requirements : [];
  return (
    <section className="allocation-preview" aria-labelledby="allocation-preview-title">
      <div className="toolbar">
        <div className="toolbar-stack">
          <h3 id="allocation-preview-title">只读预览摘要</h3>
          <div className="status-line">以下内容来自后端预览响应，确认前不会创建远端资源。</div>
        </div>
        <span className={`badge ${data.ready ? "badge-green" : "badge-red"}`}>{data.ready ? "预览通过" : "存在阻塞"}</span>
      </div>
      <div className="plan-summary-grid allocation-preview-summary">
        <DetailField label="策略" value={strategyLabel(data.strategy)} />
        <DetailField label="标注人员组" value={data.cohort_id} />
        <DetailField label="来源清单" value={data.source_manifest?.manifest_id} />
        <DetailField label="计划指纹" value={data.plan_fingerprint} />
        <DetailField label="输入指纹" value={data.input_fingerprint} />
        <DetailField label="算法版本" value={data.algorithm_version} />
      </div>

      <PreviewIssueList title="阻塞错误" items={data.blocking_errors} className="allocation-blocking-errors" />
      <PreviewIssueList title="提示" items={data.warnings} className="allocation-warnings" />

      <div className="allocation-preview-section">
        <h4>每人行数</h4>
        {!data.annotator_loads.length && <div className="empty">后端未返回人员负载。</div>}
        {data.annotator_loads.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead><tr><th>标注人员</th><th>模式</th><th>容量</th><th>总行数</th><th>校准</th><th>正式</th><th>剩余容量</th></tr></thead>
              <tbody>
                {data.annotator_loads.map((item, index) => (
                  <tr key={`${item.annotator_id || "unknown"}-${index}`}>
                    <td>{valueOrDash(item.annotator_id)}</td>
                    <td>{modeLabel(item.mode)}</td>
                    <td>{valueOrDash(item.capacity)}</td>
                    <td>{valueOrDash(item.assigned_rows)}</td>
                    <td>{valueOrDash(item.calibration_rows)}</td>
                    <td>{valueOrDash(item.production_rows)}</td>
                    <td>{valueOrDash(item.remaining_capacity)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="allocation-preview-section">
        <h4>重叠矩阵</h4>
        {!overlapCells.length && !poolRequirements.length && <div className="empty">没有重叠记录。</div>}
        {overlapCells.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead><tr><th>阶段</th><th>人员 A</th><th>人员 B</th><th>重叠行数</th></tr></thead>
              <tbody>
                {overlapCells.map((item, index) => (
                  <tr key={`${item.annotator_a}-${item.annotator_b}-${index}`}>
                    <td>{phaseLabel(item.phase)}</td>
                    <td>{valueOrDash(item.annotator_a)}</td>
                    <td>{valueOrDash(item.annotator_b)}</td>
                    <td>{valueOrDash(item.count)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {poolRequirements.length > 0 && (
          <div className="table-wrap allocation-preview-table-spaced">
            <table>
              <thead><tr><th>阶段</th><th>队列</th><th>要求提交数</th><th>记录数</th></tr></thead>
              <tbody>
                {poolRequirements.map((item, index) => (
                  <tr key={`${item.pool_id}-${index}`}>
                    <td>{phaseLabel(item.phase)}</td>
                    <td>{valueOrDash(item.pool_id)}</td>
                    <td>{valueOrDash(item.required_submissions)}</td>
                    <td>{valueOrDash(item.record_count)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="allocation-preview-section">
        <h4>工作区与数据集计划</h4>
        {!data.workspace_requirements.length && !data.dataset_requirements.length && <div className="empty">后端未返回远端资源计划。</div>}
        {data.workspace_requirements.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead><tr><th>工作区组</th><th>阶段</th><th>模式</th><th>人员组</th><th>负责人</th><th>成员</th></tr></thead>
              <tbody>
                {data.workspace_requirements.map((item, index) => (
                  <tr key={`${item.workspace_group_id || "unknown"}-${index}`}>
                    <td className="mono-cell">{valueOrDash(item.workspace_group_id)}</td>
                    <td>{phaseLabel(item.phase)}</td>
                    <td>{modeLabel(item.mode)}</td>
                    <td>{valueOrDash(item.cohort_id)}</td>
                    <td>{valueOrDash(item.assignee_id)}</td>
                    <td className="text-cell">{Array.isArray(item.member_annotator_ids) ? item.member_annotator_ids.join("、") : valueOrDash(item.member_annotator_ids)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {data.dataset_requirements.length > 0 && (
          <div className="table-wrap allocation-preview-table-spaced">
            <table>
              <thead><tr><th>数据集组</th><th>工作区组</th><th>阶段</th><th>最少提交</th><th>负责人/队列</th><th>记录数</th><th>批次数</th></tr></thead>
              <tbody>
                {data.dataset_requirements.map((item, index) => (
                  <tr key={`${item.dataset_group_id || "unknown"}-${index}`}>
                    <td className="mono-cell">{valueOrDash(item.dataset_group_id)}</td>
                    <td className="mono-cell">{valueOrDash(item.workspace_group_id)}</td>
                    <td>{phaseLabel(item.phase)}</td>
                    <td>{valueOrDash(item.min_submitted)}</td>
                    <td>{valueOrDash(item.assignee_id || item.pool_id)}</td>
                    <td>{valueOrDash(item.record_count)}</td>
                    <td>{valueOrDash(item.batch_count)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </section>
  );
}

function ProgressPanel({ progress, error, onRetry, busy }) {
  const hasProgressData = progress && typeof progress === "object" && [
    "percent", "percentage", "completed_percent", "accepted", "submitted", "completed",
    "quarantined", "quarantine_count", "total", "expected", "assignment_total", "phase",
  ].some((key) => progress[key] !== undefined);
  const percent = progressPercent(progress);
  const accepted = firstValue(progress?.accepted, progress?.submitted, progress?.completed);
  const quarantined = firstValue(progress?.quarantined, progress?.quarantine_count);
  const total = firstValue(progress?.total, progress?.expected, progress?.assignment_total);
  if (error) return <div className="empty allocation-section-error">进度读取失败：{error}<button className="btn btn-sm" type="button" onClick={onRetry} disabled={busy}>重试</button></div>;
  if (!hasProgressData) return <div className="empty allocation-section-error">服务端暂未返回执行进度。<button className="btn btn-sm" type="button" onClick={onRetry} disabled={busy}>重试</button></div>;
  return (
    <section className="allocation-progress allocation-preview-section">
      <div className="toolbar">
        <h4>执行进度</h4>
        <strong>{Math.round(percent)}%</strong>
      </div>
      <div className="allocation-progress-track" aria-label={`执行进度 ${Math.round(percent)}%`}>
        <span style={{ width: `${percent}%` }} />
      </div>
      <div className="plan-summary-grid allocation-progress-summary">
        <DetailField label="已接收" value={accepted} />
        <DetailField label="已隔离" value={quarantined} />
        <DetailField label="总任务量" value={total} />
        <DetailField label="当前阶段" value={phaseLabel(progress?.phase)} />
      </div>
    </section>
  );
}

function AllocationForm({
  form,
  setForm,
  trustedSources,
  sourceLoading,
  sourceError,
  onReloadSources,
  onSourceChange,
  formAvailability,
  busyAction,
  onPreview,
  onSave,
  onClose,
  preview,
  editing,
}) {
  const selectedSource = trustedSources.find((item) => item.key === form.trusted_source_key);
  const actionDisabled = Boolean(busyAction) || !formAvailability.enabled;
  const saveDisabled = actionDisabled || !preview?.ready || !preview?.plan_fingerprint;
  return (
    <div className="drawer-backdrop" onClick={onClose}>
      <aside className="drawer-panel drawer-panel-wide" onClick={(event) => event.stopPropagation()} role="dialog" aria-modal="true" aria-labelledby="allocation-form-title">
        <div className="drawer-head">
          <div>
            <h3 id="allocation-form-title">{editing ? "编辑分配配置" : "新建分配配置"}</h3>
            <p>只提交分配配置和非敏感标识；密码、API key 不进入此表单。</p>
          </div>
          <button className="btn btn-sm" type="button" onClick={onClose} disabled={Boolean(busyAction)}>关闭</button>
        </div>

        <form onSubmit={onSave}>
          <div className="form-grid drawer-form-grid">
            <label className="field"><span>Scaffold 工作区</span><input value={form.workspace} readOnly /></label>
            <label className="field"><span>任务编号</span><input value={form.task_id} readOnly /></label>
            <label className="field"><span>任务版本</span><input value={form.revision_id} readOnly /></label>
            <label className="field"><span>来源清单</span><input value={form.source_manifest_id} readOnly placeholder="请选择后端可信来源" /></label>
            <label className="field"><span>来源类型</span><input value={form.source_manifest_kind === "batch" ? "批次" : "样本"} readOnly /></label>
            <label className="field field-wide"><span>可信样本/批次</span><select value={form.trusted_source_key} onChange={(event) => onSourceChange(event.target.value)} disabled={sourceLoading || Boolean(busyAction) || !trustedSources.some((item) => item.available)}><option value="">{trustedSources.length ? "请选择后端返回的可信来源" : "暂无可用来源"}</option>{trustedSources.map((item) => <option key={item.key} value={item.key} disabled={!item.available}>{item.label}{item.available ? "" : "（不可用）"}</option>)}</select></label>
          </div>
          <div className="status-line">任务 revision、revision hash、manifest hash 和记录内容只接受后端返回的可信来源，不能在此手工填写。</div>
          {sourceLoading && <div className="status-line">正在读取任务、样本和批次来源...</div>}
          {sourceError && <div className="stage-tip allocation-list-error">{sourceError}<button className="btn btn-sm" type="button" onClick={onReloadSources} disabled={Boolean(busyAction) || sourceLoading}>重试</button></div>}
          {!sourceLoading && !sourceError && !trustedSources.length && <div className="empty">暂无可用来源</div>}
          {selectedSource && !selectedSource.available && <div className="stage-tip">当前来源不可用：{selectedSource.disabled_reason}</div>}

          <div className="allocation-form-section">
            <div className="toolbar-stack"><h4>分配策略</h4><div className="status-line">策略由后端规划器解释，前端不计算分配结果。</div></div>
            <div className="tabs allocation-strategy-tabs" role="group" aria-label="分配策略">
              {ALLOCATION_STRATEGIES.map((item) => (
                <button
                  key={item.value}
                  className={form.strategy === item.value ? "tab active" : "tab"}
                  type="button"
                  aria-pressed={form.strategy === item.value}
                  onClick={() => setForm((current) => ({ ...current, strategy: item.value }))}
                >
                  {item.label}
                </button>
              ))}
            </div>
            <p className="status-line allocation-strategy-help">{ALLOCATION_STRATEGIES.find((item) => item.value === form.strategy)?.description}</p>
          </div>

          <div className="allocation-form-section">
            <div className="toolbar">
              <div className="toolbar-stack"><h4>人员组与标注人员</h4><div className="status-line">此入口依赖 #66 专用 API，当前版本未接入。</div></div>
            </div>
            {!form.annotators.length && <div className="empty">暂无可用人员组或标注人员</div>}
            {form.annotators.length > 0 && (
              <div className="table-wrap">
                <table>
                  <thead><tr><th>人员组</th><th>标注人员</th><th>容量</th></tr></thead>
                  <tbody>{form.annotators.map((item, index) => <tr key={`${item.annotator_id || "unknown"}-${index}`}><td>{valueOrDash(item.cohort_id || form.cohort_id)}</td><td>{valueOrDash(item.annotator_id)}</td><td>{valueOrDash(item.capacity)}</td></tr>)}</tbody>
                </table>
              </div>
            )}
          </div>

          <div className="form-grid drawer-form-grid">
            <label className="field"><span>最少提交数</span><input type="number" min="1" value={form.min_submitted} onChange={(event) => setForm((current) => ({ ...current, min_submitted: event.target.value }))} /></label>
            <label className="field"><span>随机种子</span><input type="number" value={form.seed} onChange={(event) => setForm((current) => ({ ...current, seed: event.target.value }))} /></label>
            <label className="field field-wide"><span>算法版本</span><input value={form.algorithm_version} onChange={(event) => setForm((current) => ({ ...current, algorithm_version: event.target.value }))} /></label>
          </div>

          <div className="allocation-form-section">
            <label className="checkbox-inline"><input type="checkbox" checked={form.calibration_enabled} onChange={(event) => setForm((current) => ({ ...current, calibration_enabled: event.target.checked }))} />启用校准质量门</label>
            {form.calibration_enabled && (
              <div className="form-grid drawer-form-grid allocation-calibration-fields">
                <label className="field"><span>校准记录数</span><input type="number" min="1" value={form.calibration_count} onChange={(event) => setForm((current) => ({ ...current, calibration_count: event.target.value }))} /></label>
                <label className="field"><span>重叠要求提交数</span><input type="number" min="2" value={form.overlap_required_submissions} onChange={(event) => setForm((current) => ({ ...current, overlap_required_submissions: event.target.value }))} /></label>
                <label className="field field-wide"><span>校准人员编号</span><textarea rows="2" value={form.calibration_annotator_ids} onChange={(event) => setForm((current) => ({ ...current, calibration_annotator_ids: event.target.value }))} placeholder="每行一个编号" /></label>
              </div>
            )}
          </div>

          {preview && <PreviewSummary preview={preview} />}
          <div className="drawer-actions">
            <button className="btn btn-primary" type="submit" disabled={saveDisabled} title={saveDisabled && !preview?.plan_fingerprint ? "请先生成通过后端检查的只读预览" : formAvailability.disabledReason}>{busyAction === "save" ? "保存中..." : editing ? "保存配置" : "保存草稿"}</button>
            <button className="btn btn-accent" type="button" onClick={onPreview} disabled={actionDisabled} title={formAvailability.disabledReason}>{busyAction === "preview" ? "预览中..." : "生成只读预览"}</button>
            <button className="btn" type="button" onClick={onClose} disabled={Boolean(busyAction)}>取消</button>
          </div>
        </form>
      </aside>
    </div>
  );
}

export default function AllocationPlansPage({ task, taskId, onError }) {
  const [plans, setPlans] = useState([]);
  const [listLoading, setListLoading] = useState(false);
  const [listError, setListError] = useState("");
  const [drawer, setDrawer] = useState("");
  const [selectedPlan, setSelectedPlan] = useState(null);
  const [detailPlan, setDetailPlan] = useState(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState("");
  const [progress, setProgress] = useState(null);
  const [progressError, setProgressError] = useState("");
  const [assignments, setAssignments] = useState([]);
  const [assignmentError, setAssignmentError] = useState("");
  const [assignmentFilter, setAssignmentFilter] = useState("all");
  const [selectedAssignment, setSelectedAssignment] = useState(null);
  const [form, setForm] = useState(() => emptyAllocationForm(task, taskId));
  const [editingPlanId, setEditingPlanId] = useState("");
  const [trustedSources, setTrustedSources] = useState([]);
  const [sourceLoading, setSourceLoading] = useState(false);
  const [sourceError, setSourceError] = useState("");
  const [preview, setPreview] = useState(null);
  const [operation, setOperation] = useState({ state: "", message: "" });
  const [busyAction, setBusyAction] = useState("");
  const busyRef = useRef("");

  const scope = useMemo(() => ({
    workspace: taskWorkspace(task),
    taskId,
    revisionId: taskRevision(task),
  }), [task, taskId]);

  const reload = useCallback(async () => {
    if (!taskId) return false;
    setListLoading(true);
    setListError("");
    try {
      const data = await api.getAllocationPlans(scope);
      setPlans(extractPlans(data));
      return true;
    } catch (error) {
      const message = `分配计划列表读取失败：${String(error)}`;
      setListError(message);
      onError?.(message);
      return false;
    } finally {
      setListLoading(false);
    }
  }, [onError, scope, taskId]);

  useEffect(() => { reload(); }, [reload]);
  useEffect(() => {
    setForm(emptyAllocationForm(task, taskId));
    setTrustedSources([]);
    setSourceError("");
  }, [task, taskId]);

  const updateForm = useCallback((updater) => {
    setForm(updater);
    setPreview(null);
  }, []);

  async function runOperation(action, label, callback) {
    if (busyRef.current) {
      const message = "上一操作正在执行，请勿重复提交。";
      setOperation({ state: "error", message });
      return null;
    }
    busyRef.current = action;
    setBusyAction(action);
    setOperation({ state: "loading", message: `${label}...` });
    try {
      const result = await callback();
      setOperation({ state: "success", message: `${label}成功。` });
      return result;
    } catch (error) {
      const message = `${label}失败：${String(error)}`;
      setOperation({ state: "error", message });
      onError?.(message);
      return null;
    } finally {
      busyRef.current = "";
      setBusyAction("");
    }
  }

  async function loadTrustedSources() {
    if (!taskId) return [];
    const workspace = taskWorkspace(task);
    if (!workspace) {
      const message = "任务工作区不可用，无法读取可信来源。";
      setSourceError(message);
      setTrustedSources([]);
      return [];
    }
    setSourceLoading(true);
    setSourceError("");
    try {
      const [taskData, samplesData] = await Promise.all([
        api.getTaskControl(taskId, workspace),
        api.getTaskSamples(taskId, workspace),
      ]);
      const controlTask = taskData?.task || taskData;
      const samples = Array.isArray(samplesData)
        ? samplesData
        : samplesData?.samples || samplesData?.items || [];
      const next = trustedAllocationSourceOptions(samples, controlTask);
      setTrustedSources(next);
      setForm((current) => {
        const selected = next.find((item) => item.key === current.trusted_source_key && item.available);
        return selected ? applyTrustedSource(current, selected) : current;
      });
      return next;
    } catch (error) {
      const message = `可信来源读取失败：${String(error)}`;
      setSourceError(message);
      setTrustedSources([]);
      return [];
    } finally {
      setSourceLoading(false);
    }
  }

  function selectTrustedSource(sourceKey) {
    const source = trustedSources.find((item) => item.key === sourceKey);
    if (!source?.available) {
      setOperation({ state: "error", message: source?.disabled_reason || "暂无可用来源。" });
      return;
    }
    setForm((current) => applyTrustedSource(current, source));
    setPreview(null);
  }

  function openCreate() {
    setEditingPlanId("");
    setDetailPlan(null);
    setSelectedPlan(null);
    setPreview(null);
    setSourceError("");
    setTrustedSources([]);
    setForm(emptyAllocationForm(task, taskId));
    setDrawer("create");
    loadTrustedSources();
  }

  async function loadDetail(plan) {
    const id = planId(plan);
    if (!id) {
      const message = "服务端返回的分配计划缺少编号，无法打开详情。";
      setDetailError(message);
      onError?.(message);
      return false;
    }
    setDrawer("detail");
    setSelectedPlan(plan);
    setDetailPlan(null);
    setDetailError("");
    setProgress(null);
    setProgressError("");
    setAssignments([]);
    setAssignmentError("");
    setSelectedAssignment(null);
    setAssignmentFilter("all");
    setDetailLoading(true);
    try {
      const data = await api.getAllocationPlan(id, scope);
      const next = extractPlan(data, id);
      if (!next) throw new Error("服务端未返回分配计划详情");
      setDetailPlan(next);
      setSelectedPlan(next);
      setForm(formFromAllocationPlan(next, task, taskId));
      setPreview(next.preview || next.preview_summary || null);
      const [progressResult, assignmentResult] = await Promise.allSettled([
        api.getAllocationPlanProgress(id, scope),
        api.getAllocationPlanAssignments(id, scope),
      ]);
      if (progressResult.status === "fulfilled") {
        setProgress(progressResult.value?.progress || progressResult.value || {});
      } else {
        setProgressError(String(progressResult.reason));
      }
      if (assignmentResult.status === "fulfilled") {
        setAssignments(extractAssignments(assignmentResult.value));
      } else {
        setAssignmentError(String(assignmentResult.reason));
      }
      return true;
    } catch (error) {
      const message = `分配计划详情读取失败：${String(error)}`;
      setDetailError(message);
      onError?.(message);
      return false;
    } finally {
      setDetailLoading(false);
    }
  }

  function closeDrawer() {
    if (busyAction) return;
    setDrawer("");
    setSelectedPlan(null);
    setDetailPlan(null);
  }

  function validateForm(currentForm) {
    const availability = allocationFormAvailability({
      form: currentForm,
      sourceLoading,
      sourceError,
      busy: Boolean(busyAction),
    });
    if (!availability.enabled) return availability.disabledReason;
    if (!String(currentForm.workspace || "").trim() || !String(currentForm.task_id || "").trim() || !String(currentForm.revision_id || "").trim()) return "可信来源缺少完整任务范围。";
    if (!currentForm.annotators.length) return "至少选择一名已有标注人员。";
    if (currentForm.annotators.some((item) => !item.annotator_id || !item.cohort_id || item.capacity === "" || !Number.isFinite(Number(item.capacity)) || Number(item.capacity) < 0)) return "请补全标注人员、人员组和容量。";
    return "";
  }

  async function previewCurrent() {
    let payload;
    try {
      payload = allocationPreviewPayload(form);
    } catch (error) {
      setOperation({ state: "error", message: String(error) });
      return;
    }
    await runOperation("preview", "只读预览", async () => {
      const data = await api.previewAllocation(payload);
      const next = normalizePreview(data);
      setPreview(next);
      setDetailPlan((current) => current ? { ...current, preview: next, preview_request: payload } : current);
      return next;
    });
  }

  async function savePlan(event) {
    event.preventDefault();
    const validationError = validateForm(form);
    if (validationError) {
      setOperation({ state: "error", message: validationError });
      return;
    }
    const currentPreview = normalizePreview(preview);
    if (!preview || !currentPreview.ready || !currentPreview.plan_fingerprint) {
      setOperation({ state: "error", message: previewBlockingMessages(currentPreview)[0] || "请先生成通过后端检查的只读预览。" });
      return;
    }
    let payload;
    try {
      payload = allocationPlanPayload(form, preview);
    } catch (error) {
      setOperation({ state: "error", message: String(error) });
      return;
    }
    const fingerprint = currentPreview.plan_fingerprint;
    const idempotencyKey = editingPlanId
      ? api.allocationPlanUpdateIdempotencyKey(editingPlanId, fingerprint)
      : api.allocationPlanCreateIdempotencyKey(form.workspace, form.task_id, fingerprint);
    const result = await runOperation("save", editingPlanId ? "配置保存" : "草稿保存", async () => {
      const data = editingPlanId
        ? await api.updateAllocationPlan(editingPlanId, payload, { idempotencyKey })
        : await api.createAllocationPlan(payload, { idempotencyKey });
      const next = extractPlan(data, editingPlanId);
      if (!next || !planId(next)) throw new Error("服务端未返回分配计划编号，不能显示成功状态。");
      setEditingPlanId(planId(next));
      setDetailPlan(next);
      setSelectedPlan(next);
      setPreview(next.preview || next.preview_summary || null);
      if (!(await reload())) throw new Error("配置已提交，但分配计划列表刷新失败。");
      return next;
    });
    if (result) setDrawer("detail");
  }

  async function confirmPlan() {
    const plan = detailPlan || selectedPlan;
    const gate = confirmAvailability({ plan, preview, busy: Boolean(busyAction) });
    if (!gate.enabled) {
      setOperation({ state: "error", message: gate.disabledReason });
      return;
    }
    const id = planId(plan);
    const fingerprint = preview?.plan_fingerprint || plan?.plan_fingerprint || "";
    const idempotencyKey = api.allocationPlanConfirmIdempotencyKey(id, fingerprint);
    const result = await runOperation("confirm", "计划确认", async () => {
      const data = await api.confirmAllocationPlan(id, {
        plan_fingerprint: fingerprint,
      }, { idempotencyKey });
      if (!(await reload())) throw new Error("计划已确认，但列表刷新失败。");
      if (!(await loadDetail({ ...plan, plan_id: id }))) throw new Error("计划已确认，但详情刷新失败。");
      return data;
    });
    if (result === null) return;
  }

  async function refreshDetail() {
    const plan = detailPlan || selectedPlan;
    if (plan) await loadDetail(plan);
  }

  async function refreshAssignments() {
    const plan = detailPlan || selectedPlan;
    const id = planId(plan);
    if (!id) return;
    setAssignmentError("");
    try {
      const data = await api.getAllocationPlanAssignments(id, scope);
      setAssignments(extractAssignments(data));
    } catch (error) {
      setAssignmentError(String(error));
      onError?.(`assignment 明细读取失败：${String(error)}`);
    }
  }

  const visibleAssignments = assignments.filter((item) => {
    if (assignmentFilter === "all") return true;
    if (assignmentFilter === "accepted") return ["accepted", "submitted"].includes(item.collection_status);
    if (assignmentFilter === "quarantined") return ["quarantined", "rejected"].includes(item.collection_status);
    return true;
  });
  const currentPlan = detailPlan || selectedPlan;
  const editGate = editAvailability({ plan: currentPlan, busy: Boolean(busyAction) });
  const formGate = allocationFormAvailability({ form, sourceLoading, sourceError, busy: Boolean(busyAction) });
  const backendPreviewGate = previewAvailability({ plan: currentPlan, busy: Boolean(busyAction) });
  const previewGate = formGate.enabled ? backendPreviewGate : formGate;
  const confirmGate = confirmAvailability({ plan: currentPlan, preview, busy: Boolean(busyAction) });
  const editing = Boolean(editingPlanId);

  return (
    <div>
      <div className="crumbs"><Link to="/">全部任务</Link> / <Link to={`/task/${encodeURIComponent(taskId)}`}>{taskId}</Link> / 分配计划</div>
      <div className="page-header allocation-page-header">
        <div>
          <h2>分配计划</h2>
          <p>管理标注人员组配置，查看只读规划、确认状态、执行进度和分配明细。</p>
        </div>
        <div className="action-row">
          <button className="btn btn-sm" type="button" onClick={reload} disabled={listLoading || Boolean(busyAction)}>{listLoading ? "读取中..." : "刷新"}</button>
          <button className="btn btn-primary" type="button" onClick={openCreate} disabled={Boolean(busyAction)}>新建分配计划</button>
        </div>
      </div>

      {operation.state && <div className={operationClass(operation)} role={operation.state === "error" ? "alert" : "status"}>{operation.message}</div>}
      {listError && <div className="empty allocation-list-error">{listError}<button className="btn btn-sm" type="button" onClick={reload} disabled={listLoading}>重试</button></div>}

      <section className="card section-card allocation-list-card">
        <div className="toolbar">
          <div className="toolbar-stack">
            <h3>分配计划列表（{plans.length}）</h3>
            <div className="status-line">列表内容来自专用分配接口；点击计划进入独立详情抽屉。</div>
          </div>
        </div>
        {listLoading && <div className="status-line">正在读取分配计划...</div>}
        {!listLoading && !listError && !plans.length && <div className="empty action-empty">服务端暂未返回分配计划。<button className="btn btn-primary" type="button" onClick={openCreate}>新建分配计划</button></div>}
        {!listLoading && plans.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead><tr><th>状态</th><th>策略</th><th>任务版本</th><th>样本/批次</th><th>人员组</th><th>远端工作区 / 数据集 UUID</th><th>进度</th><th>操作</th></tr></thead>
              <tbody>
                {plans.map((plan, index) => {
                  const status = planStatusLabel(plan);
                  const progress = planProgress(plan);
                  const id = planId(plan);
                  return (
                    <tr key={id || `plan-${index}`} className={currentPlan && planId(currentPlan) === id ? "row-selected clickable-row" : "clickable-row"} onClick={() => loadDetail(plan)}>
                      <td><span className={`badge ${statusBadgeClass(status)}`}>{status}</span></td>
                      <td>{strategyLabel(plan.strategy)}</td>
                      <td className="mono-cell">{valueOrDash(firstValue(plan.revision_id, plan.task_revision_id, plan.task_revision?.revision_id))}</td>
                      <td>{valueOrDash(firstValue(plan.source_manifest_id, plan.source_manifest?.manifest_id))}<span className="status-line">{firstValue(plan.source_manifest_kind, plan.source_manifest?.kind, "-")}</span></td>
                      <td>{valueOrDash(firstValue(plan.cohort_id, plan.cohort?.id, plan.cohort?.name))}</td>
                      <td className="text-cell"><span>{valueOrDash(planWorkspace(plan))}</span><span>{valueOrDash(planDataset(plan))}</span></td>
                      <td>{progressText(progress)}</td>
                      <td><button className="btn btn-sm" type="button" onClick={(event) => { event.stopPropagation(); loadDetail(plan); }}>详情</button></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {drawer === "create" && (
        <AllocationForm
          form={form}
          setForm={updateForm}
          trustedSources={trustedSources}
          sourceLoading={sourceLoading}
          sourceError={sourceError}
          onReloadSources={loadTrustedSources}
          onSourceChange={selectTrustedSource}
          formAvailability={allocationFormAvailability({ form, sourceLoading, sourceError, busy: Boolean(busyAction) })}
          busyAction={busyAction}
          onPreview={previewCurrent}
          onSave={savePlan}
          onClose={closeDrawer}
          preview={preview}
          editing={editing}
        />
      )}

      {drawer === "detail" && (
        <div className="drawer-backdrop" onClick={closeDrawer}>
          <aside className="drawer-panel drawer-panel-wide" onClick={(event) => event.stopPropagation()} role="dialog" aria-modal="true" aria-labelledby="allocation-detail-title">
            <div className="drawer-head">
              <div>
                <h3 id="allocation-detail-title">分配计划详情</h3>
                <p>{planId(currentPlan) || "读取中..."} · 由后端状态控制可执行操作</p>
              </div>
              <button className="btn btn-sm" type="button" onClick={closeDrawer} disabled={Boolean(busyAction)}>关闭</button>
            </div>
            {detailLoading && <div className="status-line">正在读取计划详情、进度和分配明细...</div>}
            {detailError && <div className="empty allocation-list-error">{detailError}<button className="btn btn-sm" type="button" onClick={() => currentPlan && loadDetail(currentPlan)} disabled={detailLoading}>重试</button></div>}
            {!detailLoading && !detailError && currentPlan && (
              <>
                <div className="drawer-detail-grid">
                  <DetailField label="状态" value={planStatusLabel(currentPlan)} />
                  <DetailField label="策略" value={strategyLabel(currentPlan.strategy)} />
                  <DetailField label="任务版本" value={firstValue(currentPlan.revision_id, currentPlan.task_revision_id, currentPlan.task_revision?.revision_id)} />
                  <DetailField label="样本/批次" value={firstValue(currentPlan.source_manifest_id, currentPlan.source_manifest?.manifest_id)} />
                  <DetailField label="人员组" value={firstValue(currentPlan.cohort_id, currentPlan.cohort?.name, currentPlan.cohort?.id)} />
                  <DetailField label="远端工作区 UUID" value={planWorkspace(currentPlan)} />
                  <DetailField label="远端数据集 UUID" value={planDataset(currentPlan)} />
                  <DetailField label="计划指纹" value={firstValue(currentPlan.plan_fingerprint, currentPlan.fingerprint)} />
                </div>

                <div className="drawer-actions allocation-detail-actions">
                  <button className="btn btn-sm" type="button" onClick={refreshDetail} disabled={Boolean(busyAction) || detailLoading}>刷新详情</button>
                  <button className="btn btn-sm" type="button" onClick={() => { setEditingPlanId(planId(currentPlan)); setForm(formFromAllocationPlan(currentPlan, task, taskId)); setTrustedSources([]); setSourceError(""); setDrawer("create"); loadTrustedSources(); }} disabled={!editGate.enabled} title={editGate.disabledReason}>编辑配置</button>
                  <button className="btn btn-sm btn-accent" type="button" onClick={previewCurrent} disabled={!previewGate.enabled} title={previewGate.disabledReason || "请先补全配置和预览记录摘要"}>生成只读预览</button>
                  <button className="btn btn-primary" type="button" onClick={confirmPlan} disabled={!confirmGate.enabled} title={confirmGate.disabledReason}>{busyAction === "confirm" ? "确认中..." : "确认计划"}</button>
                </div>
                {!confirmGate.enabled && <div className="stage-tip">确认不可用：{confirmGate.disabledReason}</div>}
                {preview && <PreviewSummary preview={preview} />}

                <ProgressPanel progress={progress || planProgress(currentPlan)} error={progressError} onRetry={refreshDetail} busy={Boolean(busyAction)} />

                <section className="allocation-assignments allocation-preview-section">
                  <div className="toolbar">
                    <div className="toolbar-stack"><h4>分配明细（{assignments.length}）</h4><div className="status-line">可按回收结果筛选；已接收与已隔离由后端状态区分。</div></div>
                    <button className="btn btn-sm" type="button" onClick={refreshAssignments} disabled={Boolean(busyAction)}>刷新明细</button>
                  </div>
                  <div className="tabs allocation-assignment-tabs" role="tablist" aria-label="assignment 回收状态">
                    {["all", "accepted", "quarantined"].map((filter) => (
                      <button key={filter} className={assignmentFilter === filter ? "tab active" : "tab"} type="button" role="tab" aria-selected={assignmentFilter === filter} onClick={() => setAssignmentFilter(filter)}>
                        {filter === "all" ? "全部" : COLLECTION_LABELS[filter]}（{filter === "all" ? assignments.length : assignments.filter((item) => filter === "accepted" ? ["accepted", "submitted"].includes(item.collection_status) : ["quarantined", "rejected"].includes(item.collection_status)).length}）
                      </button>
                    ))}
                  </div>
                  {assignmentError && <div className="empty allocation-list-error">分配明细读取失败：{assignmentError}</div>}
                  {!assignmentError && !visibleAssignments.length && <div className="empty">当前筛选没有分配明细。</div>}
                  {!assignmentError && visibleAssignments.length > 0 && (
                    <div className="table-wrap">
                      <table>
                        <thead><tr><th>分配编号</th><th>阶段</th><th>记录</th><th>批次</th><th>标注人员</th><th>工作区 UUID</th><th>数据集 UUID</th><th>回收状态</th></tr></thead>
                        <tbody>
                          {visibleAssignments.map((item, index) => (
                            <tr key={item.assignment_id || `${item.record_id || "record"}-${item.assignee_id || "assignee"}-${index}`} className={selectedAssignment === item ? "row-selected clickable-row" : "clickable-row"} onClick={() => setSelectedAssignment(item)}>
                              <td className="mono-cell">{valueOrDash(item.assignment_id)}</td>
                              <td>{phaseLabel(item.phase)}</td>
                              <td>{valueOrDash(item.record_id)}</td>
                              <td>{valueOrDash(item.batch_id)}</td>
                              <td>{valueOrDash(item.assignee_id)}</td>
                              <td className="mono-cell">{valueOrDash(item.workspace_uuid)}</td>
                              <td className="mono-cell">{valueOrDash(item.dataset_uuid)}</td>
                              <td><span className={`badge ${item.collection_status === "quarantined" || item.collection_status === "rejected" ? "badge-red" : item.collection_status === "accepted" || item.collection_status === "submitted" ? "badge-green" : "badge-gray"}`}>{collectionLabel(item.collection_status)}</span></td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                  {selectedAssignment && (
                    <div className="allocation-assignment-focus">
                      <div className="toolbar"><h4>分配详情</h4><button className="btn btn-sm" type="button" onClick={() => setSelectedAssignment(null)}>收起</button></div>
                      <div className="drawer-detail-grid">
                        <DetailField label="分配编号" value={selectedAssignment.assignment_id} />
                        <DetailField label="回收状态" value={collectionLabel(selectedAssignment.collection_status)} />
                        <DetailField label="提交人" value={firstValue(selectedAssignment.submitted_by, selectedAssignment.annotator_id)} />
                        <DetailField label="隔离原因" value={selectedAssignment.quarantine_reason} />
                      </div>
                    </div>
                  )}
                </section>
              </>
            )}
          </aside>
        </div>
      )}
    </div>
  );
}
