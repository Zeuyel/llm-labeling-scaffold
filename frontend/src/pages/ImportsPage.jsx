import React, { useCallback, useEffect, useMemo, useState } from "react";
import * as api from "./../api.js";
import { Link } from "./../router.jsx";

function shortHash(value) {
  return value ? `${String(value).slice(0, 12)}...` : "-";
}

function cellText(value) {
  if (value === null || value === undefined || value === "") return "-";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function stateLabel(value) {
  if (value === "active") return "可用";
  if (value === "archived") return "已归档";
  return value || "-";
}

export default function ImportsPage({ task, taskId, onError }) {
  const [items, setItems] = useState([]);
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

  const selected = useMemo(
    () => items.find((item) => item.import_id === selectedId) || null,
    [items, selectedId],
  );

  const reload = useCallback(async () => {
    if (!taskId) return;
    try {
      const data = await api.getImports(taskId);
      const next = data.imports || [];
      setItems(next);
      setSelectedId((current) => current || next[0]?.import_id || "");
    } catch (error) {
      onError(String(error));
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
  }, [task?.task_id, task?.data_lake]);

  useEffect(() => {
    if (!taskId || !selectedId) {
      setDetail(null);
      setRowsData({ rows: [], fields: [], total: 0, offset: 0, limit: 25 });
      return;
    }
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
      setNotice(imported.action === "reused" ? "导入内容与已有数据一致，已幂等复用原导入。" : "导入数据已保存。");
      setName("");
      setText("");
      setFileLabel("");
      await reload();
      setSelectedId(imported.import_id || name.trim());
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
      await reload();
    } catch (error) {
      onError(String(error));
    } finally {
      setBusy(false);
    }
  }

  async function checkDataLake() {
    if (!taskId) return;
    setLakeBusy(true);
    setNotice("");
    try {
      const data = await api.getDataLakeStatus(taskId);
      setLakeStatus(data.preview || null);
      if (!lakeImportId && task?.data_lake?.default_import_id) setLakeImportId(task.data_lake.default_import_id);
      setNotice("数据湖配置可读取。");
    } catch (error) {
      onError(String(error));
    } finally {
      setLakeBusy(false);
    }
  }

  async function importLake() {
    if (!taskId) return;
    setLakeBusy(true);
    setNotice("");
    try {
      const result = await api.importFromDataLake(taskId, { import_id: lakeImportId.trim() });
      const imported = result.import || result;
      setNotice(imported.action === "reused" ? "数据湖内容与已有导入一致，已幂等复用。" : "已从数据湖生成本地导入。");
      await reload();
      setSelectedId(imported.import_id || lakeImportId.trim());
    } catch (error) {
      onError(String(error));
    } finally {
      setLakeBusy(false);
    }
  }

  function searchRows() {
    loadRows(selectedId, { offset: 0, query });
  }

  function pageRows(delta) {
    const next = Math.max(0, (rowsData.offset || 0) + delta * (rowsData.limit || 25));
    loadRows(selectedId, { offset: next });
  }

  const fields = rowsData.fields?.length ? rowsData.fields : detail?.fields || [];

  return (
    <div>
      <div className="crumbs">
        <Link to="/">全部任务</Link> / <Link to={`/task/${encodeURIComponent(taskId)}`}>{taskId}</Link> / 数据导入
      </div>
      <div className="page-header">
        <h2>数据导入</h2>
        <p>导入数据按不可覆盖资产管理；同名同内容幂等复用，同名不同内容拒绝写入</p>
      </div>

      {notice && <div className="status-banner">{notice}</div>}

      {task?.data_lake && (
        <div className="card section-card">
          <div className="toolbar">
            <div>
              <h3>从数据湖导入</h3>
              <div className="status-line">按任务配置读取 R2 数据湖 manifest，并生成当前任务的本地导入缓存</div>
            </div>
            <button className="btn btn-sm" disabled={lakeBusy} onClick={checkDataLake}>检查配置</button>
          </div>
          <div className="form-grid">
            <div className="field">
              <label>目标导入编号</label>
              <input value={lakeImportId} onChange={(event) => setLakeImportId(event.target.value)} placeholder={task.data_lake.default_import_id || "留空则自动生成"} />
              <span className="hint">同名同内容会幂等复用，同名不同内容会拒绝写入。</span>
            </div>
            <div className="field">
              <label>源数据集</label>
              <input value={task.data_lake.source_dataset_id || "-"} readOnly />
            </div>
            <div className="field field-wide">
              <label>源对象</label>
              <input value={lakeStatus?.selected_object?.path || task.data_lake.source_object_path || "-"} readOnly />
            </div>
          </div>
          {lakeStatus && (
            <div className="data-profile">
              <div><span>数据层</span><strong>{lakeStatus.dataset?.layer || "-"}</strong></div>
              <div><span>领域</span><strong>{lakeStatus.dataset?.domain || "-"}</strong></div>
              <div><span>manifest 对象数</span><strong>{lakeStatus.manifest?.object_count ?? "-"}</strong></div>
              <div><span>选中对象大小</span><strong>{lakeStatus.selected_object?.bytes ?? "-"}</strong></div>
            </div>
          )}
          <button className="btn btn-primary" disabled={lakeBusy} onClick={importLake}>从数据湖生成导入</button>
        </div>
      )}

      <div className="card section-card">
        <h3>新增导入</h3>
        <div className="form-grid">
          <div className="field">
            <label>导入编号</label>
            <input value={name} onChange={(event) => setName(event.target.value)} placeholder="例如 patent_manual_seed_20260626" />
            <span className="hint">同一编号不能覆盖不同内容；修正数据请使用新的导入编号。</span>
          </div>
          <div className="field">
            <label>上传文件</label>
            <input type="file" accept=".jsonl,.ndjson,.json,.txt,application/json,application/x-ndjson,text/plain" onChange={selectFile} />
            {fileLabel && <span className="hint">{fileLabel}</span>}
          </div>
          <div className="field field-wide">
            <label>数据内容</label>
            <textarea
              rows={8}
              value={text}
              onChange={(event) => setText(event.target.value)}
              placeholder='每行一个 JSON 对象，例如 {"record_id":"r001","title":"标题","body":"正文"}'
            />
          </div>
        </div>
        <button className="btn btn-primary" disabled={busy} onClick={submit}>保存导入数据</button>
      </div>

      <div className="card section-card">
        <div className="toolbar">
          <h3>已导入数据（{items.length}）</h3>
          <button className="btn btn-sm" onClick={reload}>刷新</button>
        </div>
        {!items.length && <div className="empty">暂无导入数据</div>}
        {items.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>导入编号</th>
                  <th>行数</th>
                  <th>ID 唯一数</th>
                  <th>缺失/重复 ID</th>
                  <th>关联样本</th>
                  <th>内容哈希</th>
                  <th>保存路径</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <tr key={item.import_id} className={selectedId === item.import_id ? "row-selected" : ""}>
                    <td>{item.import_id}</td>
                    <td>{item.rows}</td>
                    <td>{item.unique_ids ?? "-"}</td>
                    <td>{item.missing_ids ?? "-"} / {item.duplicate_ids ?? "-"}</td>
                    <td>{(item.linked_samples || []).map((sample) => sample.sample_id).join(", ") || "-"}</td>
                    <td className="mono-cell">{shortHash(item.content_sha256)}</td>
                    <td className="muted path-cell">{item.path}</td>
                    <td>
                      <div className="action-row">
                        <button className="btn btn-sm" onClick={() => setSelectedId(item.import_id)}>查看</button>
                        <a className="btn btn-sm" href={api.importDownloadUrl(taskId, item.import_id)}>下载</a>
                        <button className="btn btn-sm btn-danger" disabled={busy || (item.linked_samples || []).length > 0} onClick={() => archive(item)}>归档</button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {selected && (
        <div className="card section-card">
          <div className="toolbar">
            <h3>导入详情：{selected.import_id}</h3>
            <div className="action-row">
              <input className="toolbar-input" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索当前导入数据" />
              <button className="btn btn-sm" onClick={searchRows}>搜索</button>
              <button className="btn btn-sm" onClick={() => { setQuery(""); loadRows(selected.import_id, { offset: 0, query: "" }); }}>清空</button>
            </div>
          </div>
          <div className="data-profile">
            <div><span>状态</span><strong>{stateLabel(detail?.state || selected.state || "active")}</strong></div>
            <div><span>行数</span><strong>{detail?.rows ?? selected.rows}</strong></div>
            <div><span>ID 字段</span><strong>{detail?.id_field || "-"}</strong></div>
            <div><span>ID 唯一数</span><strong>{detail?.unique_ids ?? "-"}</strong></div>
            <div><span>缺失 ID</span><strong>{detail?.missing_ids ?? "-"}</strong></div>
            <div><span>重复 ID</span><strong>{detail?.duplicate_ids ?? "-"}</strong></div>
            <div><span>字段数</span><strong>{(detail?.fields || selected.fields || []).length}</strong></div>
            <div><span>内容哈希</span><strong className="mono-cell">{shortHash(detail?.content_sha256 || selected.content_sha256)}</strong></div>
          </div>
          {detail?.declared_path && (
            <div className="status-line">历史 manifest 中记录的原路径：{detail.declared_path}；当前面板读取的是保存路径：{detail.path}</div>
          )}
          {(detail?.linked_samples || []).length > 0 && (
            <div className="status-line">关联样本：{detail.linked_samples.map((sample) => sample.sample_id).join(", ")}</div>
          )}
          <details className="secondary-panel">
            <summary>字段清单</summary>
            <div className="field-list">{(detail?.fields || selected.fields || []).map((field) => <span key={field}>{field}</span>)}</div>
          </details>

          <div className="toolbar data-toolbar">
            <span className="muted">匹配 {rowsData.total || 0} 行，当前显示第 {(rowsData.offset || 0) + 1} - {Math.min((rowsData.offset || 0) + (rowsData.rows || []).length, rowsData.total || 0)} 行</span>
            <div className="action-row">
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
                    {fields.map((field) => <td key={field} className="text-cell">{cellText(row[field])}</td>)}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
