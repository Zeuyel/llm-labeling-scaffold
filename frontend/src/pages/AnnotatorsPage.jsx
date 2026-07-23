import React, { useCallback, useEffect, useRef, useState } from "react";
import * as api from "../api.js";
import WorkspaceScope from "../components/WorkspaceScope.jsx";
import { Link, useRouter } from "../router.jsx";
import {
  annotatorLabel,
  formatTimestamp,
  unwrapAnnotator,
  unwrapAnnotators,
  valueOrDash,
  verificationBadgeClass,
  verificationLabel,
} from "./annotatorManagementState.js";
import { memberDisplayName, roleLabel, unwrapMembers } from "./memberManagementState.js";

const EMPTY_PROVISION_FORM = {
  principal_id: "",
  personal_workspace_name: "",
  initial_password: "",
};

const EMPTY_BIND_FORM = {
  principal_id: "",
  argilla_user_id: "",
  argilla_username: "",
  personal_workspace_id: "",
};

const ANNOTATION_MEMBER_ROLES = new Set(["annotator", "experimenter", "admin"]);
const ARGILLA_ROLE_LABELS = Object.freeze({
  owner: "所有者",
  admin: "管理员",
  annotator: "标注人员",
  experimenter: "实验者",
  reviewer: "审核员",
});

function errorMessage(error) {
  return error && typeof error.message === "string" ? error.message : String(error);
}

function annotationMemberCandidates(data) {
  return unwrapMembers(data).members.filter((member) => {
    const principalId = String(member?.principal_id || "").trim();
    const role = String(member?.role || "").trim().toLowerCase();
    const status = String(member?.status || "").trim().toLowerCase();
    return Boolean(principalId) && status === "active" && ANNOTATION_MEMBER_ROLES.has(role);
  });
}

function memberSelectionError(principalId, members) {
  if (!principalId) return "请选择具有标注权限的成员";
  if (!members.some((member) => String(member?.principal_id || "").trim() === principalId)) {
    return "所选成员当前不在可用标注成员列表中，请刷新后重试";
  }
  return "";
}

function argillaRoleLabel(role) {
  const normalized = String(role || "").trim().toLowerCase();
  return normalized ? ARGILLA_ROLE_LABELS[normalized] || "未知角色" : "-";
}

function annotatorMembershipLabel(annotator) {
  if (annotator?.membership?.present !== true) return "缺失";
  return annotator?.membership?.role
    ? `已加入（${argillaRoleLabel(annotator.membership.role)}）`
    : "已加入";
}

function requireAnnotator(data) {
  const record = unwrapAnnotator(data);
  if (!record || !record.annotator_id) throw new Error("服务端未返回有效的标注人员记录");
  return record;
}

function DetailField({ label, children }) {
  return (
    <div>
      <span>{label}</span>
      <strong>{children}</strong>
    </div>
  );
}

function AnnotationMemberSelect({
  value,
  members,
  loading,
  error,
  busy,
  onChange,
  onRetry,
}) {
  const hasCandidates = members.length > 0;
  return (
    <>
      <label className="field field-wide">
        <span>标注成员</span>
        <select
          value={value}
          onChange={(event) => onChange(event.target.value)}
          disabled={busy || loading || Boolean(error) || !hasCandidates}
        >
          <option value="">请选择有标注权限的成员</option>
          {members.map((member) => (
            <option key={member.principal_id} value={member.principal_id}>
              {memberDisplayName(member)}（{roleLabel(member.role)}）
            </option>
          ))}
        </select>
      </label>
      {loading && <div className="status-line">正在读取当前工作区成员...</div>}
      {!loading && error && (
        <div className="empty management-error">
          <span>{error}</span>
          <button className="btn btn-sm" type="button" onClick={onRetry} disabled={busy}>重试</button>
        </div>
      )}
      {!loading && !error && !hasCandidates && (
        <div className="hint">当前工作区暂无可选标注成员，请先到成员管理授予标注权限。</div>
      )}
    </>
  );
}

