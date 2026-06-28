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
