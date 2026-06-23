import React, { useEffect, useState, useCallback } from "react";
import * as api from "./../api.js";
import { Link } from "./../router.jsx";

const STAGES = ["采样", "分批", "标注", "审核", "合并", "裁决", "Gold", "训练", "推理"];

export default function TaskOverviewPage({ task, taskId, onError }) {
  const [counts, setCounts] = useState({ samples: 0, runs: 0, gold: 0, models: 0, jobs: 0 });

  const reload = useCallback(async () => {
    if (!taskId) return;
    try {
      const [s, r, g, m, j] = await Promise.all([
        api.getTaskSamples(taskId),
        api.getTaskRuns(taskId),
        api.getTaskGoldVersions(taskId),
        api.getTaskModels(taskId),
        api.getJobs(taskId),
      ]);
      setCounts({
        samples: (s.samples || []).length,
        runs: (r.runs || []).length,
        gold: (g.gold_versions || []).length,
        models: (m.models || []).length,
        jobs: (j.jobs || []).length,
      });
    } catch (e) {
      onError(String(e));
    }
  }, [taskId, onError]);

  useEffect(() => { reload(); }, [reload]);

  const cards = [
    { key: "samples", label: "Artifact / 采样", val: counts.samples, to: `/task/${encodeURIComponent(taskId)}/samples` },
    { key: "runs", label: "Run / 标注运行", val: counts.runs, to: `/task/${encodeURIComponent(taskId)}/runs` },
    { key: "gold", label: "Gold 版本", val: counts.gold, to: `/task/${encodeURIComponent(taskId)}/gold` },
    { key: "models", label: "模型版本", val: counts.models, to: `/task/${encodeURIComponent(taskId)}/models` },
    { key: "jobs", label: "Job 任务", val: counts.jobs, to: `/task/${encodeURIComponent(taskId)}/jobs` },
  ];

  return (
    <div>
      <div className="crumbs"><Link to="/">全部任务</Link> / {taskId}</div>
      <div className="page-header">
        <h2>{taskId}</h2>
        <p>{task && task.primary_label ? `主标签 ${task.primary_label.name}，id 字段 ${task.id_field}` : "任务概览"}</p>
      </div>
      <div className="card" style={{ marginBottom: 16 }}>
        <h3>数据流阶段</h3>
        <div className="stage-flow">
          {STAGES.map((s, i) => (
            <React.Fragment key={s}>
              <span className="step">{s}</span>
              {i < STAGES.length - 1 && <span className="arrow">→</span>}
            </React.Fragment>
          ))}
        </div>
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
