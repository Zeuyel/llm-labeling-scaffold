import React, { useCallback, useEffect, useState } from "react";
import * as api from "./../api.js";
import { Link } from "./../router.jsx";

export default function ImportsPage({ taskId, onError }) {
  const [items, setItems] = useState([]);
  const [name, setName] = useState("");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);

  const reload = useCallback(async () => {
    if (!taskId) return;
    try {
      const data = await api.getImports(taskId);
      setItems(data.imports || []);
    } catch (error) {
      onError(String(error));
    }
  }, [taskId, onError]);

  useEffect(() => {
    reload();
  }, [reload]);

  async function submit() {
    if (!name.trim() || !text.trim()) {
      onError("请填写导入编号并粘贴数据内容");
      return;
    }
    setBusy(true);
    try {
      await api.importJsonl(taskId, name.trim(), text);
      setName("");
      setText("");
      await reload();
    } catch (error) {
      onError(String(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <div className="crumbs">
        <Link to="/">全部任务</Link> / <Link to={`/task/${encodeURIComponent(taskId)}`}>{taskId}</Link> / 数据导入
      </div>
      <div className="page-header">
        <h2>数据导入</h2>
        <p>导入实验语料，后续可基于导入数据抽取样本</p>
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <h3>导入换行数据</h3>
        <div className="form-grid">
          <div className="field">
            <label>导入编号</label>
            <input value={name} onChange={(event) => setName(event.target.value)} placeholder="例如 第一批语料" />
          </div>
          <div className="field field-wide">
            <label>数据内容</label>
            <textarea
              rows={8}
              value={text}
              onChange={(event) => setText(event.target.value)}
              placeholder='每行一个 JSON 对象，例如 {"record_id":"r001","title":"标题","body":"正文"}'
            />
            <span className="hint">保存后可在样本管理中选择该导入数据作为抽样来源。</span>
          </div>
        </div>
        <button className="btn btn-primary" disabled={busy} onClick={submit}>保存导入数据</button>
      </div>

      <div className="card">
        <div className="toolbar">
          <h3>已导入数据（{items.length}）</h3>
          <button className="btn btn-sm" onClick={reload}>刷新</button>
        </div>
        {!items.length && <div className="empty">暂无导入数据</div>}
        {items.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr><th>导入编号</th><th>行数</th><th>保存路径</th></tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <tr key={item.import_id}>
                    <td>{item.import_id}</td>
                    <td>{item.rows}</td>
                    <td className="muted">{item.path}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
