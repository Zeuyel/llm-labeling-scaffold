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

export default function TasksPage({
  tasks,
  onReload,
  onError,
  allowDataLakeOverrides = false,
  taskSource = "local",
  taskRegistryUri = "",
}) {
  const { navigate } = useRouter();
  const r2TaskSource = taskSource === "r2";
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [archiving, setArchiving] = useState("");
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
      if (allowDataLakeOverrides) {
        payload.data_lake = {
          lake_registry_uri: form.lake_registry_uri.trim(),
          source_dataset_id: form.source_dataset_id.trim(),
          source_manifest_uri: form.source_manifest_uri.trim(),
          source_object_path: form.source_object_path.trim(),
          default_import_id: form.default_import_id.trim(),
          output_base_uri: form.output_base_uri.trim(),
        };
      }
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

  async function archiveTask(task) {
    if (!task?.task_id) return;
    if (!task.deletable) {
      onError(r2TaskSource ? "这个任务来自 R2 数据湖登记表，不能在面板中归档本地缓存；请在登记表中标记为非启用状态" : "这个任务来自只读目录，不能在面板中归档");
      return;
    }
    const ok = window.confirm(`归档任务 ${task.task_id}？\n\n归档会把任务配置移到 _archive，不会删除数据。若该任务已有导入、样本、标注或模型产物，系统会拒绝归档。`);
    if (!ok) return;
    setArchiving(task.task_id);
    try {
      await api.archiveTask(task.task_id);
      await onReload();
      navigate("/");
    } catch (error) {
      onError(String(error));
    } finally {
      setArchiving("");
    }
  }

  return (
    <div>
      <div className="page-header">
        <h2>全部任务</h2>
        <p>{r2TaskSource ? "任务配置来自 R2 数据湖登记表，本地只缓存执行配置" : "选择一个标注任务进入其数据流水线"}</p>
      </div>
      <div className="toolbar">
        <span className="muted">{tasks.length} 个任务{r2TaskSource && taskRegistryUri ? ` · ${taskRegistryUri}` : ""}</span>
        <div className="action-row">
          {!r2TaskSource && (
            <button className="btn btn-sm" onClick={() => setOpen((value) => !value)}>{open ? "收起" : "新建任务"}</button>
          )}
          <button className="btn btn-sm" onClick={onReload}>刷新</button>
        </div>
      </div>
      {open && !r2TaskSource && (
        <div className="card section-card">
          <h3>新建任务</h3>
          <div className="form-grid">
            <div className="field">
              <label>任务编号</label>
              <input value={form.task_id} onChange={(event) => update("task_id", event.target.value)} placeholder="例如 patent_boundary_v1" />
            </div>
            <div className="field">
              <label>ID 字段</label>
              <input value={form.id_field} onChange={(event) => update("id_field", event.target.value)} placeholder="例如 patent_id" />
            </div>
            <div className="field field-wide">
              <label>文本字段</label>
              <textarea rows={2} value={form.text_fields} onChange={(event) => update("text_fields", event.target.value)} placeholder="例如 patent_title, patent_abstract, patent_claim_excerpt" />
            </div>
            <div className="field field-wide">
              <label>元数据字段</label>
              <textarea rows={2} value={form.metadata_fields} onChange={(event) => update("metadata_fields", event.target.value)} placeholder="例如 firm_name, application_year, ipc_main" />
            </div>
            <div className="field">
              <label>主标签字段</label>
              <input value={form.primary_label_name} onChange={(event) => update("primary_label_name", event.target.value)} placeholder="例如 innovation_boundary_label" />
            </div>
            <div className="field">
              <label>主标签标题</label>
              <input value={form.primary_label_title} onChange={(event) => update("primary_label_title", event.target.value)} placeholder="例如 创新边界判断" />
            </div>
            <div className="field field-wide">
              <label>主标签取值</label>
              <textarea rows={3} value={form.primary_label_values} onChange={(event) => update("primary_label_values", event.target.value)} placeholder="每行一个取值，或用逗号分隔" />
            </div>
            <div className="field field-wide">
              <label>Argilla 标注说明</label>
              <textarea rows={5} value={form.annotation_guidelines} onChange={(event) => update("annotation_guidelines", event.target.value)} placeholder="写给人工标注员的说明，会显示在 Argilla 数据集页面" />
            </div>
            <div className="field field-wide">
              <label>提示词</label>
              <textarea rows={5} value={form.prompt} onChange={(event) => update("prompt", event.target.value)} placeholder="可留空，后续再补充" />
            </div>
            {allowDataLakeOverrides && (
              <>
                <div className="field field-wide">
                  <label>数据湖登记表 URI</label>
                  <input value={form.lake_registry_uri} onChange={(event) => update("lake_registry_uri", event.target.value)} placeholder="可留空，默认读取 R2 当前数据湖登记表" />
                </div>
                <div className="field">
                  <label>源数据集编号</label>
                  <input value={form.source_dataset_id} onChange={(event) => update("source_dataset_id", event.target.value)} placeholder="例如 patent_boundary_v0_1_label_inputs" />
                </div>
                <div className="field">
                  <label>默认导入编号</label>
                  <input value={form.default_import_id} onChange={(event) => update("default_import_id", event.target.value)} placeholder="例如 patent_boundary_manual_seed" />
                </div>
                <div className="field field-wide">
                  <label>源 manifest URI</label>
                  <input value={form.source_manifest_uri} onChange={(event) => update("source_manifest_uri", event.target.value)} placeholder="可留空，系统按源数据集编号从登记表解析" />
                </div>
                <div className="field field-wide">
                  <label>源对象路径</label>
                  <input value={form.source_object_path} onChange={(event) => update("source_object_path", event.target.value)} placeholder="manifest objects 中的 path，用于唯一选中 JSONL 对象" />
                </div>
                <div className="field field-wide">
                  <label>标签回写根 URI</label>
                  <input value={form.output_base_uri} onChange={(event) => update("output_base_uri", event.target.value)} placeholder="例如 r2:ai-innovation-data-lake/labels/patent/<task_id>/" />
                </div>
              </>
            )}
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
      {!tasks.length && <div className="empty">{r2TaskSource ? "数据湖登记表暂无启用任务" : "未发现任务（检查 --tasks-root 目录下的 task.yaml）"}</div>}
      <div className="grid grid-cards">
        {tasks.map((t) => (
          <div key={t.path} className="card task-card">
            <div className="task-card-head">
              <Link to={`/task/${encodeURIComponent(t.task_id)}`} className="task-title-link">
                <h3>{t.task_id || "(无效)"}</h3>
              </Link>
              {t.deletable ? (
                <button
                  className="btn btn-sm btn-danger"
                  disabled={archiving === t.task_id}
                  onClick={() => archiveTask(t)}
                >
                  归档
                </button>
              ) : (
                <span className="badge badge-gray">{r2TaskSource ? "数据湖" : "只读"}</span>
              )}
            </div>
            {t.error ? (
              <span className="badge badge-red">{t.error}</span>
            ) : (
              <div className="muted">
                <div>id 字段：{t.id_field}</div>
                <div>主标签：{t.primary_label ? t.primary_label.name : "-"}</div>
                <div>来源：{t.source || "-"}</div>
              </div>
            )}
            <div className="action-row task-card-actions">
              <button className="btn btn-sm" onClick={() => navigate(`/task/${encodeURIComponent(t.task_id)}`)}>进入</button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
