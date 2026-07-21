import React, { useCallback, useEffect, useRef, useState } from "react";
import * as api from "../api.js";
import WorkspaceScope from "../components/WorkspaceScope.jsx";
import {
  MEMBER_ROLE_LABELS,
  MEMBER_ROLE_OPTIONS,
  formatMemberTimestamp,
  invitationStatusBadgeClass,
  invitationStatusLabel,
  memberDisplayName,
  memberStatusBadgeClass,
  memberStatusLabel,
  roleLabel,
  unwrapMembers,
} from "./memberManagementState.js";

const EMPTY_INVITATION_FORM = {
  email: "",
  role: "viewer",
};

function operationName(operation, resource) {
  return `${operation}:${resource}`;
}

export default function MembersPage({
  workspace,
  workspaces = [],
  canManage = false,
  onWorkspaceChange,
  onError,
}) {
  const [members, setMembers] = useState([]);
  const [invitations, setInvitations] = useState([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [operationError, setOperationError] = useState("");
  const [notice, setNotice] = useState("");
  const [invitationForm, setInvitationForm] = useState(EMPTY_INVITATION_FORM);
  const [busyOperation, setBusyOperation] = useState("");
  const operationKeysRef = useRef(new Map());

  const showError = useCallback((error) => {
    const message = String(error);
    setOperationError(message);
    onError?.(message);
  }, [onError]);

  const loadMembers = useCallback(async () => {
    if (!workspace || !canManage) {
      setMembers([]);
      setInvitations([]);
      setLoadError("");
      return;
    }
    setLoading(true);
    setLoadError("");
    try {
      const data = unwrapMembers(await api.getMembers(workspace));
      setMembers(data.members);
      setInvitations(data.invitations);
    } catch (error) {
      const message = String(error);
      setLoadError(message);
      showError(message);
    } finally {
      setLoading(false);
    }
  }, [canManage, showError, workspace]);

  useEffect(() => {
    setNotice("");
    setOperationError("");
    setInvitationForm(EMPTY_INVITATION_FORM);
    loadMembers();
  }, [loadMembers]);

  function getOperationKey(operation, resource) {
    const signature = operationName(operation, `${workspace}:${resource}`);
    const existing = operationKeysRef.current.get(signature);
    if (existing) return { key: existing, signature };
    const key = api.managementIdempotencyKey(operation, workspace, resource);
    operationKeysRef.current.set(signature, key);
    return { key, signature };
  }

  function clearOperationKey(signature) {
    operationKeysRef.current.delete(signature);
  }

  function validateWorkspace() {
    if (!workspace) return "请选择 Scaffold 工作区";
    if (!canManage) return "当前工作区缺少 workspace:manage 权限";
    return "";
  }

  async function submitInvitation(event) {
    event.preventDefault();
    if (busyOperation) return;
    const scopeError = validateWorkspace();
    if (scopeError) {
      showError(scopeError);
      return;
    }
    const email = invitationForm.email.trim();
    const role = invitationForm.role;
    if (!email) {
      showError("请填写受邀邮箱");
      return;
    }
    if (!MEMBER_ROLE_LABELS[role]) {
      showError("请选择有效成员角色");
      return;
    }
    const resource = `${email}:${role}`;
    const { key, signature } = getOperationKey("member.invitation", resource);
    setBusyOperation(signature);
    setOperationError("");
    setNotice("");
    try {
      const result = await api.createMemberInvitation(
        { workspace, email, role },
        { idempotencyKey: key },
      );
      clearOperationKey(signature);
      setInvitationForm(EMPTY_INVITATION_FORM);
      setNotice(result?.action === "unchanged" ? "该邀请记录已存在。" : "邀请记录已创建。请通过已有渠道告知受邀用户。",);
      await loadMembers();
    } catch (error) {
      showError(error);
    } finally {
      setBusyOperation("");
    }
  }

  async function changeRole(member, role) {
    if (busyOperation || role === member.role) return;
    const scopeError = validateWorkspace();
    if (scopeError) {
      showError(scopeError);
      return;
    }
    if (!MEMBER_ROLE_LABELS[role]) {
      showError("请选择有效成员角色");
      return;
    }
    const resource = `${member.principal_id}:${role}`;
    const { key, signature } = getOperationKey("member.role", resource);
    setBusyOperation(signature);
    setOperationError("");
    setNotice("");
    try {
      const result = await api.updateMemberRole(
        member.principal_id,
        { workspace, role },
        { idempotencyKey: key },
      );
      clearOperationKey(signature);
      setNotice(result?.membership?.action === "unchanged" ? "成员角色未发生变化。" : `成员角色已更新为${roleLabel(role)}。`);
      await loadMembers();
    } catch (error) {
      showError(error);
    } finally {
      setBusyOperation("");
    }
  }

  async function revokeMember(member) {
    if (busyOperation) return;
    const scopeError = validateWorkspace();
    if (scopeError) {
      showError(scopeError);
      return;
    }
    if (typeof window !== "undefined" && typeof window.confirm === "function") {
      const confirmed = window.confirm(`确认撤销 ${memberDisplayName(member)} 的工作区成员资格吗？`);
      if (!confirmed) return;
    }
    const { key, signature } = getOperationKey("member.revoke", member.principal_id);
    setBusyOperation(signature);
    setOperationError("");
    setNotice("");
    try {
      const result = await api.revokeMember(
        member.principal_id,
        { workspace },
        { idempotencyKey: key },
      );
      clearOperationKey(signature);
      setNotice(result?.membership?.action === "unchanged" ? "成员已经撤销。" : "成员已撤销。");
      await loadMembers();
    } catch (error) {
      showError(error);
    } finally {
      setBusyOperation("");
    }
  }

  const disabled = Boolean(busyOperation) || loading;

  return (
    <main className="members-page">
      <div className="page-header management-page-header">
        <div>
          <h2>成员管理</h2>
          <p>管理当前 Scaffold 工作区的成员和邀请记录。</p>
        </div>
        <WorkspaceScope
          workspace={workspace}
          workspaces={workspaces}
          onChange={onWorkspaceChange}
          canManage={canManage}
        />
      </div>

      {notice && <div className="status-banner">{notice}</div>}
      {operationError && (
        <div className="error management-error">
          <span>{operationError}</span>
          <button className="btn btn-sm" type="button" onClick={() => setOperationError("")}>关闭</button>
        </div>
      )}

      {!workspace && <div className="empty">请选择一个 Scaffold 工作区。</div>}
      {workspace && !canManage && <div className="empty">当前工作区缺少成员管理权限。</div>}
      {workspace && canManage && (
        <>
          <section className="card section-card">
            <div className="toolbar">
              <div className="toolbar-stack">
                <h3>创建邀请</h3>
                <span className="status-line">面板只创建邀请记录，不负责发送通知；请通过已有渠道告知受邀用户。</span>
              </div>
            </div>
            <form className="form-grid" onSubmit={submitInvitation}>
              <label className="field field-half">
                <span>受邀邮箱</span>
                <input
                  type="email"
                  value={invitationForm.email}
                  onChange={(event) => setInvitationForm((current) => ({ ...current, email: event.target.value }))}
                  autoComplete="email"
                  required
                  disabled={disabled}
                />
              </label>
              <label className="field field-half">
                <span>成员角色</span>
                <select
                  value={invitationForm.role}
                  onChange={(event) => setInvitationForm((current) => ({ ...current, role: event.target.value }))}
                  disabled={disabled}
                >
                  {MEMBER_ROLE_OPTIONS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
                </select>
              </label>
              <div className="action-row form-actions">
                <button className="btn btn-primary" type="submit" disabled={disabled}>
                  {busyOperation?.startsWith("member.invitation:") ? "创建中..." : "创建邀请记录"}
                </button>
              </div>
            </form>
          </section>

          <section className="card section-card">
            <div className="toolbar">
              <div className="toolbar-stack">
                <h3>当前成员</h3>
                <span className="status-line">共 {members.length} 名成员</span>
              </div>
              {loading && <span className="status-line">正在读取...</span>}
            </div>
            {loadError && (
              <div className="empty management-error">
                <span>{loadError}</span>
                <button className="btn btn-sm" type="button" onClick={loadMembers} disabled={disabled}>重试</button>
              </div>
            )}
            {!loadError && !loading && members.length === 0 && <div className="empty">暂无成员。</div>}
            {!loadError && members.length > 0 && (
              <div className="table-wrap management-table members-table">
                <table>
                  <thead>
                    <tr>
                      <th>成员</th>
                      <th>角色</th>
                      <th>状态</th>
                      <th>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {members.map((member) => {
                      const knownRole = Boolean(MEMBER_ROLE_LABELS[member.role]);
                      return (
                        <tr key={member.principal_id}>
                          <td>
                            <strong>{memberDisplayName(member)}</strong>
                            <span className="muted text-cell">{member.email || "未设置邮箱"}</span>
                          </td>
                          <td>
                            <select
                              className="member-role-select"
                              aria-label={`${memberDisplayName(member)}的角色`}
                              value={knownRole ? member.role : ""}
                              onChange={(event) => changeRole(member, event.target.value)}
                              disabled={disabled || !knownRole}
                            >
                              {!knownRole && <option value="">未知角色</option>}
                              {MEMBER_ROLE_OPTIONS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
                            </select>
                          </td>
                          <td>
                            <span className={`badge ${memberStatusBadgeClass(member.status)}`}>{memberStatusLabel(member.status)}</span>
                          </td>
                          <td>
                            <button
                              className="btn btn-danger btn-sm"
                              type="button"
                              onClick={() => revokeMember(member)}
                              disabled={disabled}
                            >
                              {busyOperation === operationName("member.revoke", `${workspace}:${member.principal_id}`) ? "撤销中..." : "撤销成员"}
                            </button>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <section className="card section-card">
            <div className="toolbar">
              <div className="toolbar-stack">
                <h3>邀请记录</h3>
                <span className="status-line">共 {invitations.length} 条记录</span>
              </div>
            </div>
            {!loading && invitations.length === 0 && <div className="empty">暂无邀请记录。</div>}
            {invitations.length > 0 && (
              <div className="table-wrap management-table members-table">
                <table>
                  <thead>
                    <tr>
                      <th>邮箱</th>
                      <th>角色</th>
                      <th>状态</th>
                      <th>有效期至</th>
                      <th>认领成员</th>
                    </tr>
                  </thead>
                  <tbody>
                    {invitations.map((invitation) => (
                      <tr key={invitation.invitation_id}>
                        <td className="text-cell">{invitation.email || "-"}</td>
                        <td>{roleLabel(invitation.role)}</td>
                        <td>
                          <span className={`badge ${invitationStatusBadgeClass(invitation.status)}`}>
                            {invitationStatusLabel(invitation.status)}
                          </span>
                        </td>
                        <td>{formatMemberTimestamp(invitation.expires_at)}</td>
                        <td>{invitation.claimed_principal_id ? "已认领" : "-"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </>
      )}
    </main>
  );
}
