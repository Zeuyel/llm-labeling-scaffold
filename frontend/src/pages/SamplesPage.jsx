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

export default function SamplesPage({ task, taskId, onError }) {
  const [samples, setSamples] = useState([]);
  const [imports, setImports] = useState([]);
  const [sampleId, setSampleId] = useState("");
  const [source, setSource] = useState("");
  const [rows, setRows] = useState(6);
  const [strategy, setStrategy] = useState("head");
  const [batchSize, setBatchSize] = useState(5);
  const [batchSample, setBatchSample] = useState("");
  const [sampleAuto, setSampleAuto] = useState(true);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");

  const reload = useCallback(async () => {
    if (!taskId) return;
    try {
      const [s, i] = await Promise.all([api.getTaskSamples(taskId), api.getImports(taskId)]);
      setSamples(s.samples || []);
      setImports(i.imports || []);
    } catch (e) { onError(String(e)); }
  }, [taskId, onError]);

  useEffect(() => { reload(); }, [reload]);

  const selectedImport = useMemo(
    () => imports.find((item) => item.path === source) || null,
    [imports, source],
  );

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
    if (!task || !batchSample) { onError("请选择要切分的样本"); return; }
    await runAction("batch", { sample: batchSample, batch_size: Number(batchSize) }, "切分批次");
  }

  async function archiveSample(sample) {
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
        <p>从导入数据生成标注样本；样本按不可覆盖资产管理，归档前会检查下游依赖</p>
      </div>
      {notice && <div className="status-banner">{notice}</div>}
      <div className="card section-card">
        <h3>创建样本</h3>
        <div className="form-grid">
          <div className="field"><label>样本编号</label><input value={sampleId} onChange={(e) => setSampleId(e.target.value)} placeholder="例如 seed_v1" /></div>
          <div className="field">
            <label>数据来源</label>
            <select value={source} onChange={(e) => { setSource(e.target.value); setSampleAuto(true); }}>
              <option value="">任务配置中的原始语料</option>
              {imports.map((item) => (
                <option key={item.import_id} value={item.path}>{item.import_id} · {item.rows} 行</option>
              ))}
            </select>
          </div>
          <div className="field">
            <label>编号方式</label>
            <select value={sampleAuto ? "auto" : "manual"} onChange={(e) => setSampleAuto(e.target.value === "auto")}>
              <option value="auto">按导入编号自动生成</option>
              <option value="manual">手动填写样本编号</option>
            </select>
          </div>
          <div className="field"><label>抽样行数</label><input type="number" min="1" value={rows} onChange={(e) => setRows(e.target.value)} /></div>
          <div className="field"><label>抽样策略</label><select value={strategy} onChange={(e) => setStrategy(e.target.value)}><option value="head">前 N 行</option><option value="random">随机抽样</option></select></div>
        </div>
        <button className="btn btn-primary" disabled={busy} onClick={createSample}>创建样本</button>
      </div>
      <div className="card section-card">
        <h3>切分批次</h3>
        <div className="form-grid">
          <div className="field"><label>样本</label><select value={batchSample} onChange={(e) => setBatchSample(e.target.value)}><option value="">选择样本</option>{samples.map((s) => <option key={s.sample_id} value={s.path}>{s.sample_id}</option>)}</select></div>
          <div className="field"><label>每批行数</label><input type="number" min="1" value={batchSize} onChange={(e) => setBatchSize(e.target.value)} /></div>
        </div>
        <button className="btn" disabled={busy} onClick={runBatch}>切分批次</button>
      </div>
      <div className="card">
        <div className="toolbar"><h3>已有样本（{samples.length}）</h3><button className="btn btn-sm" onClick={reload}>刷新</button></div>
        {!samples.length && <div className="empty">暂无样本</div>}
        {samples.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead><tr><th>样本编号</th><th>行数</th><th>抽样策略</th><th>来源导入</th><th>内容哈希</th><th>关联资产</th><th>存储路径</th><th>操作</th></tr></thead>
              <tbody>
                {samples.map((s) => (
                  <tr key={s.sample_id}>
                    <td>{s.sample_id}</td>
                    <td>{s.manifest ? s.manifest.rows : "-"}</td>
                    <td>{s.manifest?.strategy === "random" ? "随机抽样" : s.manifest?.strategy === "head" ? "前 N 行" : "-"}</td>
                    <td>{s.manifest?.source_import_id || "-"}</td>
                    <td className="mono-cell">{shortHash(s.manifest?.content_sha256)}</td>
                    <td>{(s.dependencies || []).map((dep) => `${dep.kind}:${dep.id}`).join(", ") || "-"}</td>
                    <td className="muted path-cell">{s.path}</td>
                    <td><button className="btn btn-sm btn-danger" disabled={busy || (s.dependencies || []).length > 0} onClick={() => archiveSample(s)}>归档</button></td>
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
