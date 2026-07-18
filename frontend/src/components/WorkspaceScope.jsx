import React from "react";

function workspaceLabel(item) {
  return item?.name && item.name !== item.slug ? `${item.name}（${item.slug}）` : item?.slug || "未命名工作区";
}

export default function WorkspaceScope({ workspace, workspaces = [], onChange, canManage = true }) {
  const options = workspaces.length ? workspaces : workspace ? [{ slug: workspace }] : [];
  const selected = options.find((item) => item.slug === workspace);
  const permissionMissing = selected && !canManage;

  return (
    <div className="workspace-scope" aria-label="Scaffold 工作区范围">
      <div className="workspace-scope-field">
        <label htmlFor="management-workspace">Scaffold 工作区</label>
        {options.length > 1 ? (
          <select id="management-workspace" value={workspace || ""} onChange={(event) => onChange?.(event.target.value)}>
            <option value="">请选择工作区</option>
            {options.map((item) => <option key={item.slug} value={item.slug}>{workspaceLabel(item)}</option>)}
          </select>
        ) : (
          <input id="management-workspace" value={selected ? workspaceLabel(selected) : "未取得可见工作区"} readOnly />
        )}
      </div>
      <div className={permissionMissing ? "workspace-scope-note workspace-scope-note-error" : "workspace-scope-note"}>
        {permissionMissing ? "当前工作区缺少 workspace:manage 权限，管理请求已禁用。" : "所有查询和写入都绑定当前 Scaffold 工作区。"}
      </div>
    </div>
  );
}
