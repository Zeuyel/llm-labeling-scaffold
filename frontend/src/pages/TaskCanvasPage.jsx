import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as api from "./../api.js";
import { Link } from "./../router.jsx";

const NODE_W = 188;
const NODE_H = 78;
const COL_GAP = 112;
const ROW_GAP = 34;
const PAD = 56;

const STATUS_LABEL = {
  not_started: "未开始",
  ready: "可执行",
  completed: "已完成",
  blocked: "受阻",
};

const TYPE_LABEL = {
  task: "任务",
  stage: "阶段",
  import: "导入",
  sample: "样本",
  batch: "批次",
  annotation_job: "标注",
  agreement_audit: "审计",
  run: "运行",
  decision: "结果",
  gold: "训练集",
  model: "模型",
  inference: "推理",
};

const TYPE_ORDER = [
  "task",
  "stage",
  "import",
  "sample",
  "batch",
  "annotation_job",
  "run",
  "decision",
  "agreement_audit",
  "gold",
  "model",
  "inference",
];

function normalizeStatus(value) {
  const status = String(value || "not_started").toLowerCase();
  if (["done", "completed", "complete", "succeeded", "success", "finished"].includes(status)) return "completed";
  if (["ready", "available", "runnable", "active", "running", "in_progress"].includes(status)) return "ready";
  if (["blocked", "failed", "error", "incomplete"].includes(status)) return "blocked";
  return "not_started";
}

function shortText(value, fallback = "无摘要") {
  const text = String(value || "").trim();
  return text || fallback;
}

