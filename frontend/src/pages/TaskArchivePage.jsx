import React, { useEffect, useMemo, useState, useCallback } from "react";
import * as api from "./../api.js";
import { Link, useRouter } from "./../router.jsx";

const TYPE_LABEL = {
  inference: "推理结果",
  model: "模型",
  gold: "训练集",
  agreement_audit: "一致性检查",
  decision: "标注结果",
  annotation_job: "标注分发记录",
  run: "本地标注运行",
  sample: "样本",
  import: "导入数据",
};

function formatBytes(value) {
  const n = Number(value || 0);
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(1)} GB`;
}

export default function TaskArchivePage({ taskId, onError, onReloadTasks }) {
  const { navigate } = useRouter();
  const [plan, setPlan] = useState(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState("");
  const [reason, setReason] = useState("");
  const [result, setResult] = useState(null);

  const load = useCallback(async () => {
    if (!taskId) return;
    setLoading(true);
    try {
      setPlan(await api.getTaskArchivePlan(taskId));
    } catch (error) {
      onError(String(error));
    } finally {
      setLoading(false);
    }
  }, [taskId, onError]);

  useEffect(() => { load(); }, [load]);

  const cleanupFiles = plan?.cleanup?.files || [];
  const blocked = plan?.blocked || [];
  const warnings = plan?.warnings || [];
  const totalCleanupBytes = plan?.cleanup?.total_bytes || 0;
  const activeAssets = plan?.active_assets || [];
  const dependencies = plan?.dependencies || [];
  const runnableArchiveSteps = useMemo(
    () => (plan?.archive_order || []).filter((step) => step.operation),
    [plan],
  );

  async function runArchive() {
    if (!plan?.can_archive) return;
    const ok = window.confirm(`归档任务 ${taskId}？\n\n这会移动业务资产和任务配置，不会删除本地缓存文件。`);
    if (!ok) return;
    setBusy("archive");
    setResult(null);
    try {
      const data = await api.executeTaskArchive(taskId, reason);
      setResult({ type: "archive", data });
      await onReloadTasks?.();
      await load();
    } catch (error) {
      onError(String(error));
    } finally {
      setBusy("");
    }
  }

  async function runCleanup() {
    if (!cleanupFiles.length) return;
    const ok = window.confirm(`清理 ${cleanupFiles.length} 个本地缓存文件？\n\n这只删除本机 runs 缓存，不删除 R2 数据湖权威对象。`);
    if (!ok) return;
    setBusy("cleanup");
    setResult(null);
    try {
      const data = await api.cleanupTaskCache(taskId);
      setResult({ type: "cleanup", data });
      await load();
    } catch (error) {
      onError(String(error));
    } finally {
      setBusy("");
    }
  }

  return (
    <div>
      <div className="crumbs"><Link to="/">全部任务</Link> / <Link to={`/task/${encodeURIComponent(taskId)}`}>{taskId}</Link> / 归档向导</div>
      <div className="page-header archive-page-header">
        <div>
          <h2>归档向导</h2>
          <p>{taskId}</p>
        </div>
        <div className="action-row">
          <button className="btn btn-sm" onClick={load} disabled={loading || Boolean(busy)}>{loading ? "读取中..." : "刷新计划"}</button>
          <button className="btn btn-sm" onClick={() => navigate(`/task/${encodeURIComponent(taskId)}`)}>返回任务</button>
        </div>
      </div>

      <div className="grid archive-mode-grid">
        <div className="card">
          <h3>业务归档</h3>
          <p className="muted">停用任务，把活动资产移动到 `runs/{taskId}/_archive`，把任务配置移动到 `tasks/_archive`。</p>
        </div>
        <div className="card">
          <h3>清理本地缓存</h3>
          <p className="muted">释放本机空间，只删除下方列出的本地文件；不会删除 R2 数据湖权威对象。</p>
        </div>
      </div>

      {loading && <div className="empty">正在生成归档计划...</div>}
      {!loading && plan && (
        <>
          <div className="card section-card">
            <div className="toolbar">
              <div>
                <h3>计划摘要</h3>
                <div className="status-line">资产 {activeAssets.length} 个 · 归档步骤 {runnableArchiveSteps.length} 个 · 可清理 {cleanupFiles.length} 个文件</div>
              </div>
              <span className={plan.can_archive ? "badge badge-green" : "badge badge-red"}>
                {plan.can_archive ? "可归档" : "受限"}
              </span>
            </div>
            <div className="plan-summary-grid">
              <div><span>任务配置</span><strong>{plan.task_config?.path || "-"}</strong></div>
              <div><span>运行模式</span><strong>{plan.mode === "r2" ? "R2 数据湖" : "本地任务"}</strong></div>
              <div><span>清理空间</span><strong>{formatBytes(totalCleanupBytes)}</strong></div>
            </div>
            {blocked.map((item) => (
              <div className="stage-tip" key={item.code || item.message}>{item.message}</div>
            ))}
            {warnings.map((item) => (
              <div className="info-callout archive-warning" key={item.code || item.message}>
                <strong>{item.message}</strong>
                {item.paths?.length > 0 && <p>{item.paths.slice(0, 3).join("；")}</p>}
              </div>
            ))}
          </div>

          <div className="card section-card">
            <div className="toolbar">
              <div>
                <h3>业务归档顺序</h3>
                <div className="status-line">按下游到上游移动，默认不删除文件。</div>
              </div>
              <div className="archive-reason">
                <input value={reason} onChange={(event) => setReason(event.target.value)} placeholder="归档原因，可留空" />
                <button className="btn btn-primary" disabled={!plan.can_archive || busy === "archive"} onClick={runArchive}>
                  {busy === "archive" ? "归档中..." : "执行归档"}
                </button>
              </div>
            </div>
            <div className="workflow-stage-list">
              {(plan.archive_order || []).map((step, index) => (
                <div className={step.operation ? "workflow-stage workflow-stage-ready" : "workflow-stage"} key={step.asset_type}>
                  <div className="workflow-stage-index">{index + 1}</div>
                  <div className="workflow-stage-main">
                    <div className="workflow-stage-head">
                      <div>
                        <h4>{step.label}</h4>
                        <p>{step.operation ? `${step.operation.item_count || 0} 项 · ${formatBytes(step.operation.size_bytes)}` : "未发现活动资产"}</p>
                      </div>
                      <span className={step.operation ? "badge badge-blue" : "badge badge-gray"}>{step.operation ? "将移动" : "无操作"}</span>
                    </div>
                    {step.operation && (
                      <div className="path-cell archive-path">{step.operation.source_path || (step.operation.source_paths || []).join("；")}</div>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </div>

          <div className="grid archive-detail-grid">
            <div className="card section-card">
              <h3>活动资产</h3>
              {!activeAssets.length && <div className="empty">未发现活动资产。</div>}
              {activeAssets.length > 0 && (
                <div className="table-wrap">
                  <table>
                    <thead><tr><th>类型</th><th>编号</th><th>路径</th></tr></thead>
                    <tbody>
                      {activeAssets.map((asset, index) => (
                        <tr key={`${asset.asset_type}-${asset.asset_id}-${index}`}>
                          <td>{TYPE_LABEL[asset.asset_type] || asset.asset_type}</td>
                          <td>{asset.title || asset.asset_id}</td>
                          <td className="path-cell">{asset.path || "-"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
            <div className="card section-card">
              <h3>依赖关系</h3>
              {!dependencies.length && <div className="empty">未发现资产依赖。</div>}
              {dependencies.length > 0 && (
                <div className="table-wrap">
                  <table>
                    <thead><tr><th>上游</th><th>下游</th><th>关系</th></tr></thead>
                    <tbody>
                      {dependencies.map((edge, index) => (
                        <tr key={`${edge.source}-${edge.target}-${index}`}>
                          <td>{edge.source}</td>
                          <td>{edge.target}</td>
                          <td>{edge.reason}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </div>

          <div className="card section-card">
            <div className="toolbar">
              <div>
                <h3>清理本地缓存</h3>
                <div className="status-line">将删除本机文件 {cleanupFiles.length} 个，预计释放 {formatBytes(totalCleanupBytes)}。</div>
              </div>
              <button className="btn btn-danger" disabled={!cleanupFiles.length || busy === "cleanup"} onClick={runCleanup}>
                {busy === "cleanup" ? "清理中..." : "清理本地缓存"}
              </button>
            </div>
            {!cleanupFiles.length && <div className="empty">当前没有可清理的本地缓存文件。</div>}
            {cleanupFiles.length > 0 && (
              <div className="table-wrap">
                <table>
                  <thead><tr><th>文件</th><th>大小</th><th>权限</th></tr></thead>
                  <tbody>
                    {cleanupFiles.map((file) => (
                      <tr key={file.path}>
                        <td className="path-cell">{file.path}</td>
                        <td>{formatBytes(file.size_bytes)}</td>
                        <td>{file.writable ? "可删除" : `不可删除 UID ${file.owner_uid ?? "-"}`}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          {result && (
            <div className={result.data?.ok === false ? "error" : "status-banner"}>
              {result.type === "archive" ? "归档完成" : `清理完成：删除 ${result.data?.deleted_files?.length || 0} 个文件，失败 ${result.data?.errors?.length || 0} 个。`}
              {result.data?.errors?.length > 0 && (
                <pre className="log-box">{JSON.stringify(result.data.errors, null, 2)}</pre>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}
