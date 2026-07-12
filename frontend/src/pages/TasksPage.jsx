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

const PROFILE_OPTIONS = [
  ["manual_labeling_cv_v1", "人工标注闭环 v1"],
  ["manual_labeling_quality_control_v1", "人工标注质量控制 v1"],
];

const emptyForm = () => ({
  task_id: "",
  profile: "manual_labeling_cv_v1",
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

function listText(value) {
  return Array.isArray(value) ? value.join("\n") : String(value || "");
}

function formFromSpec(spec = {}) {
  const dataLake = spec.data_lake && typeof spec.data_lake === "object" ? spec.data_lake : {};
  return {
    ...emptyForm(),
    task_id: String(spec.task_id || ""),
    profile: String(spec.profile || "manual_labeling_cv_v1"),
    id_field: String(spec.id_field || "record_id"),
    text_fields: listText(spec.text_fields),
    metadata_fields: listText(spec.metadata_fields),
    primary_label_name: String(spec.primary_label_name || "label"),
    primary_label_title: String(spec.primary_label_title || ""),
    primary_label_values: listText(spec.primary_label_values),
    annotation_guidelines: String(spec.annotation_guidelines || ""),
    prompt: String(spec.prompt || ""),
    lake_registry_uri: String(dataLake.lake_registry_uri || ""),
    source_dataset_id: String(dataLake.source_dataset_id || ""),
    source_manifest_uri: String(dataLake.source_manifest_uri || ""),
    source_object_path: String(dataLake.source_object_path || ""),
    default_import_id: String(dataLake.default_import_id || ""),
    output_base_uri: String(dataLake.output_base_uri || ""),
  };
}

function auxiliaryFromSpec(spec = {}) {
  return Array.isArray(spec.auxiliary_labels)
    ? spec.auxiliary_labels.map((item) => ({
      ...emptyAuxiliary(),
      ...item,
      name: String(item.name || ""),
      title: String(item.title || ""),
      values: listText(item.values),
      min: item.min ?? "",
      max: item.max ?? "",
      required: item.required !== false,
    }))
    : [];
}

function taskStatusLabel(status) {
  if (status === "draft") return "草稿";
  if (status === "published_with_draft") return "有待发布草稿";
  if (status === "published") return "已发布";
  return status || "未知状态";
}

export default function TasksPage({
  tasks,
  onReload,
  onSync,
  onError,
  allowDataLakeOverrides = false,
  taskSource = "local",
  taskRegistryUri = "",
}) {
  const { navigate } = useRouter();
  const r2TaskSource = taskSource === "r2";
  const controlTaskSource = taskSource === "control";
  const showDataLakeFields = controlTaskSource || allowDataLakeOverrides;
  const [open, setOpen] = useState(false);
  const [editingTaskId, setEditingTaskId] = useState("");
  const [busy, setBusy] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [notice, setNotice] = useState("");
  const [form, setForm] = useState(emptyForm);
  const [auxiliary, setAuxiliary] = useState([]);

  function update(key, value) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  function updateAuxiliary(index, key, value) {
    setAuxiliary((current) => current.map((item, i) => (i === index ? { ...item, [key]: value } : item)));
  }

  function resetForm() {
    setForm(emptyForm());
    setAuxiliary([]);
    setEditingTaskId("");
  }

  function openNewTask() {
    resetForm();
    setNotice("");
    setOpen(true);
  }

  function closeEditor() {
    resetForm();
    setOpen(false);
  }

  function payloadFromForm() {
    const payload = {
      task_id: form.task_id.trim(),
      profile: form.profile,
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
    if (showDataLakeFields) {
      payload.data_lake = {
        lake_registry_uri: form.lake_registry_uri.trim(),
        source_dataset_id: form.source_dataset_id.trim(),
        source_manifest_uri: form.source_manifest_uri.trim(),
        source_object_path: form.source_object_path.trim(),
        default_import_id: form.default_import_id.trim(),
        output_base_uri: form.output_base_uri.trim(),
      };
    }
    return payload;
  }

  function validateForm(payload, publishNow) {
    if (!payload.task_id) return "请填写任务编号";
    if (!payload.text_fields.length) return "请填写文本字段";
    if (payload.primary_label_values.length < 2) return "主标签至少需要两个取值";
    if (controlTaskSource && publishNow) {
      const required = ["source_dataset_id", "source_object_path", "default_import_id", "output_base_uri"];
      const missing = required.filter((field) => !payload.data_lake?.[field]);
      if (missing.length) return `发布前请补全数据湖字段：${missing.join("、")}`;
    }
    return "";
  }

  async function submit({ publishNow = false } = {}) {
    const payload = payloadFromForm();
    const error = validateForm(payload, publishNow);
    if (error) {
      onError(error);
      return;
    }
    setBusy(true);
    setNotice("");
    try {
      if (!controlTaskSource) {
        await api.createTask(payload);
        closeEditor();
        setNotice("任务已保存。");
      } else {
        const taskId = editingTaskId || payload.task_id;
        if (editingTaskId) {
          await api.updateTask(taskId, payload);
        } else {
          await api.createTask(payload);
          setEditingTaskId(taskId);
        }
        if (publishNow) {
          await api.publishTask(taskId);
          closeEditor();
          setNotice("任务单已发布为新 revision，可进入执行流程。");
        } else {
          setNotice("草稿已保存。发布后才会成为可执行任务。");
        }
      }
      await onReload();
    } catch (requestError) {
      onError(String(requestError));
    } finally {
      setBusy(false);
    }
  }

  async function openEditor(task) {
    if (!controlTaskSource || !task?.task_id) return;
    setBusy(true);
    setNotice("");
    try {
      const record = await api.getTaskControl(task.task_id);
      setForm(formFromSpec(record.draft_spec));
      setAuxiliary(auxiliaryFromSpec(record.draft_spec));
      setEditingTaskId(task.task_id);
      setOpen(true);
    } catch (requestError) {
      onError(String(requestError));
    } finally {
      setBusy(false);
    }
  }

  async function publishExistingTask(task) {
    if (!task?.task_id) return;
    setBusy(true);
    setNotice("");
    try {
      await api.publishTask(task.task_id);
      await onReload();
      setNotice(`任务单 ${task.task_id} 已发布为新 revision。`);
    } catch (requestError) {
      onError(String(requestError));
    } finally {
      setBusy(false);
    }
  }

  async function reloadTasks() {
    setSyncing(true);
    try {
      if (r2TaskSource) {
        await (onSync || onReload)();
      } else {
        await onReload();
      }
    } finally {
      setSyncing(false);
    }
  }

  function openArchiveWizard(task) {
    if (!task?.task_id) return;
    navigate(`/task/${encodeURIComponent(task.task_id)}/archive`);
  }

  const description = r2TaskSource
    ? "任务配置来自 R2 登记表，本地只缓存执行配置。"
    : controlTaskSource
      ? "任务单由 Scaffold 控制面创建、编辑和发布；R2 只保存数据湖来源与输出。"
      : "选择一个标注任务进入其数据流水线。";

  return (
    <div>
      <div className="page-header">
        <h2>全部任务</h2>
        <p>{description}</p>
      </div>
      {notice && <div className="status-banner">{notice}</div>}
      <div className="toolbar">
        <div className="toolbar-stack">
          <span className="muted">{tasks.length} 个任务{r2TaskSource && taskRegistryUri ? ` · ${taskRegistryUri}` : ""}</span>
          {r2TaskSource && <span className="status-line">从 registry/data_lake.yaml 同步 task_id 到 task_uri，再读取任务配置。</span>}
          {controlTaskSource && <span className="status-line">草稿可以反复修改；每次发布都会保存独立 revision 快照。</span>}
        </div>
        <div className="action-row">
          {!r2TaskSource && (
            <button className="btn btn-sm" disabled={busy} onClick={openNewTask}>新建任务单</button>
          )}
          <button className={r2TaskSource ? "btn btn-sm btn-primary" : "btn btn-sm"} disabled={syncing || busy} onClick={reloadTasks}>
            {r2TaskSource ? (syncing ? "同步中..." : "同步任务配置") : "刷新"}
          </button>
        </div>
      </div>
      {open && !r2TaskSource && (
        <div className="card section-card">
          <div className="toolbar">
            <div>
              <h3>{editingTaskId ? "编辑任务单" : "新建任务单"}</h3>
              {controlTaskSource && <span className="hint">保存只更新草稿；发布后才写入当前可执行任务配置。</span>}
            </div>
            <button className="btn btn-sm" disabled={busy} onClick={closeEditor}>关闭</button>
          </div>
          <div className="form-grid">
            <div className="field">
              <label>任务编号</label>
              <input value={form.task_id} disabled={Boolean(editingTaskId)} onChange={(event) => update("task_id", event.target.value)} placeholder="例如 feedback_labeling_v1" />
            </div>
            <div className="field">
              <label>流程预设</label>
              <select value={form.profile} onChange={(event) => update("profile", event.target.value)}>
                {PROFILE_OPTIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
              </select>
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
            {showDataLakeFields && (
              <>
                <div className="field field-wide">
                  <label>数据湖登记表地址</label>
                  <input value={form.lake_registry_uri} onChange={(event) => update("lake_registry_uri", event.target.value)} placeholder="可留空，使用系统设置中的默认 R2 登记表" />
                </div>
                <div className="field">
                  <label>源数据集编号</label>
                  <input value={form.source_dataset_id} onChange={(event) => update("source_dataset_id", event.target.value)} placeholder="例如 raw_feedback_records" />
                </div>
                <div className="field">
                  <label>默认导入编号</label>
                  <input value={form.default_import_id} onChange={(event) => update("default_import_id", event.target.value)} placeholder="例如 manual_seed_20260627" />
                </div>
                <div className="field field-wide">
                  <label>源清单文件地址</label>
                  <input value={form.source_manifest_uri} onChange={(event) => update("source_manifest_uri", event.target.value)} placeholder="可留空，按源数据集编号从数据湖登记表解析" />
                </div>
                <div className="field field-wide">
                  <label>源对象路径</label>
                  <input value={form.source_object_path} onChange={(event) => update("source_object_path", event.target.value)} placeholder="清单对象中的相对路径，用于唯一选中数据文件" />
                </div>
                <div className="field field-wide">
                  <label>标签回写根地址</label>
                  <input value={form.output_base_uri} onChange={(event) => update("output_base_uri", event.target.value)} placeholder="例如 r2:bucket/path/labels/<task_id>/" />
                </div>
              </>
            )}
          </div>

          <div className="toolbar">
            <h3>辅助字段</h3>
            <button className="btn btn-sm" disabled={busy} onClick={() => setAuxiliary((current) => [...current, emptyAuxiliary()])}>添加字段</button>
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
                      <td><button className="btn btn-sm" disabled={busy} onClick={() => setAuxiliary((current) => current.filter((_, i) => i !== index))}>删除</button></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <div className="action-row form-actions">
            {controlTaskSource ? (
              <>
                <button className="btn" disabled={busy} onClick={() => submit()}>保存草稿</button>
                <button className="btn btn-primary" disabled={busy} onClick={() => submit({ publishNow: true })}>保存并发布</button>
              </>
            ) : (
              <button className="btn btn-primary" disabled={busy} onClick={() => submit()}>保存任务</button>
            )}
            <button className="btn" disabled={busy} onClick={resetForm}>清空</button>
          </div>
        </div>
      )}
      {!tasks.length && <div className="empty">{r2TaskSource ? "R2 登记表暂无启用任务" : controlTaskSource ? "尚未创建任务单" : "未发现任务，可新建任务或检查任务目录"}</div>}
      <div className="grid grid-cards">
        {tasks.map((task) => {
          const canEnter = !controlTaskSource || (Number(task.revision || 0) > 0 && Boolean(task.path));
          const hasDraftToPublish = controlTaskSource && ["draft", "published_with_draft"].includes(task.status);
          return (
            <div key={task.task_id || task.path} className="card task-card">
              <div className="task-card-head">
                {canEnter ? (
                  <Link to={`/task/${encodeURIComponent(task.task_id)}`} className="task-title-link"><h3>{task.task_id || "(无效)"}</h3></Link>
                ) : <h3>{task.task_id || "(无效)"}</h3>}
                {!controlTaskSource && <button className="btn btn-sm" onClick={() => openArchiveWizard(task)}>归档向导</button>}
              </div>
              {task.error ? (
                <span className="badge badge-red">{task.error}</span>
              ) : (
                <div className="muted">
                  <div>记录编号字段：{task.id_field}</div>
                  <div>主标签：{task.primary_label ? task.primary_label.name : "-"}</div>
                  <div>来源：{task.source || "-"}</div>
                  {controlTaskSource && <div>状态：{taskStatusLabel(task.status)}{task.revision ? ` · revision ${task.revision}` : ""}</div>}
                </div>
              )}
              <div className="action-row task-card-actions">
                {canEnter && <button className="btn btn-sm" onClick={() => navigate(`/task/${encodeURIComponent(task.task_id)}`)}>进入</button>}
                {controlTaskSource && <button className="btn btn-sm" disabled={busy} onClick={() => openEditor(task)}>编辑任务单</button>}
                {hasDraftToPublish && <button className="btn btn-sm btn-primary" disabled={busy} onClick={() => publishExistingTask(task)}>发布草稿</button>}
                {controlTaskSource && !canEnter && <span className="badge badge-gray">发布后可执行</span>}
                {!controlTaskSource && !task.deletable && <span className="badge badge-gray">{r2TaskSource ? "数据湖" : "只读"}</span>}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
