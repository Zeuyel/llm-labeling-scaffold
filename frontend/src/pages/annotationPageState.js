function countItems(value) {
  return Array.isArray(value) ? value.length : 0;
}

export function annotationPageSections({
  annotationJobs = [],
  decisionArtifacts = [],
  agreementAudits = [],
  debugRuns = [],
} = {}) {
  return {
    primary: {
      key: "annotation_jobs",
      title: "标注任务",
      count: countItems(annotationJobs),
      defaultOpen: true,
      createAction: "create_annotation_job",
      detailTarget: "annotation_job_detail",
    },
    decisionArtifacts: {
      key: "decision_artifacts",
      title: "标注结果产物",
      count: countItems(decisionArtifacts),
      defaultOpen: false,
      detailTarget: "decision_artifact_detail",
    },
    agreementAudits: {
      key: "agreement_audits",
      title: "一致性检查记录",
      count: countItems(agreementAudits),
      defaultOpen: false,
      detailTarget: "agreement_audit_detail",
    },
    debugRuns: {
      key: "debug_runs",
      title: "本地模型标注调试",
      count: countItems(debugRuns),
      defaultOpen: false,
      detailTarget: "debug_run_actions",
    },
  };
}

export function annotationJobDetailActions({
  busy = false,
  job = null,
  decisions = [],
} = {}) {
  const hasJob = Boolean(job);
  const statusValue = job?.status === undefined || job?.status === null || job?.status === ""
    ? job?.state
    : job?.status;
  const status = String(statusValue || "").trim().toLowerCase();
  const archived = status === "archived" || status === "已归档";
  const downstreamCount = Array.isArray(decisions) ? decisions.length : 0;
  const disabledByState = busy || !hasJob;

  return {
    edit: {
      enabled: false,
      disabledReason: hasJob
        ? "已推送的标注任务不能在本地编辑；需要变更样本、批次或数据集策略时请新建标注任务。"
        : "请选择标注任务。",
    },
    delete: {
      enabled: false,
      disabledReason: hasJob
        ? "不支持从面板删除本地记录或远端 Argilla 数据集；无下游结果时可归档本地标注任务。"
        : "请选择标注任务。",
    },
    archive: {
      enabled: !disabledByState && !archived && downstreamCount === 0,
      disabledReason: !hasJob
        ? "请选择标注任务。"
        : archived
          ? "标注任务已归档。"
          : downstreamCount > 0
            ? "标注任务已有下游标注结果，不能归档。"
            : busy
              ? "当前有操作正在执行。"
              : "",
    },
  };
}

export function annotationJobGoldAction({ audits = [] } = {}) {
  const hasPassingAudit = (audits || []).some((audit) => audit?.passed === true);
  return {
    enabled: hasPassingAudit,
    disabledReason: hasPassingAudit ? "" : "通过一致性检查后构建 Gold",
  };
}
