import React, { useEffect, useMemo, useState, useCallback } from "react";
import * as api from "./../api.js";
import { Link } from "./../router.jsx";

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

function getBatchManifests(sample) {
  const manifests = [];
  const pushManifest = (value) => {
    if (!value) return;
    if (Array.isArray(value)) {
      value.forEach(pushManifest);
      return;
    }
    if (typeof value === "object") manifests.push(value);
  };
  pushManifest(sample?.latest_batch_manifest);
  pushManifest(sample?.batch_manifest);
  pushManifest(sample?.batch);
  pushManifest(sample?.batches);
  pushManifest(sample?.manifest?.batch_manifest);
  pushManifest(sample?.manifest?.batch);
  if (sample?.manifest?.batch_count || sample?.manifest?.batch_size || sample?.manifest?.batches) {
    pushManifest(sample.manifest);
  }
  return manifests;
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

export default function SamplesPage({ task, taskId, onError }) {
  const [samples, setSamples] = useState([]);
  const [imports, setImports] = useState([]);
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
  const [notice, setNotice] = useState("");

  const reload = useCallback(async () => {
    if (!taskId) {
      setAssetsLoading(false);
      return;
    }
    setAssetsLoading(true);
    try {
      const [s, i] = await Promise.all([api.getTaskSamples(taskId), api.getImports(taskId)]);
      setSamples(s.samples || []);
      setImports(i.imports || []);
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

  const selectedBatchSummary = useMemo(
    () => batchSummary(selectedBatchSample),
    [selectedBatchSample],
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
      setBatchSample(samples[0].path);
    }
  }, [samples, batchSample]);

  async function runAction(action, params, label) {
    if (!task) return false;
    setBusy(true);
    try {
      const job = await api.startAction(task.path, action, params);
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

  async function createSample() {
    if (assetsLoading) { onError("正在读取导入资产/样本集，请稍候"); return; }
    if (!task || !sampleId) { onError("请填写样本编号"); return; }
    setNotice("");
    const ok = await runAction("sample", {
      sample_id: sampleId,
      rows: Number(rows),
      strategy,
      source,
      source_import_id: selectedImport?.import_id,
    }, "创建样本");
    if (ok) {
      setSampleId("");
      setNotice("样本已创建或幂等复用。");
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
    const ok = await runAction("batch", {
      sample: batchSample,
      batch_size: numericBatchSize,
      overlap_rate: numericOverlapRate,
      min_annotators_per_overlap_item: numericMinAnnotators,
      gold_rate: numericGoldRate,
    }, "生成批次");
    if (ok) {
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
    } catch (error) {
      onError(String(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <div className="crumbs"><Link to="/">全部任务</Link> / <Link to={`/task/${encodeURIComponent(taskId)}`}>{taskId}</Link> / 样本管理</div>
      <div className="page-header">
        <h2>样本管理</h2>
        <p>从导入数据生成样本集，配置批次和一致性策略后进入标注分发</p>
      </div>
      {notice && <div className="status-banner">{notice}</div>}
      <div className="card section-card">
        <h3>样本到批次流程</h3>
        <div className="workflow-stage-list">
          <section className={`workflow-stage ${imports.length ? "workflow-stage-completed" : "workflow-stage-ready"}`}>
            <div className="workflow-stage-index">1</div>
            <div className="workflow-stage-main">
              <div className="workflow-stage-head">
                <div>
                  <h4>已导入数据</h4>
                  <p>{assetsLoading ? dataLoadingText : imports.length ? `当前任务有 ${imports.length} 个导入资产可作为来源。` : "当前任务尚未检测到导入资产，可先进入数据导入页。"}</p>
                </div>
                <div className="workflow-stage-actions">
                  <span className={`badge ${imports.length ? "badge-green" : "badge-blue"}`}>{assetsLoading ? "读取中" : imports.length ? "已就绪" : "待导入"}</span>
                  <Link className="btn btn-sm" to={`/task/${encodeURIComponent(taskId)}/imports`}>查看导入</Link>
                </div>
              </div>
            </div>
          </section>

          <section className="workflow-stage workflow-stage-ready">
            <div className="workflow-stage-index">2</div>
            <div className="workflow-stage-main">
              <div className="workflow-stage-head">
                <div>
                  <h4>创建/选择样本集</h4>
                  <p>{assetsLoading ? dataLoadingText : samples.length ? `已有 ${samples.length} 个样本集，可选择其中一个进入批次配置。` : "先从来源数据创建一个样本集。"}</p>
                </div>
                <div className="workflow-stage-actions">
                  <span className={`badge ${samples.length ? "badge-green" : "badge-blue"}`}>{assetsLoading ? "读取中" : samples.length ? "已有样本" : "可创建"}</span>
                </div>
              </div>
              <div className="form-grid workflow-form-grid">
                <div className="field"><label>样本编号</label><input value={sampleId} onChange={(e) => setSampleId(e.target.value)} placeholder="例如 seed_v1" /></div>
                <div className="field">
                  <label>数据来源</label>
                  <select value={source} disabled={assetsLoading} onChange={(e) => { setSource(e.target.value); setSampleAuto(true); }}>
                    <option value="">任务配置中的原始数据</option>
                    {imports.map((item) => (
                      <option key={item.import_id} value={item.path}>{item.import_id} · {item.rows} 行</option>
                    ))}
                  </select>
                </div>
                <div className="field">
                  <label>编号方式</label>
                  <select value={sampleAuto ? "auto" : "manual"} disabled={assetsLoading} onChange={(e) => setSampleAuto(e.target.value === "auto")}>
                    <option value="auto">按导入编号自动生成</option>
                    <option value="manual">手动填写样本编号</option>
                  </select>
                </div>
                <div className="field"><label>抽样行数</label><input type="number" min="1" value={rows} onChange={(e) => setRows(e.target.value)} /></div>
                <div className="field"><label>抽样策略</label><select value={strategy} onChange={(e) => setStrategy(e.target.value)}><option value="head">前 N 行</option><option value="random">随机抽样</option></select></div>
                <div className="field">
                  <label>后续使用样本</label>
                  <select value={batchSample} disabled={assetsLoading || !samples.length} onChange={(e) => setBatchSample(e.target.value)}>
                    <option value="">{assetsLoading ? "正在读取样本" : samples.length ? "选择样本" : "请先创建样本"}</option>
                    {samples.map((s) => <option key={s.sample_id} value={s.path}>{s.sample_id}</option>)}
                  </select>
                  <span className="hint">{assetsLoading ? dataLoadingText : samples.length ? "所选样本将用于批次生成和后续标注分发。" : "没有样本时不能生成批次。"}</span>
                </div>
              </div>
              <button className="btn btn-primary" disabled={actionDisabled} onClick={createSample}>创建样本</button>
            </div>
          </section>

          <section className={`workflow-stage ${samples.length || assetsLoading ? "workflow-stage-ready" : "workflow-stage-blocked"}`}>
            <div className="workflow-stage-index">3</div>
            <div className="workflow-stage-main">
              <div className="workflow-stage-head">
                <div>
                  <h4>配置批次与一致性策略</h4>
                  <p>{assetsLoading ? dataLoadingText : selectedBatchSample ? `当前样本：${selectedBatchSample.sample_id}` : "需要先创建并选择样本。"}</p>
                </div>
                <div className="workflow-stage-actions">
                  <span className={`badge ${samples.length || assetsLoading ? "badge-blue" : "badge-red"}`}>{assetsLoading ? "读取中" : samples.length ? "可配置" : "待样本"}</span>
                </div>
              </div>
              <div className="form-grid workflow-form-grid">
                <div className="field">
                  <label>每批行数</label>
                  <input type="number" min="1" value={batchSize} disabled={sampleActionDisabled} onChange={(e) => setBatchSize(e.target.value)} />
                </div>
                <div className="field">
                  <label>overlap_rate</label>
                  <input type="number" min="0" max="1" step="0.01" value={overlapRate} disabled={sampleActionDisabled} onChange={(e) => setOverlapRate(e.target.value)} />
                  <span className="hint">默认 0.1，用于抽取重叠样本做一致性检查。</span>
                </div>
                <div className="field">
                  <label>min_annotators_per_overlap_item</label>
                  <input type="number" min="1" step="1" value={minAnnotatorsPerOverlapItem} disabled={sampleActionDisabled} onChange={(e) => setMinAnnotatorsPerOverlapItem(e.target.value)} />
                  <span className="hint">默认 2，表示每条重叠样本至少分配给多少标注者。</span>
                </div>
                <div className="field">
                  <label>gold_rate</label>
                  <input type="number" min="0" max="1" step="0.01" value={goldRate} disabled />
                  <span className="hint">默认 0；暂不启用，预留为可选控制样本比例。</span>
                </div>
              </div>
              <div className="policy-preview">
                <div><span>预计重叠样本</span><strong>{projectedOverlapItems === null ? "选择样本后计算" : `${projectedOverlapItems} 条`}</strong></div>
                <div><span>当前策略</span><strong>重叠 {formatPercent(overlapRate)}；每条至少 {minAnnotatorsPerOverlapItem || "-"} 人；控制样本 {formatPercent(goldRate)}</strong></div>
                <div><span>已有批次记录</span><strong>{selectedBatchSummary.hasBatchInfo ? `${selectedBatchSummary.batchText}；重叠 ${selectedBatchSummary.overlapText}` : "API 未返回批次 manifest 时显示为未记录"}</strong></div>
              </div>
            </div>
          </section>

          <section className={`workflow-stage ${samples.length || assetsLoading ? "workflow-stage-ready" : "workflow-stage-blocked"}`}>
            <div className="workflow-stage-index">4</div>
            <div className="workflow-stage-main">
              <div className="workflow-stage-head">
                <div>
                  <h4>生成批次</h4>
                  <p>{assetsLoading ? dataLoadingText : samples.length ? "按当前批次配置生成批次资产。" : "请先创建样本，再生成批次。"}</p>
                </div>
                <div className="workflow-stage-actions">
                  <button className="btn btn-primary" disabled={actionDisabled || !samples.length || !batchSample} onClick={runBatch}>生成批次</button>
                </div>
              </div>
              {!assetsLoading && !samples.length && <div className="stage-tip">没有可用样本，批次生成已禁用。</div>}
            </div>
          </section>

          <section className={`workflow-stage ${samples.length || assetsLoading ? "workflow-stage-ready" : "workflow-stage-blocked"}`}>
            <div className="workflow-stage-index">5</div>
            <div className="workflow-stage-main">
              <div className="workflow-stage-head">
                <div>
                  <h4>下一步推送 Argilla</h4>
                  <p>{assetsLoading ? dataLoadingText : samples.length ? "批次生成后，进入标注分发页推送样本。" : "需要先完成样本创建和批次生成。"}</p>
                </div>
                <div className="workflow-stage-actions">
                  {samples.length ? (
                    <Link className="btn btn-accent" to={`/task/${encodeURIComponent(taskId)}/annotations`}>进入标注分发</Link>
                  ) : (
                    <button className="btn" disabled>进入标注分发</button>
                  )}
                </div>
              </div>
            </div>
          </section>
        </div>
      </div>
      <div className="card">
        <div className="toolbar"><h3>已有样本（{samples.length}）</h3><button className="btn btn-sm" disabled={assetsLoading} onClick={reload}>刷新</button></div>
        {assetsLoading && <div className="empty">{dataLoadingText}</div>}
        {!assetsLoading && !samples.length && <div className="empty">暂无样本</div>}
        {samples.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead><tr><th>样本编号</th><th>行数</th><th>抽样策略</th><th>来源导入</th><th>内容哈希</th><th>批次信息</th><th>重叠样本</th><th>一致性策略</th><th>关联资产</th><th>存储路径</th><th>操作</th></tr></thead>
              <tbody>
                {samples.map((s) => {
                  const summary = batchSummary(s);
                  return (
                    <tr key={s.sample_id}>
                      <td>{s.sample_id}</td>
                      <td>{s.manifest ? s.manifest.rows : "-"}</td>
                      <td>{s.manifest?.strategy === "random" ? "随机抽样" : s.manifest?.strategy === "head" ? "前 N 行" : "-"}</td>
                      <td>{s.manifest?.source_import_id || "-"}</td>
                      <td className="mono-cell">{shortHash(s.manifest?.content_sha256)}</td>
                      <td>{summary.batchText}</td>
                      <td>{summary.overlapText}</td>
                      <td className="text-cell">{summary.policyText}</td>
                      <td>{(s.dependencies || []).map((dep) => `${dep.kind}:${dep.id}`).join(", ") || "-"}</td>
                      <td className="muted path-cell">{s.path}</td>
                      <td><button className="btn btn-sm btn-danger" disabled={actionDisabled || (s.dependencies || []).length > 0} onClick={() => archiveSample(s)}>归档</button></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
