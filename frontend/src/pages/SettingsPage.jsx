import React, { useEffect, useMemo, useState } from "react";
import * as api from "./../api.js";
import { Link } from "./../router.jsx";

const DEFAULT_SETTINGS = {
  task_registry_uri: "",
  data_lake_r2_prefix: "",
  task_source: "local",
  rclone_config_path: "",
  allow_data_lake_overrides: false,
};

function mergeSettings(value) {
  return { ...DEFAULT_SETTINGS, ...(value || {}) };
}

function sourceLabel(value) {
  if (value === "r2") return "R2 登记表";
  if (value === "local") return "本地任务目录";
  return value || "-";
}

function boolLabel(value) {
  return value ? "是" : "否";
}

export default function SettingsPage({ settings, onSettingsSaved, onError }) {
  const normalized = useMemo(() => mergeSettings(settings), [settings]);
  const [form, setForm] = useState({
    task_registry_uri: normalized.task_registry_uri,
    data_lake_r2_prefix: normalized.data_lake_r2_prefix,
  });
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");

  useEffect(() => {
    setForm({
      task_registry_uri: normalized.task_registry_uri,
      data_lake_r2_prefix: normalized.data_lake_r2_prefix,
    });
  }, [normalized.task_registry_uri, normalized.data_lake_r2_prefix]);

  function update(key, value) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  async function save() {
    setBusy(true);
    setNotice("");
    try {
      const payload = {
        task_registry_uri: form.task_registry_uri.trim(),
        data_lake_r2_prefix: form.data_lake_r2_prefix.trim(),
      };
      const saved = await api.updateSettings(payload);
      const next = mergeSettings({ ...normalized, ...payload, ...saved });
      await onSettingsSaved?.(next);
      setNotice("系统设置已保存。");
    } catch (error) {
      onError(String(error));
    } finally {
      setBusy(false);
    }
  }

  async function reload() {
    setBusy(true);
    setNotice("");
    try {
      const fresh = mergeSettings(await api.getSettings());
      await onSettingsSaved?.(fresh);
      setNotice("系统设置已刷新。");
    } catch (error) {
      onError(String(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <div className="crumbs">
        <Link to="/">全部任务</Link> / 系统设置
      </div>
      <div className="page-header">
        <h2>系统设置</h2>
        <p>配置任务登记表和数据湖导入使用的共享位置，保存后任务列表会按新配置刷新。</p>
      </div>

      {notice && <div className="status-banner">{notice}</div>}

      <div className="card section-card">
        <div className="toolbar">
          <h3>可编辑配置</h3>
          <button className="btn btn-sm" disabled={busy} onClick={reload}>刷新</button>
        </div>
        <div className="form-grid">
          <div className="field field-wide">
            <label>任务登记表地址 <span className="field-key">task_registry_uri</span></label>
            <input
              value={form.task_registry_uri}
              onChange={(event) => update("task_registry_uri", event.target.value)}
              placeholder="r2:bucket/path/task_registry.json"
            />
            <span className="hint">R2 模式下，任务列表会从这个登记表同步任务配置。</span>
          </div>
          <div className="field field-wide">
            <label>数据湖根路径 <span className="field-key">data_lake_r2_prefix</span></label>
            <input
              value={form.data_lake_r2_prefix}
              onChange={(event) => update("data_lake_r2_prefix", event.target.value)}
              placeholder="r2:bucket/path/..."
            />
            <span className="hint">作为数据湖导入的默认 R2 前缀；任务也可以在登记表中声明更具体的数据源。</span>
          </div>
        </div>
        <div className="action-row form-actions">
          <button className="btn btn-primary" disabled={busy} onClick={save}>保存设置</button>
        </div>
      </div>

      <div className="card section-card">
        <h3>当前运行状态</h3>
        <div className="form-grid readonly-grid">
          <div className="field">
            <label>任务来源 <span className="field-key">task_source</span></label>
            <input value={`${sourceLabel(normalized.task_source)} (${normalized.task_source || "-"})`} readOnly />
          </div>
          <div className="field">
            <label>允许任务覆盖数据湖来源 <span className="field-key">allow_data_lake_overrides</span></label>
            <input value={boolLabel(Boolean(normalized.allow_data_lake_overrides))} readOnly />
          </div>
          <div className="field field-wide">
            <label>Rclone 配置文件路径 <span className="field-key">rclone_config_path</span></label>
            <input value={normalized.rclone_config_path || "-"} readOnly />
          </div>
        </div>
      </div>
    </div>
  );
}
