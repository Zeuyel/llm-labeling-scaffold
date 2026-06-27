import React, { useEffect, useMemo, useState, useCallback } from "react";
import * as api from "./../api.js";
import { Link } from "./../router.jsx";
import {
  computeSampleDetailActions,
  computeSampleWorkflow,
  computeSamplesListView,
  filterSampleAuditEvents,
  getBatchManifests,
  hasBatchPlan,
  newestSample,
  sampleCreatedAt,
  sampleCompletionNotice,
  sampleStateLabel,
} from "./samplesWorkflowState.js";
import {
  batchPlanDebugFields,
  displayPlanValue,
  formatBatchPlanSummary,
  getBatchPlans,
} from "./batchPlanDisplay.js";

function shortHash(value) {
  return value ? `${String(value).slice(0, 12)}...` : "-";
}

function sampleIdFromImport(importId) {
  return String(importId || "sample")
    .trim()
    .replace(/[^A-Za-z0-9_.-]+/g, "_")
    .replace(/_+/g, "_")
    .replace(/^[_.-]+|[_.-]+$/g, "") || "sample";
}

function formatPercent(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "-";
  return `${Math.round(numeric * 1000) / 10}%`;
}

function firstDefined(...values) {
  return values.find((value) => value !== undefined && value !== null && value !== "");
}

function batchSummary(sample) {
  const manifests = getBatchManifests(sample);
  if (!manifests.length) {
    return {
      hasBatchInfo: false,
      batchText: "未记录",
      overlapText: "未记录",
      policyText: "未记录",
    };
  }

  const manifest = manifests[0];
  const consistency = manifest.consistency || manifest.quality_controls || manifest.policy || {};
  const batchCount = firstDefined(
    manifest.batch_count,
    manifest.batches_count,
    Array.isArray(manifest.batches) ? manifest.batches.length : undefined,
  );
  const batchSize = firstDefined(manifest.batch_size, manifest.rows_per_batch);
  const overlapCount = firstDefined(
    manifest.overlap_count,
    manifest.overlap_items,
    manifest.overlap_item_count,
    consistency.overlap_count,
    consistency.overlap_items,
    consistency.overlap_item_count,
  );
  const overlapRate = firstDefined(manifest.overlap_rate, consistency.overlap_rate);
  const minAnnotators = firstDefined(
    manifest.min_annotators_per_overlap_item,
    manifest.min_annotators,
    consistency.min_annotators_per_overlap_item,
    consistency.min_annotators,
  );
  const goldRate = firstDefined(manifest.gold_rate, consistency.gold_rate);

  const policyParts = [];
  if (overlapRate !== undefined) policyParts.push(`重叠 ${formatPercent(overlapRate)}`);
  if (minAnnotators !== undefined) policyParts.push(`重叠样本至少 ${minAnnotators} 人标注`);
  if (goldRate !== undefined) policyParts.push(`控制样本 ${formatPercent(goldRate)}`);

  return {
    hasBatchInfo: true,
    batchText: batchCount ? `${batchCount} 批${batchSize ? ` · 每批 ${batchSize} 行` : ""}` : "已记录批次",
    overlapText: overlapCount !== undefined ? `${overlapCount} 条` : "未记录",
    policyText: policyParts.length ? policyParts.join("；") : "未记录",
  };
}

function DetailField({ label, value, className = "" }) {
  return (
    <div>
      <span>{label}</span>
      <strong className={className}>{value === undefined || value === null || value === "" ? "-" : value}</strong>
    </div>
  );
}

function formatStrategy(value) {
  if (value === "random") return "随机抽样";
  if (value === "head") return "前 N 行";
  return value || "-";
}

function sampleDependencies(sample) {
  return (sample?.dependencies || []).map((dep) => `${dep.kind}:${dep.id}`).join(", ") || "-";
}

function manifestJson(value) {
  return JSON.stringify(value || {}, null, 2);
}

const EVENT_LABEL = {
  "sample.create": "创建样本",
  "sample.reuse": "复用样本",
  "sample.save": "保存样本",
  "sample.archive": "归档样本",
};