function AnnotatorDetailDrawer({
  annotator,
  loading,
  error,
  busy,
  onClose,
  onRetry,
  onVerify,
}) {
  return (
    <div className="drawer-backdrop" onClick={onClose}>
      <aside className="drawer-panel drawer-panel-wide" onClick={(event) => event.stopPropagation()}>
        <div className="drawer-head">
          <div>
            <h3>标注人员详情</h3>
            <p>查看 Scaffold 用户与 Argilla 身份的绑定和最近验证结果。</p>
          </div>
          <button className="btn btn-sm" type="button" onClick={onClose}>关闭</button>
        </div>
        {loading && <div className="status-line">正在读取标注人员详情...</div>}
        {!loading && error && (
          <div className="empty drawer-error">
            <div>{error}</div>
            <button className="btn btn-sm" type="button" onClick={onRetry} disabled={busy}>重试</button>
          </div>
        )}
        {!loading && !error && annotator && (
          <>
            <div className="drawer-detail-grid">
              <DetailField label="Scaffold 用户 ID">{valueOrDash(annotator.scaffold_user_id)}</DetailField>
              <DetailField label="标注人员 ID"><span className="mono-cell">{valueOrDash(annotator.annotator_id)}</span></DetailField>
              <DetailField label="Argilla 用户名">{valueOrDash(annotator.argilla_username)}</DetailField>
              <DetailField label="Argilla 用户 UUID"><span className="mono-cell">{valueOrDash(annotator.argilla_user_id)}</span></DetailField>
              <DetailField label="Argilla 角色">{argillaRoleLabel(annotator.argilla_role)}</DetailField>
              <DetailField label="个人工作区 UUID"><span className="mono-cell">{valueOrDash(annotator.personal_workspace_id)}</span></DetailField>
              <DetailField label="验证状态（verification_state）">
                <span className={`badge ${verificationBadgeClass(annotator)}`}>{verificationLabel(annotator)}</span>
              </DetailField>
              <DetailField label="最近验证时间">{formatTimestamp(annotator.verification?.last_verified_at)}</DetailField>
              <DetailField label="工作区成员关系">{annotatorMembershipLabel(annotator)}</DetailField>
              <DetailField label="Argilla 工作区 UUID"><span className="mono-cell">{valueOrDash(annotator.membership?.workspace_id)}</span></DetailField>
              <DetailField label="成员关系角色">{valueOrDash(annotator.membership?.role)}</DetailField>
              <DetailField label="记录更新时间">{formatTimestamp(annotator.updated_at)}</DetailField>
            </div>
            <div className="drawer-actions">
              <button className="btn btn-primary" type="button" disabled={busy} onClick={onVerify}>重新验证</button>
              <button className="btn btn-sm" type="button" disabled title="后端尚未接入删除接口">删除（未接入）</button>
              <span className="disabled-action-note">删除操作尚未接入专用接口，当前不可用。</span>
            </div>
          </>
        )}
      </aside>
    </div>
  );
}

function ProvisionDrawer({
  form,
  members,
  memberLoading,
  memberError,
  busy,
  onChange,
  onRetryMembers,
  onClose,
  onSubmit,
}) {
  return (
    <div className="drawer-backdrop" onClick={onClose}>
      <aside className="drawer-panel" onClick={(event) => event.stopPropagation()}>
        <div className="drawer-head">
          <div>
            <h3>新增标注人员</h3>
            <p>创建请求只提交所选 Scaffold 成员和一次性初始密码，外部用户名由服务端派生。</p>
          </div>
          <button className="btn btn-sm" type="button" onClick={onClose}>关闭</button>
        </div>
        <form onSubmit={onSubmit}>
          <div className="form-grid drawer-form-grid">
            <AnnotationMemberSelect
              value={form.principal_id}
              members={members}
              loading={memberLoading}
              error={memberError}
              busy={busy}
              onChange={(value) => onChange("principal_id", value)}
              onRetry={onRetryMembers}
            />
            <label className="field field-wide">
              <span>个人工作区名称（可选）</span>
              <input value={form.personal_workspace_name} onChange={(event) => onChange("personal_workspace_name", event.target.value)} autoComplete="off" />
            </label>
            <label className="field field-wide">
              <span>一次性初始密码</span>
              <input
                type="password"
                value={form.initial_password}
                onChange={(event) => onChange("initial_password", event.target.value)}
                autoComplete="off"
                autoCapitalize="none"
                spellCheck="false"
              />
              <span className="hint">只在本次请求体中发送，提交后立即清空；平台不提供读取或验证密码的功能。</span>
            </label>
          </div>
          <div className="drawer-actions">
            <button className="btn btn-primary" type="submit" disabled={busy || memberLoading || Boolean(memberError) || !members.length}>{busy ? "提交中..." : "创建并绑定"}</button>
            <button className="btn" type="button" disabled={busy} onClick={onClose}>取消</button>
          </div>
        </form>
      </aside>
    </div>
  );
}

