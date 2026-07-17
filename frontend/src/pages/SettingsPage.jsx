import React, { useEffect, useMemo, useState } from "react";
import * as api from "./../api.js";
import { Link } from "./../router.jsx";

const DEFAULT_SETTINGS = {
  data_lake_r2_prefix: "",
  task_source: "control",
  rclone_config_path: "",
  allow_data_lake_overrides: false,
};

function mergeSettings(value) {
  return { ...DEFAULT_SETTINGS, ...(value || {}) };
}

function sourceLabel() {
  return "Scaffold 控制面";
}

function boolLabel(value) {
  return value ? "是" : "否";
}

export default function SettingsPage({ settings, onSettingsSaved, onSettingsLoadError, onError }) {
  const normalized = useMemo(() => mergeSettings(settings), [settings]);
  const [form, setForm] = useState({
    data_lake_r2_prefix: normalized.data_lake_r2_prefix,
  });
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");

  useEffect(() => {
    setForm({
      data_lake_r2_prefix: normalized.data_lake_r2_prefix,
    });
  }, [normalized.data_lake_r2_prefix]);

  function update(key, value) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  async function save() {
    setBusy(true);
    setNotice("");
    try {
      const payload = {
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
      if (onSettingsLoadError) {
        onSettingsLoadError(error);
      } else {
        onError(`设置读取失败：${String(error)}`);
      }
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
        <p>任务单由 Scaffold 控制面管理；这里配置数据湖资产和产物的默认存储位置。</p>
      </div>

      {notice && <div className="status-banner">{notice}</div>}

      <div className="card section-card">
        <div className="toolbar">
          <h3>可编辑配置</h3>
          <button className="btn btn-sm" disabled={busy} onClick={reload}>刷新</button>
        </div>
        <div className="form-grid">
          <div className="field field-wide">
            <label>数据湖资产与产物根路径 <span className="field-key">data_lake_r2_prefix</span></label>
            <input
              value={form.data_lake_r2_prefix}
              onChange={(event) => update("data_lake_r2_prefix", event.target.value)}
              placeholder="r2:bucket/path/..."
            />
            <span className="hint">用于读取数据湖资产清单、生成任务输入，并保存任务产物；任务单本身不写入 R2。</span>
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
            <label>任务单来源</label>
            <input value={sourceLabel()} readOnly />
          </div>
          <div className="field">
            <label>允许任务单指定数据湖来源 <span className="field-key">allow_data_lake_overrides</span></label>
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
