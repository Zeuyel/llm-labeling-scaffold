import React, { useEffect, useState, useCallback } from "react";
import * as api from "./../api.js";
import { Link } from "./../router.jsx";

const STAGES = ["数据导入", "样本抽取", "标注分发", "标注回收", "训练集构建", "模型训练", "批量推理"];

export default function TaskOverviewPage({ task, taskId, onError }) {
  const [counts, setCounts] = useState({ imports: 0, samples: 0, decisions: 0, gold: 0, models: 0, jobs: 0 });

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

  useEffect(() => { reload(); }, [reload]);

  const cards = [
    { key: "imports", label: "导入数据", val: counts.imports, to: `/task/${encodeURIComponent(taskId)}/imports` },
    { key: "samples", label: "样本", val: counts.samples, to: `/task/${encodeURIComponent(taskId)}/samples` },
    { key: "decisions", label: "标注结果", val: counts.decisions, to: `/task/${encodeURIComponent(taskId)}/annotations` },
    { key: "gold", label: "训练集版本", val: counts.gold, to: `/task/${encodeURIComponent(taskId)}/gold` },
    { key: "models", label: "模型", val: counts.models, to: `/task/${encodeURIComponent(taskId)}/models` },
    { key: "jobs", label: "执行记录", val: counts.jobs, to: `/task/${encodeURIComponent(taskId)}/jobs` },
  ];

  return (
    <div>
      <div className="crumbs"><Link to="/">全部任务</Link> / {taskId}</div>
      <div className="page-header">
        <h2>{taskId}</h2>
        <p>{task && task.primary_label ? `主标签 ${task.primary_label.name}，记录编号字段 ${task.id_field}` : "任务概览"}</p>
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
