export const VERIFICATION_LABELS = {
  unverified: "未验证",
  verified: "已验证",
  rejected: "已拒绝",
  identity_drift: "身份漂移",
  role_error: "角色错误",
  membership_missing: "工作区成员关系缺失",
  external_unavailable: "外部服务不可用",
};

export function valueOrDash(value) {
  return value === undefined || value === null || value === "" ? "-" : String(value);
}

export function formatTimestamp(value) {
  if (!value) return "-";
  return String(value).replace("T", " ").replace(/\.\d+Z$/, "").replace(/Z$/, "");
}

export function unwrapAnnotator(data) {
  if (!data || typeof data !== "object") return null;
  return data.annotator || data.record || data.mapping || data.item || data;
}

export function unwrapCohort(data) {
  if (!data || typeof data !== "object") return null;
  return data.cohort || data.record || data.item || data;
}

export function unwrapAnnotators(data) {
  if (Array.isArray(data)) return data;
  if (Array.isArray(data?.annotators)) return data.annotators;
  if (Array.isArray(data?.items)) return data.items;
  if (Array.isArray(data?.records)) return data.records;
  throw new Error("服务端返回的标注人员列表格式不受支持");
}

export function unwrapCohorts(data) {
  if (Array.isArray(data)) return data;
  if (Array.isArray(data?.cohorts)) return data.cohorts;
  if (Array.isArray(data?.items)) return data.items;
  if (Array.isArray(data?.records)) return data.records;
  throw new Error("服务端返回的人员组列表格式不受支持");
}

export function verificationCode(annotator) {
  return String(annotator?.verification?.status || "unverified").trim().toLowerCase() || "unverified";
}

export function verificationLabel(annotator) {
  return annotator?.verification?.label || VERIFICATION_LABELS[verificationCode(annotator)] || "未验证";
}

export function verificationBadgeClass(annotator) {
  const code = verificationCode(annotator);
  if (code === "verified") return "badge-green";
  if (["identity_drift", "role_error", "membership_missing", "external_unavailable", "rejected"].includes(code)) return "badge-red";
  return "badge-yellow";
}

export function isVerifiedAnnotator(annotator) {
  return verificationCode(annotator) === "verified"
    && annotator?.argilla_role === "annotator"
    && annotator?.membership?.present === true
    && annotator?.is_owner !== true;
}

export function annotatorLabel(annotator) {
  return annotator?.argilla_username || annotator?.scaffold_user_id || annotator?.annotator_id || "未命名标注人员";
}

export function cohortMemberIds(cohort) {
  const source = cohort?.member_annotator_ids ?? cohort?.annotator_ids ?? cohort?.members ?? [];
  if (!Array.isArray(source)) return [];
  return source
    .map((item) => (item && typeof item === "object" ? item.annotator_id || item.id : item))
    .map((item) => String(item || "").trim())
    .filter(Boolean);
}

export function workspaceMembershipLabel(annotator) {
  if (annotator?.membership?.present !== true) return "缺失";
  return annotator?.membership?.role ? `已加入（${annotator.membership.role}）` : "已加入";
}
