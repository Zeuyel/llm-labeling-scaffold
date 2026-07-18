import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as api from "../api.js";
import WorkspaceScope from "../components/WorkspaceScope.jsx";
import { Link, useRouter } from "../router.jsx";
import {
  annotatorLabel,
  cohortMemberIds,
  formatTimestamp,
  isVerifiedAnnotator,
  unwrapAnnotators,
  unwrapCohort,
  unwrapCohorts,
  valueOrDash,
} from "./annotatorManagementState.js";

const EMPTY_DRAFT = {
  name: "",
  default_capacity: "",
  member_annotator_ids: [],
};

function requireCohort(data) {
  const record = unwrapCohort(data);
  if (!record || !record.cohort_id) throw new Error("服务端未返回有效的人员组记录");
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

function MemberPicker({ annotators, selectedIds, invalidIds = [], disabled, onChange }) {
  const candidates = annotators.filter(isVerifiedAnnotator);
  const selected = new Set(selectedIds);
  const unavailable = invalidIds.filter((id) => selected.has(id));

  function toggle(annotatorId) {
    const next = new Set(selected);
    if (next.has(annotatorId)) next.delete(annotatorId);
    else next.add(annotatorId);
    onChange([...next]);
  }

  function removeUnavailable(annotatorId) {
    onChange([...selected].filter((id) => id !== annotatorId));
  }

  if (!candidates.length && !unavailable.length) {
    return <div className="empty member-picker-empty">当前没有同时满足“已验证、annotator 角色、工作区成员关系有效”的候选人员。</div>;
  }

  return (
    <div className="member-picker">
      {candidates.map((annotator) => (
        <label className="member-option" key={annotator.annotator_id}>
          <input
            type="checkbox"
            checked={selected.has(annotator.annotator_id)}
            disabled={disabled}
            onChange={() => toggle(annotator.annotator_id)}
          />
          <span>
            <strong>{annotatorLabel(annotator)}</strong>
            <small>{annotator.scaffold_user_id || annotator.annotator_id} · {annotator.argilla_user_id || "无 Argilla UUID"}</small>
          </span>
        </label>
      ))}
      {unavailable.map((annotatorId) => (
        <div className="member-option member-option-invalid" key={annotatorId}>
          <span>
            <strong>当前成员不可再选择</strong>
            <small>{annotatorId}</small>
          </span>
          <button className="btn btn-sm" type="button" disabled={disabled} onClick={() => removeUnavailable(annotatorId)}>移除</button>
        </div>
      ))}
    </div>
  );
}

function CohortDetailDrawer({
  cohort,
  draft,
  annotators,
  loading,
  error,
  busy,
  invalidMembers,
  draftInvalidMembers,
  onChange,
  onClose,
  onRetry,
  onSaveBasics,
  onSaveMembers,
}) {
  return (
    <div className="drawer-backdrop" onClick={onClose}>
      <aside className="drawer-panel drawer-panel-wide" onClick={(event) => event.stopPropagation()}>
        <div className="drawer-head">
          <div>
            <h3>人员组详情</h3>
            <p>成员、容量和版本变更由后端创建新的 revision，不能覆盖已引用版本。</p>
          </div>
          <button className="btn btn-sm" type="button" onClick={onClose}>关闭</button>
        </div>
        {loading && <div className="status-line">正在读取人员组详情...</div>}
        {!loading && error && (
          <div className="empty drawer-error">
            <div>{error}</div>
            <button className="btn btn-sm" type="button" onClick={onRetry} disabled={busy}>重试</button>
          </div>
        )}
        {!loading && !error && cohort && (
          <>
            <div className="drawer-detail-grid">
              <DetailField label="人员组 ID"><span className="mono-cell">{valueOrDash(cohort.cohort_id)}</span></DetailField>
              <DetailField label="当前 revision">{valueOrDash(cohort.revision)}</DetailField>
              <DetailField label="创建时间">{formatTimestamp(cohort.created_at)}</DetailField>
              <DetailField label="更新时间">{formatTimestamp(cohort.updated_at)}</DetailField>
              <DetailField label="当前成员数">{cohortMemberIds(cohort).length}</DetailField>
              <DetailField label="当前默认容量">{valueOrDash(cohort.default_capacity)}</DetailField>
            </div>

            <section className="drawer-section">
              <h4>基本信息</h4>
              <div className="form-grid drawer-form-grid">
                <label className="field field-half">
                  <span>人员组名称</span>
                  <input value={draft.name} disabled={busy} onChange={(event) => onChange("name", event.target.value)} />
                </label>
                <label className="field field-half">
                  <span>默认容量</span>
                  <input type="number" min="1" step="1" value={draft.default_capacity} disabled={busy} onChange={(event) => onChange("default_capacity", event.target.value)} />
                </label>
              </div>
              <button className="btn btn-primary" type="button" disabled={busy || !Number.isInteger(cohort.revision)} onClick={onSaveBasics}>保存基本信息</button>
              {!Number.isInteger(cohort.revision) && <span className="disabled-action-note">服务端未返回 revision，无法安全编辑。</span>}
            </section>

            <section className="drawer-section">
              <div className="toolbar">
                <div className="toolbar-stack">
                  <h4>成员</h4>
                  <span className="status-line">只能选择已验证且工作区成员关系有效的 annotator。</span>
                </div>
                <span className="badge badge-blue">{draft.member_annotator_ids.length} 人</span>
              </div>
              {invalidMembers.length > 0 && (
                <div className="management-warning">
                  当前 revision 包含不可再选择的成员：{invalidMembers.join("、")}。可以移除这些成员；若要保留，需先修复标注人员验证状态。
                </div>
              )}
              <MemberPicker
                annotators={annotators}
                selectedIds={draft.member_annotator_ids}
                invalidIds={invalidMembers}
                disabled={busy}
                onChange={(member_annotator_ids) => onChange("member_annotator_ids", member_annotator_ids)}
              />
              <div className="drawer-actions">
                <button className="btn btn-primary" type="button" disabled={busy || !Number.isInteger(cohort.revision) || draftInvalidMembers.length > 0} onClick={onSaveMembers}>保存成员</button>
                <span className="disabled-action-note">保存使用当前 revision 作为 expected_revision，冲突时不会覆盖他人修改。</span>
              </div>
            </section>

            <section className="drawer-section">
              <button className="btn btn-sm" type="button" disabled title="后端尚未接入人员组删除接口">删除人员组（未接入）</button>
              <span className="disabled-action-note">删除操作尚未接入专用接口，当前不可用。</span>
            </section>
          </>
        )}
      </aside>
    </div>
  );
}

function CreateCohortDrawer({ draft, annotators, busy, onChange, onClose, onSubmit }) {
  return (
    <div className="drawer-backdrop" onClick={onClose}>
      <aside className="drawer-panel drawer-panel-wide" onClick={(event) => event.stopPropagation()}>
        <div className="drawer-head">
          <div>
            <h3>新建人员组</h3>
            <p>成员只来自当前工作区中已验证的 annotator，创建后由后端生成初始 revision。</p>
          </div>
          <button className="btn btn-sm" type="button" onClick={onClose}>关闭</button>
        </div>
        <form onSubmit={onSubmit}>
          <div className="form-grid drawer-form-grid">
            <label className="field field-half">
              <span>人员组名称</span>
              <input value={draft.name} disabled={busy} onChange={(event) => onChange("name", event.target.value)} />
            </label>
            <label className="field field-half">
              <span>默认容量</span>
              <input type="number" min="1" step="1" value={draft.default_capacity} disabled={busy} onChange={(event) => onChange("default_capacity", event.target.value)} />
            </label>
          </div>
          <div className="drawer-section">
            <div className="toolbar">
              <div className="toolbar-stack">
                <h4>选择成员</h4>
                <span className="status-line">未验证、非 annotator 角色或成员关系缺失的账号不会出现在这里。</span>
              </div>
              <span className="badge badge-blue">{draft.member_annotator_ids.length} 人</span>
            </div>
            <MemberPicker
              annotators={annotators}
              selectedIds={draft.member_annotator_ids}
              disabled={busy}
              onChange={(member_annotator_ids) => onChange("member_annotator_ids", member_annotator_ids)}
            />
          </div>
          <div className="drawer-actions">
            <button className="btn btn-primary" type="submit" disabled={busy}>{busy ? "提交中..." : "创建人员组"}</button>
            <button className="btn" type="button" disabled={busy} onClick={onClose}>取消</button>
          </div>
        </form>
      </aside>
    </div>
  );
}

export default function CohortsPage({
  workspace,
  workspaces = [],
  canManage = false,
  onWorkspaceChange,
  detailId = "",
  onError,
}) {
  const { navigate } = useRouter();
  const [cohorts, setCohorts] = useState([]);
  const [annotators, setAnnotators] = useState([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [detail, setDetail] = useState(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState("");
  const [drawer, setDrawer] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [operationError, setOperationError] = useState("");
  const [createDraft, setCreateDraft] = useState(EMPTY_DRAFT);
  const [detailDraft, setDetailDraft] = useState(EMPTY_DRAFT);
  const pendingOperationRef = useRef(null);

  const showError = useCallback((error) => {
    const message = String(error);
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
      setCohorts([]);
      setAnnotators([]);
      setLoadError("");
      return;
    }
    setLoading(true);
    setLoadError("");
    try {
      const [cohortData, annotatorData] = await Promise.all([
        api.getCohorts(workspace),
        api.getAnnotators(workspace),
      ]);
      setCohorts(unwrapCohorts(cohortData));
      setAnnotators(unwrapAnnotators(annotatorData));
    } catch (error) {
      const message = String(error);
      setLoadError(message);
      showError(message);
    } finally {
      setLoading(false);
    }
  }, [canManage, showError, workspace]);

  const loadDetail = useCallback(async (cohortId) => {
    if (!workspace || !canManage || !cohortId) return;
    setDetailLoading(true);
    setDetailError("");
    try {
      setDetail(requireCohort(await api.getCohort(cohortId, workspace)));
    } catch (error) {
      const message = String(error);
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
    loadList();
  }, [loadList]);

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

  useEffect(() => {
    if (!detail) return;
    setDetailDraft({
      name: String(detail.name || ""),
      default_capacity: detail.default_capacity ?? "",
      member_annotator_ids: cohortMemberIds(detail),
    });
  }, [detail]);

  const verifiedIds = useMemo(
    () => new Set(annotators.filter(isVerifiedAnnotator).map((item) => item.annotator_id)),
    [annotators],
  );
  const invalidDetailMembers = useMemo(
    () => cohortMemberIds(detail).filter((id) => !verifiedIds.has(id)),
    [detail, verifiedIds],
  );
  const draftInvalidMembers = useMemo(
    () => detailDraft.member_annotator_ids.filter((id) => !verifiedIds.has(id)),
    [detailDraft.member_annotator_ids, verifiedIds],
  );

  function closeDrawer() {
    clearPendingOperation();
    setDrawer("");
    setDetail(null);
    if (detailId) navigate("/cohorts");
  }

  function openDetail(cohortId) {
    navigate(`/cohorts/${encodeURIComponent(cohortId)}`);
  }

  function openCreate() {
    clearPendingOperation();
    setCreateDraft(EMPTY_DRAFT);
    setOperationError("");
    setNotice("");
    setDrawer("create");
  }

  function validateWorkspace() {
    if (!workspace) return "请选择 Scaffold 工作区";
    if (!canManage) return "当前工作区缺少 workspace:manage 权限";
    return "";
  }

  function validateDraft(draft) {
    if (!draft.name.trim()) return "请填写人员组名称";
    const capacity = Number(draft.default_capacity);
    if (!Number.isInteger(capacity) || capacity < 1) return "默认容量必须是大于 0 的整数";
    if (draft.member_annotator_ids.some((id) => !verifiedIds.has(id))) return "人员组成员必须全部是已验证 annotator";
    return "";
  }

  async function submitCreate(event) {
    event.preventDefault();
    if (busy) return;
    const scopeError = validateWorkspace();
    const draftError = scopeError || validateDraft(createDraft);
    if (draftError) { showError(draftError); return; }
    const payload = {
      workspace,
      name: createDraft.name.trim(),
      default_capacity: Number(createDraft.default_capacity),
      member_annotator_ids: [...createDraft.member_annotator_ids],
    };
    const idempotencyKey = operationKey("cohort.create", payload.name);
    setBusy(true);
    setOperationError("");
    setNotice("");
    try {
      requireCohort(await api.createCohort(payload, { idempotencyKey }));
      setCreateDraft(EMPTY_DRAFT);
      setDrawer("");
      clearPendingOperation();
      setNotice("人员组已创建。");
      await loadList();
    } catch (error) {
      showError(error);
    } finally {
      setBusy(false);
    }
  }

  async function saveBasics() {
    if (busy || !detail) return;
    const scopeError = validateWorkspace();
    const draftError = scopeError || validateDraft({ ...detailDraft, member_annotator_ids: detailDraft.member_annotator_ids });
    if (draftError && draftError !== "人员组成员必须全部是已验证 annotator") { showError(draftError); return; }
    if (!Number.isInteger(detail.revision)) { showError("服务端未返回 revision，无法安全编辑"); return; }
    setBusy(true);
    setOperationError("");
    setNotice("");
    try {
      const idempotencyKey = operationKey("cohort.update", `${detail.cohort_id}:${detail.revision}`);
      const updated = requireCohort(await api.updateCohort(detail.cohort_id, {
        workspace,
        cohort_id: detail.cohort_id,
        name: detailDraft.name.trim(),
        default_capacity: Number(detailDraft.default_capacity),
        expected_revision: detail.revision,
      }, { idempotencyKey }));
      setDetail(updated);
      clearPendingOperation();
      setNotice("人员组基本信息已保存，并生成新的 revision。");
      await loadList();
    } catch (error) {
      showError(error);
    } finally {
      setBusy(false);
    }
  }

  async function saveMembers() {
    if (busy || !detail) return;
    const scopeError = validateWorkspace();
    if (scopeError) { showError(scopeError); return; }
    if (!Number.isInteger(detail.revision)) { showError("服务端未返回 revision，无法安全编辑"); return; }
    if (draftInvalidMembers.length > 0) { showError("人员组成员必须全部是已验证 annotator"); return; }
    setBusy(true);
    setOperationError("");
    setNotice("");
    try {
      const idempotencyKey = operationKey("cohort.members", `${detail.cohort_id}:${detail.revision}`);
      const updated = requireCohort(await api.replaceCohortMembers(detail.cohort_id, {
        workspace,
        cohort_id: detail.cohort_id,
        member_annotator_ids: [...detailDraft.member_annotator_ids],
        expected_revision: detail.revision,
      }, { idempotencyKey }));
      setDetail(updated);
      clearPendingOperation();
      setNotice("人员组成员已保存，并生成新的 revision。");
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

  function updateCreateDraft(key, value) {
    clearPendingOperation();
    setCreateDraft((current) => ({ ...current, [key]: value }));
  }

  function updateDetailDraft(key, value) {
    clearPendingOperation();
    setDetailDraft((current) => ({ ...current, [key]: value }));
  }

  return (
    <div>
      <div className="crumbs"><Link to="/">全部任务</Link> / 人员组</div>
      <div className="page-header management-page-header">
        <div>
          <h2>人员组</h2>
          <p>维护可复用的标注人员集合、默认容量和不可变 revision。</p>
        </div>
        <WorkspaceScope workspace={workspace} workspaces={workspaces} onChange={handleWorkspaceChange} canManage={canManage} />
      </div>

      {notice && <div className="status-banner">{notice}</div>}
      {operationError && <div className="error" role="alert">{operationError}</div>}

      <section className="card section-card">
        <div className="toolbar">
          <div className="toolbar-stack">
            <h3>人员组列表（{cohorts.length}）</h3>
            <span className="status-line">每次编辑使用 expected_revision，冲突会 fail closed。</span>
          </div>
          <div className="action-row">
            <button className="btn btn-primary" type="button" disabled={Boolean(disabledReason) || busy} title={disabledReason} onClick={openCreate}>新建人员组</button>
            <button className="btn btn-sm" type="button" disabled={loading || Boolean(disabledReason)} onClick={loadList}>刷新</button>
          </div>
        </div>

        {!workspace && <div className="empty">当前会话没有可用的 Scaffold 工作区，无法读取人员组。</div>}
        {workspace && !canManage && <div className="empty">当前工作区缺少 workspace:manage 权限，人员组管理已禁用。</div>}
        {workspace && canManage && loading && <div className="status-line">正在读取人员组和已验证标注人员...</div>}
        {workspace && canManage && !loading && loadError && (
          <div className="empty management-error">
            <div>人员组读取失败，请重试。</div>
            <button className="btn btn-sm" type="button" onClick={loadList} disabled={loading}>重试</button>
          </div>
        )}
        {workspace && canManage && !loading && !loadError && !cohorts.length && <div className="empty">当前工作区暂无人员组。</div>}
        {workspace && canManage && !loading && !loadError && cohorts.length > 0 && (
          <div className="table-wrap management-table">
            <table>
              <thead><tr><th>人员组</th><th>成员数</th><th>默认容量</th><th>revision</th><th>创建时间</th><th>更新时间</th><th>操作</th></tr></thead>
              <tbody>
                {cohorts.map((cohort) => (
                  <tr key={cohort.cohort_id} className="clickable-row" onClick={() => openDetail(cohort.cohort_id)}>
                    <td>
                      <div>{valueOrDash(cohort.name)}</div>
                      <div className="muted mono-cell">{valueOrDash(cohort.cohort_id)}</div>
                    </td>
                    <td>{cohortMemberIds(cohort).length}</td>
                    <td>{valueOrDash(cohort.default_capacity)}</td>
                    <td>{valueOrDash(cohort.revision)}</td>
                    <td>{formatTimestamp(cohort.created_at)}</td>
                    <td>{formatTimestamp(cohort.updated_at)}</td>
                    <td><button className="btn btn-sm" type="button" onClick={(event) => { event.stopPropagation(); openDetail(cohort.cohort_id); }}>详情</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {drawer === "detail" && (
        <CohortDetailDrawer
          cohort={detail}
          draft={detailDraft}
          annotators={annotators}
          loading={detailLoading}
          error={detailError}
          busy={busy}
          invalidMembers={invalidDetailMembers}
          draftInvalidMembers={draftInvalidMembers}
          onChange={updateDetailDraft}
          onClose={closeDrawer}
          onRetry={() => loadDetail(detailId)}
          onSaveBasics={saveBasics}
          onSaveMembers={saveMembers}
        />
      )}
      {drawer === "create" && (
        <CreateCohortDrawer
          draft={createDraft}
          annotators={annotators}
          busy={busy}
          onChange={updateCreateDraft}
          onClose={closeDrawer}
          onSubmit={submitCreate}
        />
      )}
    </div>
  );
}