function BindDrawer({
  form,
  members,
  memberLoading,
  memberError,
  busy,
  onChange,
  onRetryMembers,
  onClose,
  onSubmit,
}) {
  return (
    <div className="drawer-backdrop" onClick={onClose}>
      <aside className="drawer-panel" onClick={(event) => event.stopPropagation()}>
        <div className="drawer-head">
          <div>
            <h3>绑定已有 Argilla 身份</h3>
            <p>服务端会重新校验 UUID、角色、个人工作区和工作区成员关系。</p>
          </div>
          <button className="btn btn-sm" type="button" onClick={onClose}>关闭</button>
        </div>
        <form onSubmit={onSubmit}>
          <div className="form-grid drawer-form-grid">
            <AnnotationMemberSelect
              value={form.principal_id}
              members={members}
              loading={memberLoading}
              error={memberError}
              busy={busy}
              onChange={(value) => onChange("principal_id", value)}
              onRetry={onRetryMembers}
            />
            <label className="field field-wide">
              <span>Argilla 用户 UUID</span>
              <input value={form.argilla_user_id} onChange={(event) => onChange("argilla_user_id", event.target.value)} autoComplete="off" />
            </label>
            <label className="field field-wide">
              <span>Argilla 用户名（可选）</span>
              <input value={form.argilla_username} onChange={(event) => onChange("argilla_username", event.target.value)} autoComplete="off" />
            </label>
            <label className="field field-wide">
              <span>个人工作区 UUID（可选）</span>
              <input value={form.personal_workspace_id} onChange={(event) => onChange("personal_workspace_id", event.target.value)} autoComplete="off" />
            </label>
          </div>
          <div className="drawer-actions">
            <button className="btn btn-primary" type="submit" disabled={busy || memberLoading || Boolean(memberError) || !members.length}>{busy ? "提交中..." : "绑定身份"}</button>
            <button className="btn" type="button" disabled={busy} onClick={onClose}>取消</button>
          </div>
        </form>
      </aside>
    </div>
  );
}

