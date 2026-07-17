import React, { useCallback, useEffect, useMemo, useState } from "react";
import * as api from "./../api.js";
import { Link } from "./../router.jsx";
import {
  createImportActions,
  displayValue,
  filterImportAuditEvents,
  hasEffectiveDataLakeConfig,
  importActionState,
  shortHash,
  stateLabel,
  summarizeImportAsset,
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
  if (imported?.action === "reused") return "任务输入与已有资产一致，已幂等复用。下一步：样本抽取。";
  return `${savedText}下一步：样本抽取。`;
}

const EVENT_LABEL = {
  "import.create": "生成输入",
  "import.reuse": "复用输入",
  "import.save": "保存输入",
  "import.archive": "归档输入",
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
  onError,
}) {
  const [items, setItems] = useState([]);
  const [auditEvents, setAuditEvents] = useState([]);
  const [assetsLoading, setAssetsLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [lakeBusy, setLakeBusy] = useState(false);
  const [lakeImportId, setLakeImportId] = useState("");
  const [lakeStatus, setLakeStatus] = useState(null);
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
  const lakeWorking = lakeBusy || isActiveJob(lakeJob);
  const createActions = createImportActions({ hasDataLakeConfig });
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

  async function archive(item) {
    if (!item?.import_id) return;
    if ((item.linked_samples || []).length) {
      onError(`任务输入已被样本使用，不能归档：${item.linked_samples.map((sample) => sample.sample_id).join(", ")}`);
      return;
    }
    const ok = window.confirm(`归档任务输入 ${item.import_id}？\n\n归档会从当前列表移除，但不会删除原始文件；文件会移动到 runs 下的 _archive 目录。`);
    if (!ok) return;
    setBusy(true);
    try {
      await api.archiveImport(taskId, item.import_id, "panel archive");
      setNotice(`已归档任务输入：${item.import_id}`);
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
      onError("当前任务没有数据湖资产来源。请返回任务单，在数据湖资产配置中选择已登记的数据集和对象后发布。");
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
      onError("当前任务没有数据湖资产来源。请返回任务单，在数据湖资产配置中选择已登记的数据集和对象后发布。");
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
        setNotice("已提交任务输入生成任务，正在轮询执行状态。");
        if (!isActiveJob(job)) await finishLakeJob(job);
        return;
      }

      const imported = extractImport(result);
      const nextImportId = importedId(result, lakeImportId.trim());
      setNotice(completionNotice(imported, "已从数据湖生成任务输入。"));
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
      setNotice(completionNotice(imported, "数据湖任务输入已完成。"));
      setCompletedImport({ import_id: nextImportId, source: "data_lake" });
      await reload();
      if (nextImportId) setSelectedId(nextImportId);
      setCreatePanel("");
    } else if (JOB_FAILED_STATUSES.has(status)) {
      setNotice("数据湖任务输入未完成，请查看任务状态和错误信息。");
      onError(`数据湖任务输入失败：${jobErrorText(job)}`);
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
  const openDefaultCreatePanel = () => {
    const action = createActions[0];
    if (action) setCreatePanel(action.key);
  };

  return (
    <div>
      <div className="crumbs">
        <Link to="/">全部任务</Link> / <Link to={`/task/${encodeURIComponent(taskId)}`}>{taskId}</Link> / 任务输入
      </div>
      <div className="page-header imports-page-header">
        <div>
          <h2>任务输入</h2>
          <p>从已登记的数据湖资产生成任务级输入；原始对象、清单和任务产物仍由数据湖保存。</p>
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
            <span className="status-line">请先在任务单配置数据湖资产来源。</span>
          )}
        </div>
      </div>

      {notice && <div className="status-banner">{notice}</div>}
      {completedImport && (
        <div className="card section-card next-step-card">
          <div>
            <h3>下一步：样本抽取</h3>
            <p>
              {completedImport.import_id ? `任务输入 ${completedImport.import_id} 已可用。` : "任务输入已可用。"}
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
            <h3>任务输入资产（{items.length}）</h3>
            <div className="status-line">
              {hasDataLakeConfig
                ? "当前任务已绑定数据湖资产；生成输入时会按清单校验对象、哈希和行数。"
                : "当前任务尚未绑定数据湖资产，请先在任务单中选择已登记的数据集和对象。"}
            </div>
          </div>
          <button className="btn btn-sm" disabled={assetsLoading} onClick={reload}>
            {assetsLoading ? "刷新中..." : "刷新"}
          </button>
        </div>
        {assetsLoading && !items.length && <div className="empty">正在读取任务输入资产...</div>}
        {!assetsLoading && !items.length && (
          <div className="empty action-empty">
            <span>暂无任务输入</span>
            {createActions.length > 0 ? (
              <button className="btn btn-primary" onClick={openDefaultCreatePanel}>生成任务输入</button>
            ) : (
              <span>请先在任务单中配置数据湖资产来源。</span>
            )}
          </div>
        )}
        {items.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>输入编号</th>
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
                <h3>生成任务输入</h3>
                <p>从已绑定的数据湖资产生成不可覆盖的任务级输入。</p>
              </div>
              <button className="btn btn-sm" type="button" onClick={() => setCreatePanel("")}>关闭</button>
            </div>

            {createPanel === "data_lake" && (
              <div>
                <div className="info-callout import-drawer-callout">
                  <strong>生成任务输入</strong>
                  <p>按任务单绑定的数据湖资产读取清单文件，并生成当前任务的输入缓存。</p>
                </div>
                <div className="form-grid drawer-form-grid">
                  <div className="field field-half">
                    <label>目标输入编号</label>
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
                        <h3>任务输入执行状态</h3>
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
                    {lakeWorking ? "生成任务输入中..." : "生成任务输入"}
                  </button>
                  <button className="btn" disabled={lakeWorking} onClick={checkDataLake}>检查数据湖配置</button>
                </div>
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
                <h3>任务输入详情：{selected.import_id}</h3>
                <p>清单、数据行预览、存储路径和资产审计信息。</p>
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
              <a className="btn" href={api.importDownloadUrl(taskId, selected.import_id)}>下载输入</a>
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
              <strong>清单与存储</strong>
              <p>清单：{selectedDetail?.manifest_path || "-"}</p>
              <p>保存路径：{selectedDetail?.path || "-"}</p>
              {selectedDetail?.declared_path && <p>历史清单原路径：{selectedDetail.declared_path}</p>}
              {selectedDetail?.source_dataset_id && <p>源数据集：{selectedDetail.source_dataset_id}</p>}
              {selectedDetail?.source_object_path && <p>源对象：{selectedDetail.source_object_path}</p>}
              {selectedDetail?.source_manifest_uri && <p>源清单：{selectedDetail.source_manifest_uri}</p>}
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
                <input className="toolbar-input" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索当前任务输入" />
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
              {!selectedAuditEvents.length && <div className="empty">暂无该任务输入的审计事件</div>}
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
