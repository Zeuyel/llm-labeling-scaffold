export const MEMBER_ROLE_LABELS = Object.freeze({
  viewer: "查看者",
  annotator: "标注人员",
  experimenter: "实验者",
  admin: "管理员",
});

export const MEMBER_ROLE_OPTIONS = Object.freeze(
  Object.entries(MEMBER_ROLE_LABELS).map(([value, label]) => ({ value, label })),
);

const MEMBER_STATUS_LABELS = Object.freeze({
  active: "活跃",
  inactive: "已停用",
});

const INVITATION_STATUS_LABELS = Object.freeze({
  pending: "待认领",
  claimed: "已认领",
  revoked: "已撤销",
  expired: "已过期",
});

export function roleLabel(role) {
  return MEMBER_ROLE_LABELS[String(role || "").trim().toLowerCase()] || "未知角色";
}

export function memberStatusLabel(status) {
  return MEMBER_STATUS_LABELS[String(status || "").trim().toLowerCase()] || "未知状态";
}

export function invitationStatusLabel(status) {
  return INVITATION_STATUS_LABELS[String(status || "").trim().toLowerCase()] || "未知状态";
}

export function memberStatusBadgeClass(status) {
  return String(status || "").trim().toLowerCase() === "active" ? "badge-green" : "badge-gray";
}

export function invitationStatusBadgeClass(status) {
  const normalized = String(status || "").trim().toLowerCase();
  if (normalized === "claimed") return "badge-green";
  if (normalized === "pending") return "badge-yellow";
  if (normalized === "revoked" || normalized === "expired") return "badge-gray";
  return "badge-gray";
}

export function unwrapMembers(data) {
  const source = Array.isArray(data) ? {} : data || {};
  return {
    workspace: typeof source.workspace === "string" ? source.workspace : "",
    members: Array.isArray(source.members) ? source.members : [],
    invitations: Array.isArray(source.invitations) ? source.invitations : [],
  };
}

export function memberDisplayName(member) {
  return member?.display_name || member?.email || member?.principal_id || "未命名成员";
}

export function formatMemberTimestamp(value) {
  if (!value) return "-";
  return String(value).replace("T", " ").replace(/\.\d+Z$/, "").replace(/Z$/, "");
}