export default function AnnotatorsPage({
  workspace,
  workspaces = [],
  canManage = false,
  onWorkspaceChange,
  detailId = "",
  onError,
}) {
  const { navigate } = useRouter();
  const [annotators, setAnnotators] = useState([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [memberCandidates, setMemberCandidates] = useState([]);
  const [memberLoading, setMemberLoading] = useState(false);
  const [memberLoadError, setMemberLoadError] = useState("");
  const [detail, setDetail] = useState(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState("");
  const [drawer, setDrawer] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [operationError, setOperationError] = useState("");
  const [provisionForm, setProvisionForm] = useState(EMPTY_PROVISION_FORM);
  const [bindForm, setBindForm] = useState(EMPTY_BIND_FORM);
  const pendingOperationRef = useRef(null);

  const showError = useCallback((error) => {
    const message = errorMessage(error);
    setOperationError(message);
    onError?.(message);
  }, [onError]);

  function clearPendingOperation() {
    pendingOperationRef.current = null;
  }

  function operationKey(operation, resource = "") {
    const current = pendingOperationRef.current;
    if (current?.operation === operation && current.workspace === workspace && current.resource === resource) {
      return current.key;
    }
    const key = api.managementIdempotencyKey(operation, workspace, resource);
    pendingOperationRef.current = { key, operation, resource, workspace };
    return key;
  }

  const loadList = useCallback(async () => {
    if (!workspace || !canManage) {
      setAnnotators([]);
      setLoadError("");
      return;
    }
    setLoading(true);
    setLoadError("");
    try {
      const data = await api.getAnnotators(workspace);
      setAnnotators(unwrapAnnotators(data));
    } catch (error) {
      const message = errorMessage(error);
      setLoadError(message);
      showError(message);
    } finally {
      setLoading(false);
    }
  }, [canManage, showError, workspace]);

  const loadMembers = useCallback(async () => {
    if (!workspace || !canManage) {
      setMemberCandidates([]);
      setMemberLoadError("");
      return;
    }
    setMemberLoading(true);
    setMemberLoadError("");
    setMemberCandidates([]);
    try {
      const data = await api.getMembers(workspace);
      setMemberCandidates(annotationMemberCandidates(data));
    } catch (error) {
      const message = errorMessage(error);
      setMemberLoadError(message);
      setMemberCandidates([]);
      showError(message);
    } finally {
      setMemberLoading(false);
    }
  }, [canManage, showError, workspace]);

  const loadDetail = useCallback(async (annotatorId) => {
    if (!workspace || !canManage || !annotatorId) return;
    setDetailLoading(true);
    setDetailError("");
    try {
      setDetail(requireAnnotator(await api.getAnnotator(annotatorId, workspace)));
    } catch (error) {
      const message = errorMessage(error);
      setDetailError(message);
      showError(message);
    } finally {
      setDetailLoading(false);
    }
  }, [canManage, showError, workspace]);

  useEffect(() => {
    setNotice("");
    setOperationError("");
    setDetail(null);
    setDetailError("");
    setProvisionForm(EMPTY_PROVISION_FORM);
    setBindForm(EMPTY_BIND_FORM);
    loadList();
    loadMembers();
  }, [loadList, loadMembers]);

  useEffect(() => {
    if (!detailId) {
      if (drawer === "detail") setDrawer("");
      return;
    }
    if (!workspace || !canManage) {
      setDrawer("");
      return;
    }
    setDrawer("detail");
    loadDetail(detailId);
  }, [detailId, loadDetail]);

  function closeDrawer() {
    clearPendingOperation();
    setDrawer("");
    setDetail(null);
    if (detailId) navigate("/annotators");
  }

  function openDetail(annotatorId) {
    navigate(`/annotators/${encodeURIComponent(annotatorId)}`);
  }

  function openProvision() {
    clearPendingOperation();
    setOperationError("");
    setNotice("");
    setProvisionForm(EMPTY_PROVISION_FORM);
    setDrawer("provision");
  }

  function openBind() {
    clearPendingOperation();
    setOperationError("");
    setNotice("");
    setBindForm(EMPTY_BIND_FORM);
    setDrawer("bind");
  }

  function validateWorkspace() {
    if (!workspace) return "请选择 Scaffold 工作区";
    if (!canManage) return "当前工作区缺少 workspace:manage 权限";
    return "";
  }

  async function submitProvision(event) {
    event.preventDefault();
    if (busy) return;
    const scopeError = validateWorkspace();
    if (scopeError) { showError(scopeError); return; }
    const principalId = provisionForm.principal_id.trim();
    const memberError = memberSelectionError(principalId, memberCandidates);
    if (memberError) { showError(memberError); return; }
    if (!provisionForm.initial_password) { showError("请填写一次性初始密码"); return; }
    const payload = {
      workspace,
      principal_id: principalId,
      personal_workspace_name: provisionForm.personal_workspace_name.trim() || undefined,
      initial_password: provisionForm.initial_password,
    };
    const idempotencyKey = operationKey("annotator.provision", payload.principal_id);
    setBusy(true);
    setOperationError("");
    setNotice("");
    try {
      requireAnnotator(await api.provisionAnnotator(payload, { idempotencyKey }));
      setProvisionForm(EMPTY_PROVISION_FORM);
      setDrawer("");
      clearPendingOperation();
      setNotice("标注人员已创建并绑定，初始密码已从表单清除。");
      await loadList();
    } catch (error) {
      showError(error);
    } finally {
      payload.initial_password = "";
      setProvisionForm((current) => ({ ...current, initial_password: "" }));
      setBusy(false);
    }
  }

  async function submitBind(event) {
    event.preventDefault();
    if (busy) return;
    const scopeError = validateWorkspace();
    if (scopeError) { showError(scopeError); return; }
    const principalId = bindForm.principal_id.trim();
    const memberError = memberSelectionError(principalId, memberCandidates);
    if (memberError) { showError(memberError); return; }
    if (!bindForm.argilla_user_id.trim()) { showError("请填写 Argilla 用户 UUID"); return; }
    const payload = {
      workspace,
      principal_id: principalId,
      argilla_user_id: bindForm.argilla_user_id.trim(),
      argilla_username: bindForm.argilla_username.trim() || undefined,
      personal_workspace_id: bindForm.personal_workspace_id.trim() || undefined,
    };
    const idempotencyKey = operationKey("annotator.bind", `${payload.principal_id}:${payload.argilla_user_id}`);
    setBusy(true);
    setOperationError("");
    setNotice("");
    try {
      requireAnnotator(await api.bindAnnotator(payload, { idempotencyKey }));
      setBindForm(EMPTY_BIND_FORM);
      setDrawer("");
      clearPendingOperation();
      setNotice("已有 Argilla 身份已提交绑定，服务端校验通过后才会进入人员组候选。");
      await loadList();
    } catch (error) {
      showError(error);
    } finally {
      setBusy(false);
    }
  }

  async function verify() {
    if (busy || !detail?.annotator_id) return;
    const scopeError = validateWorkspace();
    if (scopeError) { showError(scopeError); return; }
    setBusy(true);
    setOperationError("");
    setNotice("");
    try {
      const idempotencyKey = operationKey("annotator.verify", detail.annotator_id);
      const updated = requireAnnotator(await api.verifyAnnotator(detail.annotator_id, workspace, { idempotencyKey }));
      setDetail(updated);
      clearPendingOperation();
      setNotice("标注人员已重新验证，页面已更新最新验证结果。");
      await loadList();
    } catch (error) {
      showError(error);
    } finally {
      setBusy(false);
    }
  }

  const disabledReason = !workspace ? "请选择 Scaffold 工作区" : !canManage ? "缺少 workspace:manage 权限" : "";

  function handleWorkspaceChange(value) {
    clearPendingOperation();
    onWorkspaceChange?.(value);
  }

  function updateProvisionField(key, value) {
    clearPendingOperation();
    setProvisionForm((current) => ({ ...current, [key]: value }));
  }

  function updateBindField(key, value) {
    clearPendingOperation();
    setBindForm((current) => ({ ...current, [key]: value }));
  }

  return (
    <div>
      <div className="crumbs"><Link to="/">全部任务</Link> / 标注人员</div>
      <div className="page-header management-page-header">
        <div>
          <h2>标注人员</h2>
          <p>管理 Scaffold 用户与 Argilla annotator 身份绑定，验证状态由后端返回。</p>
        </div>
        <WorkspaceScope workspace={workspace} workspaces={workspaces} onChange={handleWorkspaceChange} canManage={canManage} />
      </div>

      {notice && <div className="status-banner">{notice}</div>}
      {operationError && <div className="error" role="alert">{operationError}</div>}

      <section className="card section-card">
        <div className="toolbar">
          <div className="toolbar-stack">
            <h3>标注人员名册（{annotators.length}）</h3>
            <span className="status-line">列表不展示契约未定义的账号启用状态字段。</span>
          </div>
          <div className="action-row">
            <button className="btn btn-primary" type="button" disabled={Boolean(disabledReason) || busy} title={disabledReason} onClick={openProvision}>新增标注人员</button>
            <button className="btn" type="button" disabled={Boolean(disabledReason) || busy} title={disabledReason} onClick={openBind}>绑定已有身份</button>
            <button className="btn btn-sm" type="button" disabled={loading || Boolean(disabledReason)} onClick={loadList}>刷新</button>
          </div>
        </div>

        {!workspace && <div className="empty">当前会话没有可用的 Scaffold 工作区，无法读取标注人员。</div>}
        {workspace && !canManage && <div className="empty">当前工作区缺少 workspace:manage 权限，标注人员管理已禁用。</div>}
        {workspace && canManage && loading && <div className="status-line">正在读取标注人员...</div>}
        {workspace && canManage && memberLoading && <div className="status-line">正在读取当前工作区成员...</div>}
        {workspace && canManage && !memberLoading && memberLoadError && (
          <div className="empty management-error">
            <div>当前工作区成员读取失败，请重试。</div>
            <button className="btn btn-sm" type="button" onClick={loadMembers} disabled={memberLoading}>重试</button>
          </div>
        )}
        {workspace && canManage && !memberLoading && !memberLoadError && !memberCandidates.length && (
          <div className="empty">当前工作区暂无可选标注成员，请先到成员管理授予标注权限。</div>
        )}
        {workspace && canManage && !loading && loadError && (
          <div className="empty management-error">
            <div>标注人员读取失败，请重试。</div>
            <button className="btn btn-sm" type="button" onClick={loadList} disabled={loading}>重试</button>
          </div>
        )}
        {workspace && canManage && !loading && !loadError && !annotators.length && <div className="empty">当前工作区暂无标注人员。</div>}
        {workspace && canManage && !loading && !loadError && annotators.length > 0 && (
          <div className="table-wrap management-table">
            <table>
              <thead>
                <tr>
                  <th>Scaffold 用户</th>
                  <th>Argilla 用户 UUID</th>
                  <th>角色</th>
                  <th>个人工作区</th>
                  <th>工作区成员关系</th>
                  <th>验证状态</th>
                  <th>最近验证时间</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {annotators.map((annotator) => (
                  <tr key={annotator.annotator_id} className="clickable-row" onClick={() => openDetail(annotator.annotator_id)}>
                    <td>
                      <div>{valueOrDash(annotator.scaffold_user_id)}</div>
                      <div className="muted">{annotatorLabel(annotator)}</div>
                    </td>
                    <td className="mono-cell">{valueOrDash(annotator.argilla_user_id)}</td>
                    <td>{valueOrDash(annotator.argilla_role)}</td>
                    <td className="mono-cell">{valueOrDash(annotator.personal_workspace_id)}</td>
                    <td>{annotatorMembershipLabel(annotator)}</td>
                    <td><span className={`badge ${verificationBadgeClass(annotator)}`}>{verificationLabel(annotator)}</span></td>
                    <td>{formatTimestamp(annotator.verification?.last_verified_at)}</td>
                    <td>
                      <button className="btn btn-sm" type="button" onClick={(event) => { event.stopPropagation(); openDetail(annotator.annotator_id); }}>详情</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {drawer === "detail" && (
        <AnnotatorDetailDrawer
          annotator={detail}
          loading={detailLoading}
          error={detailError}
          busy={busy}
          onClose={closeDrawer}
          onRetry={() => loadDetail(detailId)}
          onVerify={verify}
        />
      )}
      {drawer === "provision" && (
        <ProvisionDrawer
          form={provisionForm}
          members={memberCandidates}
          memberLoading={memberLoading}
          memberError={memberLoadError}
          busy={busy}
          onChange={updateProvisionField}
          onRetryMembers={loadMembers}
          onClose={closeDrawer}
          onSubmit={submitProvision}
        />
      )}
      {drawer === "bind" && (
        <BindDrawer
          form={bindForm}
          members={memberCandidates}
          memberLoading={memberLoading}
          memberError={memberLoadError}
          busy={busy}
          onChange={updateBindField}
          onRetryMembers={loadMembers}
          onClose={closeDrawer}
          onSubmit={submitBind}
        />
      )}
    </div>
  );
}
