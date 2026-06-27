import React, { useEffect, useState, useCallback } from "react";
import * as api from "./../api.js";
import { Link } from "./../router.jsx";

const STATUS_LABEL = {
  not_started: "未开始",
  ready: "可执行",
  completed: "已完成",
  blocked: "受阻",
};

const STATUS_BADGE = {
  not_started: "badge-gray",
  ready: "badge-blue",
  completed: "badge-green",
  blocked: "badge-red",
};

function normalizeStageStatus(value) {
  const status = String(value || "not_started").toLowerCase();
  if (["completed", "complete", "done", "succeeded", "success", "finished"].includes(status)) return "completed";
  if (["ready", "available", "runnable", "actionable", "active", "running", "in_progress", "next"].includes(status)) return "ready";
  if (["blocked", "blocking", "waiting", "failed", "error"].includes(status)) return "blocked";
  return "not_started";
}

function compactJson(value) {
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function itemLabel(value) {
  if (value === null || value === undefined || value === "") return "";
  if (typeof value !== "object") return String(value);
  return String(value.title || value.name || value.id || value.key || value.path || compactJson(value));
}

function detailText(value, fallback) {
  if (Array.isArray(value)) {
    const labels = value.map(itemLabel).filter(Boolean);
    return labels.length ? labels.join("、") : fallback;
  }
  if (value && typeof value === "object") {
    const labels = Object.entries(value)
      .filter(([, item]) => item !== null && item !== undefined && item !== "")
      .map(([key, item]) => `${key}: ${itemLabel(item)}`)
      .filter(Boolean);
    return labels.length ? labels.join("；") : fallback;
  }
  const label = itemLabel(value);
  return label || fallback;
}

function profileName(data) {
  const profile = data?.profile;
  if (typeof profile === "string") return profile;
  if (profile && typeof profile === "object") {
    return profile.title || profile.name || profile.id || "未命名预设";
  }
  return data?.profile_name || data?.profile_id || "未命名预设";
}

function profileStages(data) {
  if (Array.isArray(data?.stages)) return data.stages;
  if (Array.isArray(data?.profile?.stages)) return data.profile.stages;
  return [];
}

function stageBlockReason(stage, status) {
  const reason = stage.blocked_reason || stage.blocking_reason || stage.block_reason || stage.status_reason || stage.reason;
  if (reason) return detailText(reason, "后端未提供阻塞原因");
  return status === "blocked" ? "后端未提供阻塞原因" : "无";
}

function stageActionHint(stage, status, index, total) {
  if (stage.action_hint) return detailText(stage.action_hint, "暂无下一步提示");
  if (status === "blocked") return "先处理阻塞项";
  if (status === "completed") return index === total - 1 ? "流程已完成" : "确认产物并进入下一阶段";
  if (status === "ready") return "可从对应功能入口执行";
  return "等待前置阶段完成";
}

function stageRoute(taskId, stage) {
  const action = String(stage.action || stage.id || "");
  const target = {
    import: "imports",
    lake_import: "imports",
    sample: "samples",
    argilla_push: "annotations",
    argilla_dispatch: "annotations",
    argilla_pull: "annotations",
    audit: "annotations",
    agreement_audit: "annotations",
    gold: "gold",
    gold_build: "gold",
    train: "models",
    infer: "models",
    batch_infer: "models",
  }[action];
  if (!target) return `/task/${encodeURIComponent(taskId)}`;
  return `/task/${encodeURIComponent(taskId)}/${target}`;
}

function stageRouteLabel(stage, status) {
  const action = String(stage.action || stage.id || "");
  const verb = status === "completed" ? "查看" : "进入";
  if (["import", "lake_import"].includes(action)) return `${verb}导入`;
  if (action === "sample") return `${verb}样本`;
  if (["argilla_push", "argilla_dispatch", "argilla_pull", "audit", "agreement_audit"].includes(action)) return `${verb}标注`;
  if (["gold", "gold_build"].includes(action)) return `${verb}训练集`;
  if (["train", "infer", "batch_infer"].includes(action)) return `${verb}模型`;
  return `${verb}处理`;
}

export default function TaskOverviewPage({ task, taskId, onError }) {
  const [counts, setCounts] = useState({ imports: 0, samples: 0, decisions: 0, gold: 0, models: 0, jobs: 0 });
  const [profile, setProfile] = useState({ loading: false, data: null, error: "" });

  const reload = useCallback(async () => {
    if (!taskId) return;
    try {
      const [i, s, d, g, m, j] = await Promise.all([
        api.getImports(taskId),
        api.getTaskSamples(taskId),
        api.getDecisionArtifacts(taskId),
        api.getTaskGoldVersions(taskId),
        api.getTaskModels(taskId),
        api.getJobs(taskId),
      ]);
      setCounts({
        imports: (i.imports || []).length,
        samples: (s.samples || []).length,
        decisions: (d.decision_artifacts || []).length,
        gold: (g.gold_versions || []).length,
        models: (m.models || []).length,
        jobs: (j.jobs || []).length,
      });
    } catch (e) {
      onError(String(e));
    }
  }, [taskId, onError]);

  const loadProfile = useCallback(async () => {
    if (!taskId) return;
    setProfile({ loading: true, data: null, error: "" });
    try {
      const data = await api.getTaskProfile(taskId);
      setProfile({ loading: false, data: data || {}, error: "" });
    } catch {
      setProfile({ loading: false, data: null, error: "流程预设暂不可用，仍可使用下方入口推进任务。" });
    }
  }, [taskId]);

  useEffect(() => { reload(); }, [reload]);
  useEffect(() => { loadProfile(); }, [loadProfile]);

  const cards = [
    { key: "imports", label: "导入数据", val: counts.imports, to: `/task/${encodeURIComponent(taskId)}/imports` },
    { key: "samples", label: "样本", val: counts.samples, to: `/task/${encodeURIComponent(taskId)}/samples` },
    { key: "decisions", label: "标注结果", val: counts.decisions, to: `/task/${encodeURIComponent(taskId)}/annotations` },
    { key: "gold", label: "训练集版本", val: counts.gold, to: `/task/${encodeURIComponent(taskId)}/gold` },
    { key: "models", label: "模型", val: counts.models, to: `/task/${encodeURIComponent(taskId)}/models` },
    { key: "jobs", label: "执行记录", val: counts.jobs, to: `/task/${encodeURIComponent(taskId)}/jobs` },
  ];

  const stages = profileStages(profile.data);
  const profileTitle = profile.data ? profileName(profile.data) : "等待流程预设";
  const nextStage = stages.find((stage) => normalizeStageStatus(stage.status) === "ready");

  return (
    <div>
      <div className="crumbs"><Link to="/">全部任务</Link> / {taskId}</div>
      <div className="page-header">
        <h2>{taskId}</h2>
        <p>{task && task.primary_label ? `主标签 ${task.primary_label.name}，记录编号字段 ${task.id_field}` : "任务概览"}</p>
      </div>
      <div className="card section-card">
        <div className="toolbar profile-toolbar">
          <div>
            <h3>流程预设</h3>
            <div className="status-line">
              当前预设：{profileTitle}{stages.length ? ` · ${stages.length} 个阶段` : ""}
              {nextStage ? ` · 下一步：${nextStage.title || nextStage.name || nextStage.id}` : ""}
            </div>
          </div>
        </div>
        {profile.loading && <div className="empty profile-empty">正在读取流程预设...</div>}
        {!profile.loading && profile.error && <div className="empty profile-empty">{profile.error}</div>}
        {!profile.loading && !profile.error && !stages.length && <div className="empty profile-empty">当前流程预设没有返回阶段列表。</div>}
        {!profile.loading && !profile.error && stages.length > 0 && (
          <div className="profile-stage-list">
            {stages.map((stage, index) => {
              const status = normalizeStageStatus(stage.status);
              const title = stage.title || stage.name || stage.id || `阶段 ${index + 1}`;
              return (
                <div className={`profile-stage profile-stage-${status}`} key={stage.id || `${title}-${index}`}>
                  <div className="profile-stage-index">{index + 1}</div>
                  <div className="profile-stage-main">
                    <div className="profile-stage-head">
                      <div>
                        <h4>{title}</h4>
                        {stage.description && <p>{stage.description}</p>}
                      </div>
                      <div className="profile-stage-actions">
                        <span className={`badge ${STATUS_BADGE[status]}`}>{STATUS_LABEL[status]}</span>
                        <Link
                          className={status === "ready" ? "btn btn-sm btn-primary" : "btn btn-sm"}
                          to={stageRoute(taskId, stage)}
                        >
                          {stageRouteLabel(stage, status)}
                        </Link>
                      </div>
                    </div>
                    <div className="profile-stage-details">
                      <div><span>输入条件</span><strong>{detailText(stage.required_inputs, "无前置输入")}</strong></div>
                      <div><span>下一步</span><strong>{stageActionHint(stage, status, index, stages.length)}</strong></div>
                      <div><span>阻塞原因</span><strong>{stageBlockReason(stage, status)}</strong></div>
                      <div><span>产物摘要</span><strong>{detailText(stage.outputs, "暂无产物摘要")}</strong></div>
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
      <div className="grid grid-cards">
        {cards.map((c) => (
          <Link key={c.key} to={c.to} className="card">
            <div className="stat"><span className="val">{c.val}</span><span className="key">{c.label}</span></div>
          </Link>
        ))}
      </div>
    </div>
  );
}