export default function SamplesPage({ task, taskId, onError }) {
  const [samples, setSamples] = useState([]);
  const [imports, setImports] = useState([]);
  const [auditEvents, setAuditEvents] = useState([]);
  const [auditLoadError, setAuditLoadError] = useState("");
  const [sampleId, setSampleId] = useState("");
  const [source, setSource] = useState("");
  const [rows, setRows] = useState(6);
  const [strategy, setStrategy] = useState("head");
  const [batchSize, setBatchSize] = useState(5);
  const [batchSample, setBatchSample] = useState("");
  const [overlapRate, setOverlapRate] = useState(0.1);
  const [minAnnotatorsPerOverlapItem, setMinAnnotatorsPerOverlapItem] = useState(2);
  const [goldRate] = useState(0);
  const [sampleAuto, setSampleAuto] = useState(true);
  const [assetsLoading, setAssetsLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [activeAction, setActiveAction] = useState("");
  const [lastAction, setLastAction] = useState("");
  const [actionError, setActionError] = useState("");
  const [notice, setNotice] = useState("");
  const [drawer, setDrawer] = useState("");
  const [detailSamplePath, setDetailSamplePath] = useState("");

  const reload = useCallback(async () => {
    if (!taskId) {
      setAssetsLoading(false);
      return;
    }
    setAssetsLoading(true);
    setAuditLoadError("");
    try {
      const [s, i, a] = await Promise.all([
        api.getTaskSamples(taskId),
        api.getImports(taskId),
        api.getAuditEvents(taskId).catch((error) => ({ events: [], error })),
      ]);
      setSamples(s.samples || []);
      setImports(i.imports || []);
      setAuditEvents(a.events || []);
      setAuditLoadError(a.error ? String(a.error) : "");
    } catch (e) {
      onError(String(e));
    } finally {
      setAssetsLoading(false);
    }
  }, [taskId, onError]);

  useEffect(() => { reload(); }, [reload]);

  const selectedImport = useMemo(
    () => imports.find((item) => item.path === source) || null,
    [imports, source],
  );

  const selectedBatchSample = useMemo(
    () => samples.find((item) => item.path === batchSample) || null,
    [samples, batchSample],
  );

  const selectedDetailSample = useMemo(
    () => samples.find((item) => item.path === detailSamplePath) || null,
    [samples, detailSamplePath],
  );

  const selectedBatchSummary = useMemo(
    () => batchSummary(selectedBatchSample),
    [selectedBatchSample],
  );
  const selectedDetailSummary = useMemo(
    () => batchSummary(selectedDetailSample),
    [selectedDetailSample],
  );
  const detailBatchPlans = useMemo(
    () => getBatchPlans(selectedDetailSample),
    [selectedDetailSample],
  );
  const latestDetailBatchPlan = detailBatchPlans[0] || null;
  const detailSourceImport = useMemo(() => {
    const sourceImportId = selectedDetailSample?.manifest?.source_import_id;
    const sourcePath = selectedDetailSample?.manifest?.source || selectedDetailSample?.manifest?.source_path;
    return imports.find((item) => item.import_id === sourceImportId || item.path === sourcePath) || null;
  }, [imports, selectedDetailSample]);
  const workflow = useMemo(
    () => computeSampleWorkflow({
      assetsLoading,
      imports,
      samples,
      selectedSample: selectedBatchSample,
      action: activeAction || (actionError ? lastAction : ""),
      actionError,
    }),
    [assetsLoading, imports, samples, selectedBatchSample, activeAction, lastAction, actionError],
  );
  const listView = useMemo(
    () => computeSamplesListView({ assetsLoading, imports, samples }),
    [assetsLoading, imports, samples],
  );
  const detailActions = useMemo(
    () => computeSampleDetailActions({ assetsLoading, busy, sample: selectedDetailSample }),
    [assetsLoading, busy, selectedDetailSample],
  );
  const selectedAuditEvents = useMemo(
    () => filterSampleAuditEvents(auditEvents, selectedDetailSample?.sample_id),
    [auditEvents, selectedDetailSample],
  );

  const selectedSampleRows = Number(selectedBatchSample?.manifest?.rows || 0);
  const overlapRateNumber = Number(overlapRate);
  const projectedOverlapItems =
    selectedSampleRows > 0 && Number.isFinite(overlapRateNumber)
      ? Math.ceil(selectedSampleRows * overlapRateNumber)
      : null;
  const dataLoadingText = "正在读取导入资产/样本集";
  const actionDisabled = busy || assetsLoading;
  const sampleActionDisabled = actionDisabled || !samples.length;
  const createSampleDisabled = actionDisabled || !imports.length;
  const selectedSampleHasBatchPlan = hasBatchPlan(selectedBatchSample);

  useEffect(() => {
    if (!source && imports.length) {
      setSource(imports[0].path);
    }
  }, [imports, source]);

  useEffect(() => {
    if (!selectedImport) return;
    if (sampleAuto) {
      setSampleId(sampleIdFromImport(selectedImport.import_id));
    }
    setRows((current) => {
      const numeric = Number(current);
      if (!current || numeric === 6) return selectedImport.rows || current;
      return current;
    });
  }, [sampleAuto, selectedImport]);

  useEffect(() => {
    if (!samples.length) {
      setBatchSample("");
      return;
    }
    if (!samples.some((item) => item.path === batchSample)) {
      setBatchSample(newestSample(samples)?.path || samples[0].path);
    }
  }, [samples, batchSample]);

  useEffect(() => {
    if (drawer === "detail" && detailSamplePath && !selectedDetailSample) {
      setDrawer("");
      setDetailSamplePath("");
    }
  }, [drawer, detailSamplePath, selectedDetailSample]);

  function openCreateDrawer() {
    setDrawer("create");
    setNotice("");
  }

  function openSampleDetail(sample) {
    if (!sample?.path) return;
    setDetailSamplePath(sample.path);
    setBatchSample(sample.path);
    setDrawer("detail");
    setNotice("");
  }

  async function runAction(action, params, label) {
    if (!task) return false;
    setBusy(true);
    setActiveAction(action);
    setLastAction(action);
    setActionError("");
    try {
      const job = await api.startAction(task.path, action, params);
      const finished = job?.id ? await api.waitForJob(taskId, job.id) : null;
      if (finished?.status === "failed") {
        throw new Error(finished.error || "执行失败");
      }
      await reload();
      return finished?.result || job?.result || job || {};
    } catch (e) {
      setActionError(String(e));
      onError(`${label}: ${e}`);
      return null;
    } finally {
      setBusy(false);
      setActiveAction("");
    }
  }

  async function createSample() {
    if (assetsLoading) { onError("正在读取导入资产/样本集，请稍候"); return; }
    if (!imports.length) { onError("当前任务没有导入资产，请先进入数据导入页完成导入"); return; }
    if (!task || !sampleId) { onError("请填写样本编号"); return; }
    const pendingSampleId = sampleId;
    const existedBefore = samples.some((item) => item.sample_id === pendingSampleId);
    setNotice("正在创建样本集...");
    const result = await runAction("sample", {
      sample_id: sampleId,
      rows: Number(rows),
      strategy,
      source,
      source_import_id: selectedImport?.import_id,
    }, "创建样本");
    if (result) {
      setSampleId("");
      setNotice(sampleCompletionNotice({ existedBefore, result }));
      if (result.artifact) {
        setBatchSample(result.artifact);
        setDetailSamplePath(result.artifact);
        setDrawer("detail");
      } else {
        setDrawer("");
      }
    }
  }

  async function runBatch() {
    if (!task) return;
    if (assetsLoading) { onError("正在读取导入资产/样本集，请稍候"); return; }
    if (!samples.length) { onError("请先创建样本，再生成批次"); return; }
    if (!batchSample) { onError("请选择要生成批次的样本"); return; }
    const numericBatchSize = Number(batchSize);
    const numericOverlapRate = Number(overlapRate);
    const numericMinAnnotators = Number(minAnnotatorsPerOverlapItem);
    const numericGoldRate = Number(goldRate);
    if (!Number.isFinite(numericBatchSize) || numericBatchSize < 1) { onError("每批行数必须大于 0"); return; }
    if (!Number.isFinite(numericOverlapRate) || numericOverlapRate < 0 || numericOverlapRate > 1) { onError("重叠比例需在 0 到 1 之间"); return; }
    if (!Number.isFinite(numericMinAnnotators) || numericMinAnnotators < 1) { onError("重叠样本最少标注人数必须大于 0"); return; }
    if (!Number.isFinite(numericGoldRate) || numericGoldRate < 0 || numericGoldRate > 1) { onError("控制样本比例需在 0 到 1 之间"); return; }
    setNotice("正在生成批次计划...");
    const result = await runAction("batch", {
      sample: batchSample,
      batch_size: numericBatchSize,
      overlap_rate: numericOverlapRate,
      min_annotators_per_overlap_item: numericMinAnnotators,
      gold_rate: numericGoldRate,
    }, "生成批次");
    if (result) {
      setNotice("批次生成任务已完成。");
    }
  }

  async function archiveSample(sample) {
    if (assetsLoading) return;
    if (!sample?.sample_id) return;
    const deps = sample.dependencies || [];
    if (deps.length) {
      onError(`样本已被下游资产使用，不能归档：${deps.map((dep) => `${dep.kind}:${dep.id}`).join(", ")}`);
      return;
    }
    const ok = window.confirm(`归档样本 ${sample.sample_id}？\n\n归档会从当前列表移除，但不会删除样本文件；文件会移动到 runs 下的 _archive 目录。`);
    if (!ok) return;
    setBusy(true);
    setNotice("");
    try {
      await api.archiveSample(taskId, sample.sample_id, "panel archive");
      setNotice(`已归档样本：${sample.sample_id}`);
      await reload();
      setDrawer("");
      setDetailSamplePath("");
    } catch (error) {
      onError(String(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <div className="crumbs"><Link to="/">全部任务</Link> / <Link to={`/task/${encodeURIComponent(taskId)}`}>{taskId}</Link> / 样本管理</div>
      <div className="page-header canvas-page-header">
        <div>
          <h2>样本管理</h2>
          <p>管理样本集资源，进入详情后配置批次和后续标注分发。</p>
        </div>
        <div className="action-row">
          <button className="btn btn-primary" disabled={assetsLoading || !imports.length} onClick={openCreateDrawer}>
            创建样本集
          </button>
          <button className="btn" disabled={assetsLoading} onClick={reload}>刷新</button>
        </div>
      </div>
      {notice && <div className="status-banner">{notice}</div>}
      <div className="card section-card">
        <div className="toolbar">
          <div>
            <h3>样本集（{samples.length}）</h3>
            <div className="status-line">
              {assetsLoading ? dataLoadingText : samples.length ? "点击样本行查看 manifest、批次计划和可执行动作。" : listView.emptyReason}
            </div>
          </div>
          <span className={`badge badge-${listView.status === "loading" ? "blue" : samples.length ? "green" : "red"}`}>
            {assetsLoading ? "读取中" : samples.length ? "列表就绪" : "暂无样本"}
          </span>
        </div>
        {assetsLoading && <div className="status-line">{dataLoadingText}</div>}
        {listView.showEmpty && (
          <div className="empty action-empty">
            <span>{listView.emptyReason}</span>
            {listView.canCreate ? (
              <button className="btn btn-primary" onClick={openCreateDrawer}>创建样本集</button>
            ) : (
              <Link className="btn btn-primary" to={`/task/${encodeURIComponent(taskId)}/imports`}>进入数据导入</Link>
            )}
          </div>
        )}
        {listView.showList && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>样本集</th>
                  <th>状态</th>
                  <th>行数</th>
                  <th>抽样策略</th>
                  <th>来源导入</th>
                  <th>批次计划</th>
                  <th>一致性策略</th>
                  <th>关联资产</th>
                  <th>创建时间</th>
                  <th>存储路径</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {samples.map((s) => {
                  const summary = batchSummary(s);
                  return (
                    <tr className="clickable-row" key={s.sample_id} onClick={() => openSampleDetail(s)}>
                      <td>
                        <strong>{s.sample_id}</strong>
                        <div className="status-line mono-cell">{shortHash(s.manifest?.content_sha256)}</div>
                      </td>
                      <td><span className={`badge ${sampleStateLabel(s) === "可用" ? "badge-green" : "badge-gray"}`}>{sampleStateLabel(s)}</span></td>
                      <td>{s.manifest ? s.manifest.rows : "-"}</td>
                      <td>{formatStrategy(s.manifest?.strategy)}</td>
                      <td>{s.manifest?.source_import_id || "-"}</td>
                      <td>{summary.batchText}</td>
                      <td className="text-cell">{summary.policyText}</td>
                      <td>{sampleDependencies(s)}</td>
                      <td className="muted">{sampleCreatedAt(s)}</td>
                      <td className="muted path-cell">{s.path}</td>
                      <td>
                        <div className="action-row">
                          <button className="btn btn-sm" onClick={(event) => { event.stopPropagation(); openSampleDetail(s); }}>详情</button>
                          <button className="btn btn-sm" disabled={actionDisabled} onClick={(event) => { event.stopPropagation(); openSampleDetail(s); }}>
                            生成批次
                          </button>
                        </div>
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
        <h3>状态摘要</h3>
        <div className="workflow-stage-list">
          <section className={`workflow-stage workflow-stage-${workflow.imports.status}`}>
            <div className="workflow-stage-index">1</div>
            <div className="workflow-stage-main">
              <div className="workflow-stage-head">
                <div>
                  <h4>已导入数据</h4>
                  <p>{assetsLoading ? dataLoadingText : imports.length ? `当前任务有 ${imports.length} 个导入资产可作为来源。` : "当前任务尚未检测到导入资产，可先进入数据导入页。"}</p>
                </div>
                <div className="workflow-stage-actions">
                  <span className={`badge badge-${workflow.imports.badgeTone}`}>{workflow.imports.badge}</span>
                  <Link className="btn btn-sm" to={`/task/${encodeURIComponent(taskId)}/imports`}>查看导入</Link>
                </div>
              </div>
            </div>
          </section>

          <section className={`workflow-stage workflow-stage-${workflow.sample.status}`}>
            <div className="workflow-stage-index">2</div>
            <div className="workflow-stage-main">
              <div className="workflow-stage-head">
                <div>
                  <h4>创建/选择样本集</h4>
                  <p>{assetsLoading ? dataLoadingText : activeAction === "sample" ? "正在创建样本集..." : samples.length ? `已有 ${samples.length} 个样本集，可选择其中一个进入批次配置。` : imports.length ? "先从来源数据创建一个样本集。" : "需要先完成数据导入。"}</p>
                </div>
                <div className="workflow-stage-actions">
                  <span className={`badge badge-${workflow.sample.badgeTone}`}>{workflow.sample.badge}</span>
                  <button className="btn btn-sm" disabled={createSampleDisabled} onClick={openCreateDrawer}>创建样本集</button>
                </div>
              </div>
              {!assetsLoading && !imports.length && (
                <div className="stage-tip">
                  当前任务没有导入资产，请先<Link to={`/task/${encodeURIComponent(taskId)}/imports`}>进入数据导入</Link>完成导入。
                </div>
              )}
            </div>
          </section>

          <section className={`workflow-stage workflow-stage-${workflow.batchConfig.status}`}>
            <div className="workflow-stage-index">3</div>
            <div className="workflow-stage-main">
              <div className="workflow-stage-head">
                <div>
                  <h4>配置批次与一致性策略</h4>
                  <p>{assetsLoading ? dataLoadingText : selectedBatchSample ? `当前样本：${selectedBatchSample.sample_id}` : "需要先创建并选择样本。"}</p>
                </div>
                <div className="workflow-stage-actions">
                  <span className={`badge badge-${workflow.batchConfig.badgeTone}`}>{workflow.batchConfig.badge}</span>
                  <button className="btn btn-sm" disabled={actionDisabled || !samples.length} onClick={() => selectedBatchSample && openSampleDetail(selectedBatchSample)}>打开详情</button>
                </div>
              </div>
              <div className="policy-preview">
                <div><span>预计重叠样本</span><strong>{projectedOverlapItems === null ? "选择样本后计算" : `${projectedOverlapItems} 条`}</strong></div>
                <div><span>当前策略</span><strong>重叠 {formatPercent(overlapRate)}；每条至少 {minAnnotatorsPerOverlapItem || "-"} 人；控制样本 {formatPercent(goldRate)}</strong></div>
                <div><span>已有批次记录</span><strong>{selectedBatchSummary.hasBatchInfo ? `${selectedBatchSummary.batchText}；重叠 ${selectedBatchSummary.overlapText}` : "API 未返回批次 manifest 时显示为未记录"}</strong></div>
              </div>
            </div>
          </section>

          <section className={`workflow-stage workflow-stage-${workflow.batch.status}`}>
            <div className="workflow-stage-index">4</div>
            <div className="workflow-stage-main">
              <div className="workflow-stage-head">
                <div>
                  <h4>生成批次</h4>
                  <p>{assetsLoading ? dataLoadingText : activeAction === "batch" ? "正在生成批次计划..." : selectedSampleHasBatchPlan ? "当前样本已有批次计划，可继续推送 Argilla。" : samples.length ? "按当前批次配置生成批次资产。" : "请先创建样本，再生成批次。"}</p>
                </div>
                <div className="workflow-stage-actions">
                  <span className={`badge badge-${workflow.batch.badgeTone}`}>{workflow.batch.badge}</span>
                  <button className="btn btn-sm btn-primary" disabled={actionDisabled || !samples.length || !batchSample} onClick={() => selectedBatchSample && openSampleDetail(selectedBatchSample)}>
                    {activeAction === "batch" ? "正在生成批次计划..." : "生成批次"}
                  </button>
                </div>
              </div>
              {!assetsLoading && !samples.length && <div className="stage-tip">没有可用样本，批次生成已禁用。</div>}
            </div>
          </section>

          <section className={`workflow-stage workflow-stage-${workflow.argilla.status}`}>
            <div className="workflow-stage-index">5</div>
            <div className="workflow-stage-main">
              <div className="workflow-stage-head">
                <div>
                  <h4>下一步推送 Argilla</h4>
                  <p>{assetsLoading ? dataLoadingText : selectedSampleHasBatchPlan ? "批次计划已生成，可进入标注分发页推送样本。" : samples.length ? "请先生成批次计划，再进入标注分发页推送样本。" : "需要先完成样本创建和批次生成。"}</p>
                </div>
                <div className="workflow-stage-actions">
                  <span className={`badge badge-${workflow.argilla.badgeTone}`}>{workflow.argilla.badge}</span>
                  {selectedSampleHasBatchPlan ? (
                    <Link className="btn btn-accent" to={`/task/${encodeURIComponent(taskId)}/annotations`}>进入标注分发</Link>
                  ) : (
                    <button className="btn" disabled title={workflow.argilla.disabledReason || "请先生成批次计划"}>进入标注分发</button>
                  )}
                </div>
              </div>
              {!assetsLoading && workflow.argilla.disabledReason && <div className="stage-tip">{workflow.argilla.disabledReason}</div>}
            </div>
          </section>
        </div>
      </div>

      {drawer === "create" && (
        <div className="drawer-backdrop" onClick={() => setDrawer("")}>
          <aside className="drawer-panel" onClick={(event) => event.stopPropagation()}>
            <div className="drawer-head">
              <div>
                <h3>创建样本集</h3>
                <p>从已有导入资产生成一个样本集，完成后回到样本列表管理。</p>
              </div>
              <button className="btn btn-sm" type="button" onClick={() => setDrawer("")}>关闭</button>
            </div>
            <div className="form-grid drawer-form-grid">
              <div className="field field-half"><label>样本编号</label><input value={sampleId} disabled={actionDisabled} onChange={(e) => setSampleId(e.target.value)} placeholder="例如 seed_v1" /></div>
              <div className="field field-half">
                <label>数据来源</label>
                <select value={source} disabled={actionDisabled} onChange={(e) => { setSource(e.target.value); setSampleAuto(true); }}>
                  <option value="">任务配置中的原始数据</option>
                  {imports.map((item) => (
                    <option key={item.import_id} value={item.path}>{item.import_id} · {item.rows} 行</option>
                  ))}
                </select>
              </div>
              <div className="field field-half">
                <label>编号方式</label>
                <select value={sampleAuto ? "auto" : "manual"} disabled={actionDisabled} onChange={(e) => setSampleAuto(e.target.value === "auto")}>
                  <option value="auto">按导入编号自动生成</option>
                  <option value="manual">手动填写样本编号</option>
                </select>
              </div>
              <div className="field field-half"><label>抽样行数</label><input type="number" min="1" value={rows} disabled={actionDisabled} onChange={(e) => setRows(e.target.value)} /></div>
              <div className="field field-half"><label>抽样策略</label><select value={strategy} disabled={actionDisabled} onChange={(e) => setStrategy(e.target.value)}><option value="head">前 N 行</option><option value="random">随机抽样</option></select></div>
            </div>
            {!assetsLoading && !imports.length && (
              <div className="stage-tip">
                当前任务没有导入资产，请先<Link to={`/task/${encodeURIComponent(taskId)}/imports`}>进入数据导入</Link>完成导入。
              </div>
            )}
            <div className="drawer-actions">
              <button className="btn btn-primary" disabled={createSampleDisabled} onClick={createSample}>
                {activeAction === "sample" ? "正在创建样本集..." : "创建样本集"}
              </button>
              <span className={`badge badge-${workflow.sample.badgeTone}`}>{workflow.sample.badge}</span>
            </div>
          </aside>
        </div>
      )}

      {drawer === "detail" && selectedDetailSample && (
        <div className="drawer-backdrop" onClick={() => setDrawer("")}>
          <aside className="drawer-panel" onClick={(event) => event.stopPropagation()}>
            <div className="drawer-head">
              <div>
                <h3>{selectedDetailSample.sample_id}</h3>
                <p>样本 manifest、来源导入、批次计划、依赖与后续动作。</p>
              </div>
              <button className="btn btn-sm" type="button" onClick={() => setDrawer("")}>关闭</button>
            </div>

            <div className="drawer-detail-grid">
              <DetailField label="状态" value={sampleStateLabel(selectedDetailSample)} />
              <DetailField label="创建时间" value={sampleCreatedAt(selectedDetailSample)} />
              <DetailField label="行数" value={selectedDetailSample.manifest?.rows} />
              <DetailField label="抽样策略" value={formatStrategy(selectedDetailSample.manifest?.strategy)} />
              <DetailField label="来源导入" value={selectedDetailSample.manifest?.source_import_id || detailSourceImport?.import_id} />
              <DetailField label="内容哈希" value={shortHash(selectedDetailSample.manifest?.content_sha256)} className="mono-cell" />
              <DetailField label="批次计划" value={selectedDetailSummary.batchText} />
              <DetailField label="重叠样本" value={selectedDetailSummary.overlapText} />
              <DetailField label="一致性策略" value={selectedDetailSummary.policyText} />
              <DetailField label="依赖" value={sampleDependencies(selectedDetailSample)} />
              <DetailField label="样本路径" value={selectedDetailSample.path} className="mono-cell" />
              <DetailField label="manifest 路径" value={selectedDetailSample.manifest_path || selectedDetailSample.manifest?.manifest_path} className="mono-cell" />
            </div>

            <details className="secondary-panel">
              <summary>样本 manifest</summary>
              <pre className="log-box">{manifestJson(selectedDetailSample.manifest)}</pre>
            </details>

            <details className="secondary-panel" open>
              <summary>资产审计</summary>
              {auditLoadError && <div className="status-line danger-line">审计事件读取失败：{auditLoadError}</div>}
              {!auditLoadError && !selectedAuditEvents.length && <div className="empty">暂无该样本的审计事件</div>}
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

            <details className="secondary-panel" open>
              <summary>来源导入</summary>
              {detailSourceImport ? (
                <div className="drawer-detail-grid">
                  <DetailField label="导入编号" value={detailSourceImport.import_id} />
                  <DetailField label="行数" value={detailSourceImport.rows} />
                  <DetailField label="导入路径" value={detailSourceImport.path} className="mono-cell" />
                  <DetailField label="状态" value={detailSourceImport.state || "active"} />
                </div>
              ) : (
                <div className="status-line">API 未返回匹配的来源导入详情。</div>
              )}
            </details>

            <details className="secondary-panel" open>
              <summary>批次计划</summary>
              {latestDetailBatchPlan ? (
                <>
                  <div className="batch-summary-callout">
                    <span>最新计划</span>
                    <strong>{formatBatchPlanSummary(latestDetailBatchPlan)}</strong>
                  </div>
                  <div className="plan-summary-grid">
                    {batchPlanDebugFields(latestDetailBatchPlan).map(([key, value]) => (
                      <div key={key}><span>{key}</span><strong>{displayPlanValue(value)}</strong></div>
                    ))}
                  </div>
                </>
              ) : (
                <div className="stage-tip">该样本集还没有批次计划，请先生成批次。</div>
              )}
            </details>

            <details className="secondary-panel" open>
              <summary>生成批次</summary>
              <div className="form-grid drawer-form-grid">
                <div className="field field-half">
                  <label>每批行数</label>
                  <input type="number" min="1" value={batchSize} disabled={sampleActionDisabled} onChange={(e) => setBatchSize(e.target.value)} />
                </div>
                <div className="field field-half">
                  <label>overlap_rate</label>
                  <input type="number" min="0" max="1" step="0.01" value={overlapRate} disabled={sampleActionDisabled} onChange={(e) => setOverlapRate(e.target.value)} />
                  <span className="hint">默认 0.1，用于抽取重叠样本做一致性检查。</span>
                </div>
                <div className="field field-half">
                  <label>min_annotators_per_overlap_item</label>
                  <input type="number" min="1" step="1" value={minAnnotatorsPerOverlapItem} disabled={sampleActionDisabled} onChange={(e) => setMinAnnotatorsPerOverlapItem(e.target.value)} />
                  <span className="hint">默认 2，表示每条重叠样本至少分配给多少标注者。</span>
                </div>
                <div className="field field-half">
                  <label>gold_rate</label>
                  <input type="number" min="0" max="1" step="0.01" value={goldRate} disabled />
                  <span className="hint">默认 0；暂不启用，预留为可选控制样本比例。</span>
                </div>
              </div>
              <div className="policy-preview">
                <div><span>预计重叠样本</span><strong>{projectedOverlapItems === null ? "选择样本后计算" : `${projectedOverlapItems} 条`}</strong></div>
                <div><span>当前策略</span><strong>重叠 {formatPercent(overlapRate)}；每条至少 {minAnnotatorsPerOverlapItem || "-"} 人；控制样本 {formatPercent(goldRate)}</strong></div>
                <div><span>已有批次记录</span><strong>{selectedDetailSummary.hasBatchInfo ? `${selectedDetailSummary.batchText}；重叠 ${selectedDetailSummary.overlapText}` : "未记录"}</strong></div>
              </div>
              <div className="drawer-actions">
                <button className="btn btn-primary" disabled={!detailActions.generateBatch.enabled} onClick={runBatch}>
                  {activeAction === "batch" ? "正在生成批次计划..." : "生成批次"}
                </button>
                <span className={`badge badge-${workflow.batch.badgeTone}`}>{workflow.batch.badge}</span>
              </div>
              {!detailActions.generateBatch.enabled && detailActions.generateBatch.disabledReason && (
                <div className="status-line danger-line">{detailActions.generateBatch.disabledReason}</div>
              )}
            </details>

            <div className="drawer-actions">
              {detailActions.pushArgilla.enabled ? (
                <Link className="btn btn-accent" to={`/task/${encodeURIComponent(taskId)}/annotations`}>下一步推送 Argilla</Link>
              ) : (
                <button className="btn" disabled title={detailActions.pushArgilla.disabledReason}>下一步推送 Argilla</button>
              )}
              <button className="btn btn-danger" disabled={!detailActions.archive.enabled} onClick={() => archiveSample(selectedDetailSample)}>归档样本集</button>
            </div>
            {!detailActions.pushArgilla.enabled && detailActions.pushArgilla.disabledReason && (
              <div className="stage-tip">{detailActions.pushArgilla.disabledReason}</div>
            )}
            {!detailActions.archive.enabled && detailActions.archive.disabledReason && selectedDetailSample && (
              <div className="status-line danger-line">{detailActions.archive.disabledReason}</div>
            )}
          </aside>
        </div>
      )}
    </div>
  );
}
