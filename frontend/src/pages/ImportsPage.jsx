import React, { useCallback, useEffect, useMemo, useState } from "react";
import * as api from "./../api.js";
import { Link } from "./../router.jsx";
import {
  backendAllowsManualImports,
  createImportActions,
  displayValue,
  filterImportAuditEvents,
  hasEffectiveDataLakeConfig,
  importActionState,
  shortHash,
  stateLabel,
  summarizeImportAsset,
  usesLocalTaskSource,
  usesR2TaskSource,
} from "./importsPageState.js";

const JOB_STATUS_LABEL = {
  pending: "等待中",
  queued: "排队中",
  running: "运行中",
  in_progress: "运行中",
  started: "运行中",
  succeeded: "成功",
  success: "成功",
  completed: "已完成",
  complete: "已完成",
  done: "已完成",
  finished: "已完成",
  failed: "失败",
  error: "失败",
  cancelled: "已取消",
  canceled: "已取消",
};

const JOB_ACTIVE_STATUSES = new Set(["pending", "queued", "running", "in_progress", "started"]);
const JOB_SUCCESS_STATUSES = new Set(["succeeded", "success", "completed", "complete", "done", "finished"]);
const JOB_FAILED_STATUSES = new Set(["failed", "error", "cancelled", "canceled"]);

function normalizeStatus(value) {
  return String(value || "pending").toLowerCase();
}

function jobStatusLabel(value) {
  const status = normalizeStatus(value);
  return JOB_STATUS_LABEL[status] || value || "-";
}

function jobBadgeClass(value) {
  const status = normalizeStatus(value);
  if (JOB_SUCCESS_STATUSES.has(status)) return "badge-green";
  if (JOB_FAILED_STATUSES.has(status)) return "badge-red";
  if (JOB_ACTIVE_STATUSES.has(status)) return "badge-blue";
  return "badge-gray";
}

function isActiveJob(job) {
  return Boolean(job?.id && JOB_ACTIVE_STATUSES.has(normalizeStatus(job.status)));
}

function normalizeJob(job) {
  if (!job || typeof job !== "object") return null;
  const id = job.id || job.job_id;
  return id ? { ...job, id } : null;
}

function findJob(jobs, jobId) {
  return (jobs || []).find((job) => job.id === jobId || job.job_id === jobId) || null;
}

function jobErrorText(job) {
  if (!job) return "后端未返回错误详情";
  if (job.error) return String(job.error);
  if (job.result?.error) return String(job.result.error);
  return "后端未返回错误详情";
}

function extractImport(value) {
  if (!value || typeof value !== "object") return null;
  const direct = value.import || value.result?.import || value.result || value;
  if (!direct || typeof direct !== "object") return null;
  if (direct.import_id || direct.action || direct.path || direct.rows !== undefined) return direct;
  return null;
}

function importedId(value, fallback = "") {
  const imported = extractImport(value);
  return imported?.import_id || value?.import_id || value?.result?.import_id || fallback || "";
}

function completionNotice(imported, savedText) {
  if (imported?.action === "reused") return "导入内容与已有数据一致，已幂等复用。下一步：样本抽取。";
  return `${savedText}下一步：样本抽取。`;
}

const EVENT_LABEL = {
  "import.create": "创建导入",
  "import.reuse": "复用导入",
  "import.save": "保存导入",
  "import.archive": "归档导入",
};

function DetailField({ label, value, className = "" }) {
  return (
    <div>
      <span>{label}</span>
      <strong className={className}>{displayValue(value)}</strong>
    </div>
  );
}

