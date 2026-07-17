import React, { useState } from "react";
import * as api from "./../api.js";
import { Link, useRouter } from "./../router.jsx";

const LABEL_TYPES = [
  ["categorical", "分类"],
  ["integer", "整数"],
  ["number", "数值"],
  ["boolean", "布尔"],
  ["string", "文本"],
];

const emptyAuxiliary = () => ({
  name: "",
  title: "",
  type: "string",
  values: "",
  min: "",
  max: "",
  required: true,
});

function parseList(value) {
  return String(value || "")
    .split(/[\n,]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

async function readDataLakeCatalog(datasetId = "") {
  const query = datasetId ? `?dataset_id=${encodeURIComponent(datasetId)}` : "";
  const response = await fetch(`/api/data_lake/catalog${query}`);
  if (!response.ok) throw new Error(`读取数据湖资产目录失败：${response.status}`);
  return response.json();
}

function generatedImportId(taskId, datasetId, objectPath) {
  const suffix = String(objectPath || "v001").split("/").filter(Boolean).pop() || "v001";
  return `${taskId || "task"}_${datasetId || "dataset"}_${suffix.replace(/\.[^.]+$/, "")}`
    .replace(/[^A-Za-z0-9_.-]+/g, "_");
}

export default function TasksPage({
  tasks,
  onReload,
  onError,
}) {
  const { navigate } = useRouter();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [catalog, setCatalog] = useState(null);
  const [catalogBusy, setCatalogBusy] = useState(false);
  const [selectedAsset, setSelectedAsset] = useState(null);
  const [form, setForm] = useState({
    task_id: "",
    id_field: "record_id",
    text_fields: "",
    metadata_fields: "",
    primary_label_name: "label",
    primary_label_title: "",
    primary_label_values: "",
    annotation_guidelines: "",
    prompt: "",
    lake_registry_uri: "",
    source_dataset_id: "",
    source_manifest_uri: "",
    source_object_path: "",
    default_import_id: "",
    output_base_uri: "",
  });
  const [auxiliary, setAuxiliary] = useState([]);

  function update(key, value) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  function updateAuxiliary(index, key, value) {
    setAuxiliary((current) => current.map((item, i) => (i === index ? { ...item, [key]: value } : item)));
  }

  function resetForm() {
    setForm({
      task_id: "",
      id_field: "record_id",
      text_fields: "",
      metadata_fields: "",
      primary_label_name: "label",
      primary_label_title: "",
      primary_label_values: "",
      annotation_guidelines: "",
      prompt: "",
      lake_registry_uri: "",
      source_dataset_id: "",
      source_manifest_uri: "",
      source_object_path: "",
      default_import_id: "",
      output_base_uri: "",
    });
    setAuxiliary([]);
    setSelectedAsset(null);
  }

  async function submit() {
    if (!form.task_id.trim()) { onError("请填写任务编号"); return; }
    if (!parseList(form.text_fields).length) { onError("请填写文本字段"); return; }
    if (parseList(form.primary_label_values).length < 2) { onError("主标签至少需要两个取值"); return; }
    setBusy(true);
    try {
      const payload = {
        task_id: form.task_id.trim(),
        id_field: form.id_field.trim() || "record_id",
        text_fields: parseList(form.text_fields),
        metadata_fields: parseList(form.metadata_fields),
        primary_label_name: form.primary_label_name.trim() || "label",
        primary_label_title: form.primary_label_title.trim(),
        primary_label_values: parseList(form.primary_label_values),
        annotation_guidelines: form.annotation_guidelines.trim(),
        prompt: form.prompt,
        auxiliary_labels: auxiliary
          .filter((item) => item.name.trim())
          .map((item) => ({
            name: item.name.trim(),
            title: item.title.trim(),
            type: item.type,
            values: parseList(item.values),
            min: item.min,
            max: item.max,
            required: item.required,
          })),
      };
      payload.data_lake = {
        lake_registry_uri: form.lake_registry_uri.trim(),
        source_dataset_id: form.source_dataset_id.trim(),
        source_manifest_uri: form.source_manifest_uri.trim(),
        source_object_path: form.source_object_path.trim(),
        default_import_id: form.default_import_id.trim(),
        output_base_uri: form.output_base_uri.trim(),
      };
      await api.createTask(payload);
      resetForm();
      setOpen(false);
      await onReload();
    } catch (error) {
      onError(String(error));
    } finally {
      setBusy(false);
    }
  }

  async function loadCatalog() {
    setCatalogBusy(true);
    try {
      const next = await readDataLakeCatalog();
      setCatalog(next);
      setForm((current) => ({
        ...current,
        lake_registry_uri: current.lake_registry_uri || next.registry_uri || "",
      }));
    } catch (error) {
      onError(String(error));
    } finally {
      setCatalogBusy(false);
    }
  }

  async function chooseDataset(datasetId) {
    const item = (catalog?.datasets || []).find((dataset) => dataset.dataset_id === datasetId);
    if (!item) {
      setSelectedAsset(null);
      setForm((current) => ({
        ...current,
        source_dataset_id: "",
        source_manifest_uri: "",
        source_object_path: "",
      }));
      return;
    }

    setSelectedAsset(item);
    try {
      const response = await readDataLakeCatalog(item.dataset_id);
      const detail = (response.datasets || []).find((dataset) => dataset.dataset_id === item.dataset_id) || item;
      const objects = detail.manifest?.objects || [];
      setSelectedAsset(detail);
      setForm((current) => ({
        ...current,
        lake_registry_uri: current.lake_registry_uri || catalog.registry_uri || "",
        source_dataset_id: detail.dataset_id,
        source_manifest_uri: detail.manifest_uri || "",
        source_object_path: objects.length === 1 ? objects[0].path || "" : current.source_object_path,
        default_import_id: current.default_import_id || generatedImportId(current.task_id, detail.dataset_id, objects[0]?.path),
      }));
    } catch (error) {
      onError(String(error));
    }
  }

  async function reloadTasks() {
    setRefreshing(true);
    try {
      await onReload();
    } finally {
      setRefreshing(false);
    }
  }

  function openArchiveWizard(task) {
    if (!task?.task_id) return;
    navigate(`/task/${encodeURIComponent(task.task_id)}/archive`);
  }

  return (
    <div>
      <div className="page-header">
        <h2>全部任务</h2>
        <p>任务单由 Scaffold 控制面管理；R2 仅提供数据湖资产与任务产物。</p>
      </div>
      <div className="toolbar">
        <div className="toolbar-stack">
          <span className="muted">{tasks.length} 个任务 · 控制面</span>
        </div>
        <div className="action-row">
          <button className="btn btn-sm" disabled={busy} onClick={() => setOpen((value) => !value)}>{open ? "收起" : "新建任务单"}</button>
          <button className="btn btn-sm" disabled={refreshing} onClick={reloadTasks}>{refreshing ? "刷新中..." : "刷新任务"}</button>
        </div>
      </div>
      {open && (
        <div className="card section-card">
          <h3>新建任务单</h3>
          <div className="form-grid">
            <div className="field">
              <label>任务编号</label>
              <input value={form.task_id} onChange={(event) => update("task_id", event.target.value)} placeholder="例如 feedback_labeling_v1" />
            </div>
            <div className="field">
              <label>记录编号字段</label>
              <input value={form.id_field} onChange={(event) => update("id_field", event.target.value)} placeholder="例如 record_id" />
            </div>
            <div className="field field-wide">
              <label>文本字段</label>
              <textarea rows={2} value={form.text_fields} onChange={(event) => update("text_fields", event.target.value)} placeholder="例如 title, body, summary" />
            </div>
            <div className="field field-wide">
              <label>元数据字段</label>
              <textarea rows={2} value={form.metadata_fields} onChange={(event) => update("metadata_fields", event.target.value)} placeholder="例如 source, created_at, language" />
            </div>
            <div className="field">
              <label>主标签字段</label>
              <input value={form.primary_label_name} onChange={(event) => update("primary_label_name", event.target.value)} placeholder="例如 category" />
            </div>
            <div className="field">
              <label>主标签标题</label>
              <input value={form.primary_label_title} onChange={(event) => update("primary_label_title", event.target.value)} placeholder="例如 内容分类" />
            </div>
            <div className="field field-wide">
              <label>主标签取值</label>
              <textarea rows={3} value={form.primary_label_values} onChange={(event) => update("primary_label_values", event.target.value)} placeholder="每行一个取值，或用逗号分隔" />
            </div>
            <div className="field field-wide">
              <label>人工标注说明</label>
              <textarea rows={5} value={form.annotation_guidelines} onChange={(event) => update("annotation_guidelines", event.target.value)} placeholder="写给人工标注员的说明，会显示在标注数据集页面" />
            </div>
            <div className="field field-wide">
              <label>提示词</label>
              <textarea rows={5} value={form.prompt} onChange={(event) => update("prompt", event.target.value)} placeholder="可留空，后续再补充" />
            </div>
            <div className="field field-wide">
              <label>数据湖资产目录</label>
              <div className="action-row">
                <select value={form.source_dataset_id} disabled={!catalog} onChange={(event) => chooseDataset(event.target.value)}>
                  <option value="">{catalog ? "请选择已登记数据集" : "请先读取数据湖资产目录"}</option>
                  {(catalog?.datasets || []).map((item) => <option key={item.dataset_id} value={item.dataset_id}>{item.name || item.dataset_id} · {item.dataset_id}</option>)}
                </select>
                <button className="btn btn-sm" type="button" disabled={catalogBusy} onClick={loadCatalog}>{catalogBusy ? "读取中..." : "读取资产目录"}</button>
              </div>
              <span className="hint">从已登记的数据湖资产选择任务输入来源；任务单保存数据集和对象引用，不保存原始数据。</span>
              {selectedAsset && <span className="hint">已选择：{selectedAsset.name || selectedAsset.dataset_id} · {selectedAsset.layer || "未标注层级"} · {selectedAsset.domain || "未标注领域"}</span>}
            </div>
            <div className="field">
              <label>源数据集编号</label>
              <input value={form.source_dataset_id} onChange={(event) => update("source_dataset_id", event.target.value)} placeholder="例如 raw_feedback_records" />
            </div>
            <div className="field">
              <label>默认输入编号</label>
              <input value={form.default_import_id} onChange={(event) => update("default_import_id", event.target.value)} placeholder="选择资产后自动生成，可调整" />
            </div>
            <div className="field field-wide">
              <label>资产清单地址</label>
              <input value={form.source_manifest_uri} onChange={(event) => update("source_manifest_uri", event.target.value)} placeholder="选择资产后由数据湖目录填充" />
            </div>
            <div className="field field-wide">
              <label>源对象路径</label>
              <input value={form.source_object_path} onChange={(event) => update("source_object_path", event.target.value)} placeholder="清单对象中的路径，用于唯一选中数据文件" />
            </div>
            <div className="field field-wide">
              <label>产物写回根地址</label>
              <input value={form.output_base_uri} onChange={(event) => update("output_base_uri", event.target.value)} placeholder="例如 r2:bucket/path/labels/<task_id>/" />
            </div>
            <div className="field field-wide">
              <label>资产目录来源</label>
              <input value={form.lake_registry_uri || "由数据湖资产目录提供"} readOnly />
            </div>
          </div>

          <div className="toolbar">
            <h3>辅助字段</h3>
            <button className="btn btn-sm" onClick={() => setAuxiliary((current) => [...current, emptyAuxiliary()])}>添加字段</button>
          </div>
          {auxiliary.length > 0 && (
            <div className="table-wrap auxiliary-table">
              <table>
                <thead>
                  <tr><th>字段名</th><th>标题</th><th>类型</th><th>取值</th><th>范围</th><th>必填</th><th>操作</th></tr>
                </thead>
                <tbody>
                  {auxiliary.map((item, index) => (
                    <tr key={index}>
                      <td><input value={item.name} onChange={(event) => updateAuxiliary(index, "name", event.target.value)} placeholder="例如 confidence" /></td>
                      <td><input value={item.title} onChange={(event) => updateAuxiliary(index, "title", event.target.value)} placeholder="中文标题" /></td>
                      <td>
                        <select value={item.type} onChange={(event) => updateAuxiliary(index, "type", event.target.value)}>
                          {LABEL_TYPES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                        </select>
                      </td>
                      <td><input value={item.values} onChange={(event) => updateAuxiliary(index, "values", event.target.value)} placeholder="可选值" /></td>
                      <td>
                        <div className="range-inputs">
                          <input value={item.min} onChange={(event) => updateAuxiliary(index, "min", event.target.value)} placeholder="最小" />
                          <input value={item.max} onChange={(event) => updateAuxiliary(index, "max", event.target.value)} placeholder="最大" />
                        </div>
                      </td>
                      <td>
                        <label className="checkbox-inline">
                          <input type="checkbox" checked={item.required} onChange={(event) => updateAuxiliary(index, "required", event.target.checked)} />
                          是
                        </label>
                      </td>
                      <td><button className="btn btn-sm" onClick={() => setAuxiliary((current) => current.filter((_, i) => i !== index))}>删除</button></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <div className="action-row form-actions">
            <button className="btn btn-primary" disabled={busy} onClick={submit}>保存任务</button>
            <button className="btn" disabled={busy} onClick={resetForm}>清空</button>
          </div>
        </div>
      )}
      {!tasks.length && <div className="empty">尚未创建任务单，可新建任务单或检查控制面连接。</div>}
      <div className="grid grid-cards">
        {tasks.map((t) => (
          <div key={t.task_id || t.path} className="card task-card">
            <div className="task-card-head">
              <Link to={`/task/${encodeURIComponent(t.task_id)}`} className="task-title-link">
                <h3>{t.task_id || "(无效)"}</h3>
              </Link>
              <button className="btn btn-sm" onClick={() => openArchiveWizard(t)}>归档向导</button>
            </div>
            {t.error ? (
              <span className="badge badge-red">{t.error}</span>
            ) : (
              <div className="muted">
                <div>记录编号字段：{t.id_field}</div>
                <div>主标签：{t.primary_label ? t.primary_label.name : "-"}</div>
                <div>来源：{t.source || "-"}</div>
              </div>
            )}
            <div className="action-row task-card-actions">
              <button className="btn btn-sm" onClick={() => navigate(`/task/${encodeURIComponent(t.task_id)}`)}>进入</button>
              {!t.deletable && <span className="badge badge-gray">只读</span>}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