function compactValue(value) {
  if (value === null || value === undefined || value === "") return "";
  if (Array.isArray(value)) return value.map(compactValue).filter(Boolean).join("、");
  if (typeof value === "object") {
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

function layoutGraph(nodes, edges) {
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const indegree = new Map(nodes.map((node) => [node.id, 0]));
  const outgoing = new Map(nodes.map((node) => [node.id, []]));
  edges.forEach((edge) => {
    if (!byId.has(edge.source) || !byId.has(edge.target)) return;
    indegree.set(edge.target, (indegree.get(edge.target) || 0) + 1);
    outgoing.get(edge.source).push(edge.target);
  });

  const depth = new Map();
  const queue = nodes.filter((node) => (indegree.get(node.id) || 0) === 0).map((node) => node.id);
  queue.forEach((id) => depth.set(id, 0));
  for (let index = 0; index < queue.length; index += 1) {
    const id = queue[index];
    const nextDepth = (depth.get(id) || 0) + 1;
    outgoing.get(id).forEach((target) => {
      if ((depth.get(target) ?? -1) < nextDepth) depth.set(target, nextDepth);
      indegree.set(target, (indegree.get(target) || 1) - 1);
      if (indegree.get(target) === 0) queue.push(target);
    });
  }

  nodes.forEach((node) => {
    if (!depth.has(node.id)) {
      const fallbackDepth = Math.max(0, TYPE_ORDER.indexOf(node.type));
      depth.set(node.id, fallbackDepth);
    }
  });

  const columns = new Map();
  nodes
    .slice()
    .sort((a, b) => {
      const da = depth.get(a.id) || 0;
      const db = depth.get(b.id) || 0;
      if (da !== db) return da - db;
      const ta = TYPE_ORDER.indexOf(a.type);
      const tb = TYPE_ORDER.indexOf(b.type);
      if (ta !== tb) return ta - tb;
      return String(a.title).localeCompare(String(b.title), "zh-Hans-CN");
    })
    .forEach((node) => {
      const column = depth.get(node.id) || 0;
      if (!columns.has(column)) columns.set(column, []);
      columns.get(column).push(node);
    });

  const positioned = new Map();
  let maxX = PAD + NODE_W;
  let maxY = PAD + NODE_H;
  Array.from(columns.entries()).forEach(([column, items]) => {
    items.forEach((node, row) => {
      const x = PAD + column * (NODE_W + COL_GAP);
      const y = PAD + row * (NODE_H + ROW_GAP);
      positioned.set(node.id, { ...node, x, y, w: NODE_W, h: NODE_H });
      maxX = Math.max(maxX, x + NODE_W + PAD);
      maxY = Math.max(maxY, y + NODE_H + PAD);
    });
  });

  return {
    nodes: Array.from(positioned.values()),
    edges: edges
      .map((edge) => ({ ...edge, sourceNode: positioned.get(edge.source), targetNode: positioned.get(edge.target) }))
      .filter((edge) => edge.sourceNode && edge.targetNode),
    width: maxX,
    height: maxY,
  };
}

function edgePath(edge) {
  const sx = edge.sourceNode.x + edge.sourceNode.w;
  const sy = edge.sourceNode.y + edge.sourceNode.h / 2;
  const tx = edge.targetNode.x;
  const ty = edge.targetNode.y + edge.targetNode.h / 2;
  const mid = Math.max(32, (tx - sx) / 2);
  return `M ${sx} ${sy} C ${sx + mid} ${sy}, ${tx - mid} ${ty}, ${tx} ${ty}`;
}

export default function TaskCanvasPage({ task, taskId, onError }) {
  const [graph, setGraph] = useState({ nodes: [], edges: [] });
  const [loading, setLoading] = useState(false);
  const [selectedId, setSelectedId] = useState("");
  const [view, setView] = useState({ x: 24, y: 24, scale: 0.9 });
  const dragRef = useRef(null);

  const load = useCallback(async () => {
    if (!taskId) return;
    setLoading(true);
    try {
      const data = await api.getTaskGraph(taskId);
      setGraph({ nodes: data.nodes || [], edges: data.edges || [] });
      setSelectedId((current) => current || (data.nodes || [])[0]?.id || "");
    } catch (error) {
      onError(String(error));
    } finally {
      setLoading(false);
    }
  }, [taskId, onError]);

  useEffect(() => { load(); }, [load]);

  const layout = useMemo(() => layoutGraph(graph.nodes, graph.edges), [graph]);
  const selected = layout.nodes.find((node) => node.id === selectedId) || layout.nodes[0] || null;

  function zoom(delta) {
    setView((current) => ({
      ...current,
      scale: Math.min(1.8, Math.max(0.45, Number((current.scale + delta).toFixed(2)))),
    }));
  }

  function resetView() {
    setView({ x: 24, y: 24, scale: 0.9 });
  }

  function onPointerDown(event) {
    if (event.button !== 0) return;
    dragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      view,
    };
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function onPointerMove(event) {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    setView({
      ...drag.view,
      x: drag.view.x + event.clientX - drag.startX,
      y: drag.view.y + event.clientY - drag.startY,
    });
  }

  function onPointerUp(event) {
    if (dragRef.current?.pointerId === event.pointerId) dragRef.current = null;
  }

  return (
    <div>
      <div className="crumbs"><Link to="/">全部任务</Link> / <Link to={`/task/${encodeURIComponent(taskId)}`}>{taskId}</Link> / 流程画布</div>
      <div className="page-header canvas-page-header">
        <div>
          <h2>流程画布</h2>
          <p>{task?.primary_label ? `任务 ${taskId} · 主标签 ${task.primary_label.name}` : `任务 ${taskId}`}</p>
        </div>
        <div className="canvas-toolbar">
          <button className="btn btn-sm" type="button" onClick={() => zoom(-0.1)} title="缩小">-</button>
          <span className="canvas-scale">{Math.round(view.scale * 100)}%</span>
          <button className="btn btn-sm" type="button" onClick={() => zoom(0.1)} title="放大">+</button>
          <button className="btn btn-sm" type="button" onClick={resetView}>重置</button>
          <button className="btn btn-sm btn-primary" type="button" onClick={load} disabled={loading}>{loading ? "刷新中..." : "刷新"}</button>
        </div>
      </div>

      <div className="canvas-layout">
        <div
          className="canvas-viewport"
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerCancel={onPointerUp}
        >
          {loading && !layout.nodes.length && <div className="canvas-loading">正在读取流程图...</div>}
          <div
            className="canvas-world"
            style={{
              width: `${layout.width}px`,
              height: `${layout.height}px`,
              transform: `translate(${view.x}px, ${view.y}px) scale(${view.scale})`,
            }}
          >
            <svg className="canvas-edges" width={layout.width} height={layout.height} viewBox={`0 0 ${layout.width} ${layout.height}`}>
              <defs>
                <marker id="canvas-arrow" markerWidth="10" markerHeight="8" refX="9" refY="4" orient="auto">
                  <path d="M 0 0 L 10 4 L 0 8 z" />
                </marker>
              </defs>
              {layout.edges.map((edge, index) => (
                <g key={`${edge.source}-${edge.target}-${index}`}>
                  <path className="canvas-edge" d={edgePath(edge)} markerEnd="url(#canvas-arrow)" />
                  <title>{edge.reason}</title>
                </g>
              ))}
            </svg>
            {layout.nodes.map((node) => {
              const status = normalizeStatus(node.status);
              return (
                <button
                  key={node.id}
                  type="button"
                  className={`canvas-node canvas-node-${node.type} canvas-node-${status} ${selected?.id === node.id ? "is-selected" : ""}`}
                  style={{ left: `${node.x}px`, top: `${node.y}px`, width: `${node.w}px`, height: `${node.h}px` }}
                  onClick={(event) => {
                    event.stopPropagation();
                    setSelectedId(node.id);
                  }}
                >
                  <span className="canvas-node-kind">{TYPE_LABEL[node.type] || node.type}</span>
                  <strong>{node.title}</strong>
                  <em>{shortText(node.summary)}</em>
                </button>
              );
            })}
          </div>
        </div>

        <aside className="canvas-detail">
          {selected ? (
            <>
              <div className="canvas-detail-head">
                <span className={`badge badge-${normalizeStatus(selected.status) === "completed" ? "green" : normalizeStatus(selected.status) === "ready" ? "blue" : normalizeStatus(selected.status) === "blocked" ? "red" : "gray"}`}>
                  {STATUS_LABEL[normalizeStatus(selected.status)]}
                </span>
                <span className="muted">{TYPE_LABEL[selected.type] || selected.type}</span>
              </div>
              <h3>{selected.title}</h3>
              <p>{shortText(selected.summary)}</p>
              <div className="canvas-detail-grid">
                <div><span>节点编号</span><strong>{selected.id}</strong></div>
                <div><span>路径</span><strong>{selected.path || "无"}</strong></div>
              </div>
              {selected.route && <Link className="btn btn-sm btn-primary" to={selected.route}>打开对应页面</Link>}
              {selected.data && (
                <details className="canvas-detail-json">
                  <summary>详情数据</summary>
                  <pre>{compactValue(selected.data)}</pre>
                </details>
              )}
            </>
          ) : (
            <div className="empty">暂无节点</div>
          )}
        </aside>
      </div>
    </div>
  );
}