export default function ImportsPage({
  task,
  taskId,
  taskSource = "",
  allowManualImports = false,
  settingsReady = false,
  settingsError = "",
  onError,
}) {
  const [items, setItems] = useState([]);
  const [auditEvents, setAuditEvents] = useState([]);
  const [assetsLoading, setAssetsLoading] = useState(false);
  const [name, setName] = useState("");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [lakeBusy, setLakeBusy] = useState(false);
  const [lakeImportId, setLakeImportId] = useState("");
  const [lakeStatus, setLakeStatus] = useState(null);
  const [fileLabel, setFileLabel] = useState("");
  const [selectedId, setSelectedId] = useState("");
  const [detail, setDetail] = useState(null);
  const [rowsData, setRowsData] = useState({ rows: [], fields: [], total: 0, offset: 0, limit: 25 });
  const [query, setQuery] = useState("");
  const [notice, setNotice] = useState("");
  const [lakeJob, setLakeJob] = useState(null);
  const [completedImport, setCompletedImport] = useState(null);
  const [createPanel, setCreatePanel] = useState("");

  const selected = useMemo(
    () => items.find((item) => item.import_id === selectedId) || null,
    [items, selectedId],
  );
  const dataLake = task?.data_lake || null;
  const hasDataLakeConfig = hasEffectiveDataLakeConfig(dataLake);
  const r2TaskSource = usesR2TaskSource(taskSource, task);
  const localTaskSource = usesLocalTaskSource(taskSource);
  const settingsAvailable = settingsReady && !settingsError;
  const manualAllowed = backendAllowsManualImports(task, allowManualImports);
  const showManualImports = settingsAvailable && localTaskSource && !r2TaskSource && manualAllowed;
  const lakeWorking = lakeBusy || isActiveJob(lakeJob);
  const createActions = createImportActions({ hasDataLakeConfig, showManualImports });
  const loadedDetail = detail?.import_id === selectedId ? detail : null;
  const selectedDetail = loadedDetail || selected;
  const selectedAuditEvents = filterImportAuditEvents(auditEvents, selectedId);

  const reload = useCallback(async () => {
    if (!taskId) return;
    setAssetsLoading(true);
    try {
      const [data, audit] = await Promise.all([
        api.getImports(taskId),
        api.getAuditEvents(taskId).catch(() => ({ events: [] })),
      ]);
      const next = data.imports || [];
      setItems(next);
      setAuditEvents(audit.events || []);
      setSelectedId((current) => (current && next.some((item) => item.import_id === current) ? current : ""));
    } catch (error) {
      onError(String(error));
    } finally {
      setAssetsLoading(false);
    }
  }, [taskId, onError]);

  const loadRows = useCallback(async (importId, opts = {}) => {
    if (!taskId || !importId) return;
    try {
      const data = await api.getImportRows(taskId, importId, {
        offset: opts.offset ?? 0,
        limit: rowsData.limit || 25,
        query: opts.query ?? query,
      });
      setRowsData(data);
    } catch (error) {
      onError(String(error));
    }
  }, [taskId, onError, query, rowsData.limit]);

  useEffect(() => {
    reload();
  }, [reload]);

  useEffect(() => {
    const configured = task?.data_lake || {};
    setLakeImportId(configured.default_import_id || "");
    setLakeStatus(null);
    setLakeJob(null);
    setCompletedImport(null);
  }, [task?.task_id, task?.data_lake]);

  useEffect(() => {
    if (!taskId || !selectedId) {
      setDetail(null);
      setRowsData({ rows: [], fields: [], total: 0, offset: 0, limit: 25 });
      return;
    }
    setDetail(null);
    setRowsData({ rows: [], fields: [], total: 0, offset: 0, limit: 25 });
    api.getImportDetail(taskId, selectedId)
      .then((data) => setDetail(data.import || null))
      .catch((error) => onError(String(error)));
    loadRows(selectedId, { offset: 0 });
  }, [taskId, selectedId, loadRows, onError]);

  async function submit() {
    if (!name.trim() || !text.trim()) {
      onError("请填写导入编号并上传或粘贴数据内容");
      return;
    }
    setBusy(true);
    setNotice("");
    try {
      const result = await api.importJsonl(taskId, name.trim(), text);
      const imported = result.import || result;
      const nextImportId = imported.import_id || name.trim();
      setNotice(completionNotice(imported, "导入数据已保存。"));
      setCompletedImport({ import_id: nextImportId, source: "manual" });
      setName("");
      setText("");
      setFileLabel("");
      await reload();
      setSelectedId(nextImportId);
      setCreatePanel("");
    } catch (error) {
      onError(String(error));
    } finally {
      setBusy(false);
    }
  }

  async function selectFile(event) {
    const file = event.target.files?.[0];
    if (!file) return;
    try {
      const content = await file.text();
      const baseName = file.name.replace(/\.[^.]+$/, "").replace(/[\\/:*?"<>|]/g, "_").replace(/\.\.+/g, ".");
      if (!name.trim()) setName(baseName || "imported");
      setText(content);
      setFileLabel(`${file.name} · ${content.split(/\r?\n/).filter((line) => line.trim()).length} 行`);
    } catch (error) {
      onError(String(error));
    }
  }

  async function archive(item) {
    if (!item?.import_id) return;
    if ((item.linked_samples || []).length) {
      onError(`导入数据已被样本使用，不能归档：${item.linked_samples.map((sample) => sample.sample_id).join(", ")}`);
      return;
    }
    const ok = window.confirm(`归档导入数据 ${item.import_id}？\n\n归档会从当前列表移除，但不会删除原始文件；文件会移动到 runs 下的 _archive 目录。`);
    if (!ok) return;
    setBusy(true);
    try {
      await api.archiveImport(taskId, item.import_id, "panel archive");
      setNotice(`已归档：${item.import_id}`);
      setSelectedId("");
      setDetail(null);
      setRowsData({ rows: [], fields: [], total: 0, offset: 0, limit: 25 });
      await reload();
    } catch (error) {
      onError(String(error));
    } finally {
      setBusy(false);
    }
  }

  async function checkDataLake() {
    if (!taskId) return;
    if (!hasDataLakeConfig) {
      onError("当前任务没有 data_lake 来源。需要在 R2 任务配置的 data_lake 字段登记来源，然后同步任务配置。");
      return;
    }
    setLakeBusy(true);
    setNotice("");
    try {
      const data = await api.getDataLakeStatus(taskId);
      setLakeStatus(data.preview || null);
      if (!lakeImportId && dataLake?.default_import_id) setLakeImportId(dataLake.default_import_id);
      setNotice("数据湖配置可读取。");
    } catch (error) {
      onError(String(error));
    } finally {
      setLakeBusy(false);
    }
  }

  async function importLake() {
    if (!taskId) return;
    if (!hasDataLakeConfig) {
      onError("当前任务没有 data_lake 来源。需要在 R2 任务配置的 data_lake 字段登记来源，然后同步任务配置。");
      return;
    }
    setLakeBusy(true);
    setNotice("");
    setLakeJob(null);
    setCompletedImport(null);
    try {
      const result = await api.importFromDataLake(taskId, { import_id: lakeImportId.trim() });
      const job = normalizeJob(result.job || result.import_job || result.data?.job || (result.ok ? result : null));
      if (job) {
        setLakeJob(job);
        setNotice("已提交数据湖导入任务，正在轮询执行状态。");
        if (!isActiveJob(job)) await finishLakeJob(job);
        return;
      }

      const imported = extractImport(result);
      const nextImportId = importedId(result, lakeImportId.trim());
      setNotice(completionNotice(imported, "已从数据湖生成本地导入。"));
      setCompletedImport({ import_id: nextImportId, source: "data_lake" });
      await reload();
      if (nextImportId) setSelectedId(nextImportId);
      setCreatePanel("");
    } catch (error) {
      onError(String(error));
    } finally {
      setLakeBusy(false);
    }
  }

  const finishLakeJob = useCallback(async (job) => {
    const status = normalizeStatus(job?.status);
    if (JOB_SUCCESS_STATUSES.has(status)) {
      const imported = extractImport(job);
      const nextImportId = importedId(job, lakeImportId.trim());
      setNotice(completionNotice(imported, "数据湖导入已完成。"));
      setCompletedImport({ import_id: nextImportId, source: "data_lake" });
      await reload();
      if (nextImportId) setSelectedId(nextImportId);
      setCreatePanel("");
    } else if (JOB_FAILED_STATUSES.has(status)) {
      setNotice("数据湖导入未完成，请查看任务状态和错误信息。");
      onError(`数据湖导入失败：${jobErrorText(job)}`);
    }
    setLakeBusy(false);
  }, [lakeImportId, onError, reload]);

  useEffect(() => {
    if (!taskId || !isActiveJob(lakeJob)) return undefined;
    const jobId = lakeJob.id;
    let stopped = false;
    let inflight = false;

    async function pollJob() {
      if (inflight) return;
      inflight = true;
      try {
        const data = await api.getJobs(taskId);
        const latest = normalizeJob(findJob(data.jobs || [], jobId));
        if (!stopped && latest) {
          setLakeJob(latest);
          if (!isActiveJob(latest)) await finishLakeJob(latest);
        }
      } catch (error) {
        if (!stopped) onError(String(error));
      } finally {
        inflight = false;
      }
    }

    pollJob();
    const timer = window.setInterval(pollJob, 2000);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [taskId, lakeJob?.id, lakeJob?.status, finishLakeJob, onError]);

  function searchRows() {
    loadRows(selectedId, { offset: 0, query });
  }

  function pageRows(delta) {
    const next = Math.max(0, (rowsData.offset || 0) + delta * (rowsData.limit || 25));
    loadRows(selectedId, { offset: next });
  }

  const fields = rowsData.fields?.length ? rowsData.fields : selectedDetail?.fields || [];
  const selectedActions = importActionState(selectedDetail, { busy });
  const openDefaultCreatePanel = () => setCreatePanel(createActions[0]?.key || "unavailable");

  return (
    <div>
      <div className="crumbs">
        <Link to="/">全部任务</Link> / <Link to={`/task/${encodeURIComponent(taskId)}`}>{taskId}</Link> / 数据导入
      </div>
      <div className="page-header imports-page-header">
        <div>
          <h2>数据导入</h2>
          <p>生产路径优先从 R2 数据湖读取；导入数据按不可覆盖资产管理，同名同内容幂等复用。</p>
        </div>
        <div className="action-row">
          {createActions.length > 0 ? (
            createActions.map((action) => (
              <button
                className={`btn ${action.primary ? "btn-primary" : ""}`}
                key={action.key}
                onClick={() => setCreatePanel(action.key)}
              >
                {action.label}
              </button>
            ))
          ) : (
            <button className="btn" disabled>暂无可用导入入口</button>
          )}
        </div>
      </div>

      {notice && <div className="status-banner">{notice}</div>}
      {completedImport && (
        <div className="card section-card next-step-card">
          <div>
            <h3>下一步：样本抽取</h3>
            <p>
              {completedImport.import_id ? `导入 ${completedImport.import_id} 已可用。` : "导入已可用。"}
              可以回到任务概览查看 profile 阶段，也可以进入样本管理创建样本。
            </p>
          </div>
          <div className="action-row">
            <Link className="btn" to={`/task/${encodeURIComponent(taskId)}`}>回到任务概览</Link>
            <Link className="btn btn-primary" to={`/task/${encodeURIComponent(taskId)}/samples`}>进入样本管理</Link>
          </div>
        </div>
      )}

      <div className="card section-card">
        <div className="toolbar">
          <div>
            <h3>导入资产（{items.length}）</h3>
            <div className="status-line">
              {hasDataLakeConfig
                ? "当前任务已配置数据湖来源；新增导入会按 task.yaml 中的数据湖配置执行。"
                : "当前任务未配置 data_lake 来源；生产环境不会展示手动覆盖 R2 来源的主动作。"}
            </div>
          </div>
          <button className="btn btn-sm" disabled={assetsLoading} onClick={reload}>
            {assetsLoading ? "刷新中..." : "刷新"}
          </button>
        </div>
        {assetsLoading && !items.length && <div className="empty">正在读取导入资产...</div>}
        {!assetsLoading && !items.length && (
          <div className="empty action-empty">
            <span>暂无导入数据</span>
            {createActions.length > 0 ? (
              <button className="btn btn-primary" onClick={openDefaultCreatePanel}>新增导入</button>
            ) : (
              <Link className="btn" to="/">去任务列表同步</Link>
            )}
          </div>
        )}
        {items.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>导入编号</th>
                  <th>来源</th>
                  <th>状态</th>
                  <th>行数</th>
                  <th>记录编号唯一数</th>
                  <th>质量摘要</th>
                  <th>关联样本</th>
                  <th>内容哈希</th>
                  <th>保存路径</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => {
                  const summary = summarizeImportAsset(item);
                  return (
                    <tr
                      key={item.import_id}
                      className={`clickable-row ${selectedId === item.import_id ? "row-selected" : ""}`}
                      onClick={() => setSelectedId(item.import_id)}
                    >
                      <td><strong>{summary.importId}</strong></td>
                      <td>{summary.source}</td>
                      <td>{summary.state}</td>
                      <td>{summary.rows}</td>
                      <td>{summary.uniqueIds}</td>
                      <td>{summary.idQuality}</td>
                      <td>{summary.linkedSamples}</td>
                      <td className="mono-cell">{summary.contentHash}</td>
                      <td className="muted path-cell">{summary.storagePath}</td>
                      <td>
                        <button
                          className="btn btn-sm"
                          onClick={(event) => {
                            event.stopPropagation();
                            setSelectedId(item.import_id);
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

      {createPanel && (
        <div className="drawer-backdrop" onClick={() => setCreatePanel("")}>
          <aside className="drawer-panel" onClick={(event) => event.stopPropagation()}>
            <div className="drawer-head">
              <div>
                <h3>新增导入</h3>
                <p>新增和执行动作在这里完成，完成后回到导入资产列表。</p>
              </div>
              <button className="btn btn-sm" type="button" onClick={() => setCreatePanel("")}>关闭</button>
            </div>

            {createActions.length > 1 && (
              <div className="tabs import-create-tabs">
                {createActions.map((action) => (
                  <button
                    className={`tab ${createPanel === action.key ? "active" : ""}`}
                    key={action.key}
                    onClick={() => setCreatePanel(action.key)}
                  >
                    {action.label}
                  </button>
                ))}
              </div>
            )}

            {createPanel === "data_lake" && (
              <div>
                <div className="info-callout import-drawer-callout">
                  <strong>从数据湖导入</strong>
                  <p>按任务配置读取 R2 数据湖清单文件，并生成当前任务的本地导入缓存。</p>
                </div>
                <div className="form-grid drawer-form-grid">
                  <div className="field field-half">
                    <label>目标导入编号</label>
                    <input value={lakeImportId} onChange={(event) => setLakeImportId(event.target.value)} placeholder={dataLake.default_import_id || "留空则自动生成"} />
                    <span className="hint">同名同内容会幂等复用，同名不同内容会拒绝写入。</span>
                  </div>
                  <div className="field field-half">
                    <label>源数据集</label>
                    <input value={dataLake.source_dataset_id || "-"} readOnly />
                  </div>
                  <div className="field field-wide">
                    <label>源对象</label>
                    <input value={lakeStatus?.selected_object?.path || dataLake.source_object_path || "-"} readOnly />
                  </div>
                </div>
                {lakeStatus && (
                  <div className="drawer-detail-grid">
                    <DetailField label="数据层" value={lakeStatus.dataset?.layer} />
                    <DetailField label="领域" value={lakeStatus.dataset?.domain} />
                    <DetailField label="清单对象数" value={lakeStatus.manifest?.object_count} />
                    <DetailField label="选中对象大小" value={lakeStatus.selected_object?.bytes} />
                  </div>
                )}
                {lakeJob && (
                  <div className="job-panel">
                    <div className="toolbar">
                      <div>
                        <h3>导入任务状态</h3>
                        <div className="status-line">执行编号：<span className="mono-cell">{lakeJob.id}</span></div>
                      </div>
                      <span className={`badge ${jobBadgeClass(lakeJob.status)}`}>{jobStatusLabel(lakeJob.status)}</span>
                    </div>
                    <div className="job-grid">
                      <div><span>轮询状态</span><strong>{isActiveJob(lakeJob) ? "每 2 秒刷新" : "已停止"}</strong></div>
                      <div><span>创建时间</span><strong>{(lakeJob.created_at || "").slice(0, 19) || "-"}</strong></div>
                      <div><span>最近更新</span><strong>{(lakeJob.updated_at || lakeJob.finished_at || "").slice(0, 19) || "-"}</strong></div>
                    </div>
                    {JOB_FAILED_STATUSES.has(normalizeStatus(lakeJob.status)) && (
                      <div className="status-line danger-line">错误：{jobErrorText(lakeJob)}</div>
                    )}
                  </div>
                )}
                <div className="drawer-actions">
                  <button className="btn btn-primary" disabled={lakeWorking} onClick={importLake}>
                    {lakeWorking ? "导入任务执行中..." : "从数据湖导入"}
                  </button>
                  <button className="btn" disabled={lakeWorking} onClick={checkDataLake}>检查配置</button>
                </div>
              </div>
            )}

            {createPanel === "manual" && (
              <div>
                <div className="info-callout import-drawer-callout">
                  <strong>手动上传</strong>
                  <p>仅在本地/开发任务且后端允许手动导入时可用；生产 R2 任务请使用数据湖导入。</p>
                </div>
                <div className="form-grid drawer-form-grid">
                  <div className="field field-half">
                    <label>导入编号</label>
                    <input value={name} onChange={(event) => setName(event.target.value)} placeholder="例如 manual_seed_20260627" />
                    <span className="hint">同一编号不能覆盖不同内容；修正数据请使用新的导入编号。</span>
                  </div>
                  <div className="field field-half">
                    <label>上传文件</label>
                    <input type="file" accept=".jsonl,.ndjson,.json,.txt,application/json,application/x-ndjson,text/plain" onChange={selectFile} />
                    {fileLabel && <span className="hint">{fileLabel}</span>}
                  </div>
                  <div className="field field-wide">
                    <label>数据内容</label>
                    <textarea
                      rows={10}
                      value={text}
                      onChange={(event) => setText(event.target.value)}
                      placeholder='每行一个 JSON 对象，例如 {"record_id":"r001","title":"标题","body":"正文"}'
                    />
                  </div>
                </div>
                <div className="drawer-actions">
                  <button className="btn btn-primary" disabled={busy} onClick={submit}>
                    {busy ? "保存中..." : "保存手动导入"}
                  </button>
                </div>
              </div>
            )}

            {createPanel === "unavailable" && (
              <div className="info-callout">
                <strong>暂无可用导入入口</strong>
                <p>当前任务没有可执行的数据湖导入配置，且当前模式不允许手动上传。</p>
              </div>
            )}
          </aside>
        </div>
      )}

      {selected && (
        <div className="drawer-backdrop" onClick={() => setSelectedId("")}>
          <aside className="drawer-panel drawer-panel-wide" onClick={(event) => event.stopPropagation()}>
            <div className="drawer-head">
              <div>
                <h3>导入详情：{selected.import_id}</h3>
                <p>manifest、数据行预览、存储路径和资产审计信息。</p>
              </div>
              <button className="btn btn-sm" type="button" onClick={() => setSelectedId("")}>关闭</button>
            </div>

            <div className="drawer-detail-grid">
              <DetailField label="状态" value={stateLabel(selectedDetail?.state || "active")} />
              <DetailField label="来源" value={summarizeImportAsset(selectedDetail).source} />
              <DetailField label="行数" value={selectedDetail?.rows} />
              <DetailField label="记录编号字段" value={selectedDetail?.id_field} />
              <DetailField label="记录编号唯一数" value={selectedDetail?.unique_ids} />
              <DetailField label="缺失记录编号" value={selectedDetail?.missing_ids} />
              <DetailField label="重复记录编号" value={selectedDetail?.duplicate_ids} />
              <DetailField label="内容哈希" value={shortHash(selectedDetail?.content_sha256)} className="mono-cell" />
            </div>

            <div className="drawer-actions">
              <button className="btn btn-primary" disabled={!selectedActions.canViewRows} onClick={() => loadRows(selected.import_id, { offset: 0 })}>查看行</button>
              <a className="btn" href={api.importDownloadUrl(taskId, selected.import_id)}>下载</a>
              <button
                className="btn btn-danger"
                disabled={!selectedActions.canArchive}
                title={selectedActions.archiveDisabledReason}
                onClick={() => archive(selectedDetail)}
              >
                归档
              </button>
            </div>

            <div className="info-callout import-manifest-panel">
              <strong>Manifest 与存储</strong>
              <p>manifest：{selectedDetail?.manifest_path || "-"}</p>
              <p>保存路径：{selectedDetail?.path || "-"}</p>
              {selectedDetail?.declared_path && <p>历史清单原路径：{selectedDetail.declared_path}</p>}
              {selectedDetail?.source_dataset_id && <p>源数据集：{selectedDetail.source_dataset_id}</p>}
              {selectedDetail?.source_object_path && <p>源对象：{selectedDetail.source_object_path}</p>}
              {selectedDetail?.source_manifest_uri && <p>源 manifest：{selectedDetail.source_manifest_uri}</p>}
            </div>

            {(selectedDetail?.linked_samples || []).length > 0 && (
              <div className="status-line">关联样本：{selectedDetail.linked_samples.map((sample) => sample.sample_id).join(", ")}</div>
            )}

            <details className="secondary-panel" open>
              <summary>字段清单</summary>
              <div className="field-list">{(selectedDetail?.fields || []).map((field) => <span key={field}>{field}</span>)}</div>
            </details>

            <div className="toolbar data-toolbar">
              <div>
                <h3>数据行</h3>
                <div className="status-line">匹配 {rowsData.total || 0} 行，当前显示第 {(rowsData.offset || 0) + 1} - {Math.min((rowsData.offset || 0) + (rowsData.rows || []).length, rowsData.total || 0)} 行</div>
              </div>
              <div className="action-row">
                <input className="toolbar-input" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索当前导入数据" />
                <button className="btn btn-sm" onClick={searchRows}>搜索</button>
                <button className="btn btn-sm" onClick={() => { setQuery(""); loadRows(selected.import_id, { offset: 0, query: "" }); }}>清空</button>
                <button className="btn btn-sm" disabled={(rowsData.offset || 0) <= 0} onClick={() => pageRows(-1)}>上一页</button>
                <button className="btn btn-sm" disabled={(rowsData.offset || 0) + (rowsData.limit || 25) >= (rowsData.total || 0)} onClick={() => pageRows(1)}>下一页</button>
              </div>
            </div>
            <div className="table-wrap data-table">
              <table>
                <thead>
                  <tr>
                    <th>#</th>
                    {fields.map((field) => <th key={field}>{field}</th>)}
                  </tr>
                </thead>
                <tbody>
                  {(rowsData.rows || []).map((row, index) => (
                    <tr key={`${rowsData.offset || 0}-${index}`}>
                      <td>{(rowsData.offset || 0) + index + 1}</td>
                      {fields.map((field) => <td key={field} className="text-cell">{displayValue(row[field])}</td>)}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <details className="secondary-panel" open>
              <summary>资产审计</summary>
              {!selectedAuditEvents.length && <div className="empty">暂无该导入的审计事件</div>}
              {selectedAuditEvents.length > 0 && (
                <div className="table-wrap">
                  <table>
                    <thead><tr><th>时间</th><th>事件</th><th>状态</th><th>详情</th></tr></thead>
                    <tbody>
                      {selectedAuditEvents.map((event, index) => (
                        <tr key={`${event.created_at}-${index}`}>
                          <td className="muted">{(event.created_at || "").slice(0, 19)}</td>
                          <td>{EVENT_LABEL[event.event] || event.event}</td>
                          <td><span className={`badge ${event.status === "failed" ? "badge-red" : "badge-green"}`}>{event.status === "failed" ? "失败" : "成功"}</span></td>
                          <td className="muted path-cell">{JSON.stringify(event.details || {}).slice(0, 180)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </details>
          </aside>
        </div>
      )}
    </div>
  );
}
