import React, { useEffect, useState } from "react";
import * as api from "./../api.js";
import { Link } from "./../router.jsx";

function value(value) {
  return value === undefined || value === null || value === "" ? "-" : String(value);
}

export default function DataAssetsPage({ onError }) {
  const [catalog, setCatalog] = useState(null);
  const [selectedId, setSelectedId] = useState("");
  const [detail, setDetail] = useState(null);
  const [busy, setBusy] = useState(false);
  const [detailBusy, setDetailBusy] = useState(false);

  async function reload() {
    setBusy(true);
    try {
      const next = await api.getDataLakeCatalog();
      setCatalog(next);
      setSelectedId((current) => (
        current && (next.datasets || []).some((item) => item.dataset_id === current) ? current : ""
      ));
      setDetail(null);
    } catch (error) {
      onError(String(error));
    } finally {
      setBusy(false);
    }
  }

  async function openDetail(datasetId) {
    setSelectedId(datasetId);
    setDetailBusy(true);
    try {
      const next = await api.getDataLakeCatalog(datasetId);
      setDetail((next.datasets || []).find((item) => item.dataset_id === datasetId) || null);
    } catch (error) {
      onError(String(error));
    } finally {
      setDetailBusy(false);
    }
  }

  useEffect(() => { reload(); }, []);

  return (
    <div>
      <div className="crumbs"><Link to="/">全部任务</Link> / 数据资产</div>
      <div className="page-header">
        <div className="toolbar">
          <div>
            <h2>数据资产</h2>
            <p>这里登记和查看 R2 数据湖中的元数据。读取数据内容只在明确执行任务时发生。</p>
          </div>
          <button className="btn btn-primary" disabled={busy} onClick={reload}>{busy ? "读取中..." : "刷新目录"}</button>
        </div>
      </div>

      {catalog && (
        <div className="card section-card">
          <div className="data-profile">
            <div><span>存储类型</span><strong>{value(catalog.backend)}</strong></div>
            <div><span>登记表版本</span><strong>{value(catalog.registry_version)}</strong></div>
            <div><span>数据集数量</span><strong>{(catalog.datasets || []).length}</strong></div>
          </div>
          <div className="status-line path-cell">登记表：{value(catalog.registry_uri)}</div>
        </div>
      )}

      {!catalog && busy && <div className="empty">正在读取数据资产目录...</div>}
      {catalog && !(catalog.datasets || []).length && <div className="empty">登记表中暂无数据集。</div>}
      {catalog && (catalog.datasets || []).length > 0 && (
        <div className="card section-card">
          <div className="toolbar"><h3>数据集台账（{catalog.datasets.length}）</h3></div>
          <div className="table-wrap">
            <table>
              <thead><tr><th>数据集</th><th>名称</th><th>层级</th><th>领域</th><th>状态</th><th>Manifest</th><th>操作</th></tr></thead>
              <tbody>
                {catalog.datasets.map((item) => (
                  <tr key={item.dataset_id} className={selectedId === item.dataset_id ? "row-selected" : "clickable-row"} onClick={() => openDetail(item.dataset_id)}>
                    <td className="mono-cell">{item.dataset_id}</td>
                    <td>{value(item.name)}</td>
                    <td>{value(item.layer)}</td>
                    <td>{value(item.domain)}</td>
                    <td><span className="badge badge-green">{value(item.status)}</span></td>
                    <td className="path-cell">{value(item.manifest_uri)}</td>
                    <td><button className="btn btn-sm" type="button" onClick={(event) => { event.stopPropagation(); openDetail(item.dataset_id); }}>查看清单</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {selectedId && (
        <div className="drawer-backdrop" onClick={() => setSelectedId("")}>
          <aside className="drawer-panel drawer-panel-wide" onClick={(event) => event.stopPropagation()}>
            <div className="drawer-head">
              <div><h3>数据集清单：{selectedId}</h3><p>只读查看登记的对象、版本和完整性信息。</p></div>
              <button className="btn btn-sm" type="button" onClick={() => setSelectedId("")}>关闭</button>
            </div>
            {detailBusy && <div className="empty">正在读取清单...</div>}
            {!detailBusy && detail && (
              <>
                <div className="drawer-detail-grid">
                  <div><span>名称</span><strong>{value(detail.name)}</strong></div>
                  <div><span>状态</span><strong>{value(detail.status)}</strong></div>
                  <div><span>登记表</span><strong className="path-cell">{value(detail.manifest_uri)}</strong></div>
                  <div><span>规范路径</span><strong className="path-cell">{value(detail.canonical_uri)}</strong></div>
                  <div><span>清单版本</span><strong>{value(detail.manifest?.version)}</strong></div>
                  <div><span>对象数量</span><strong>{value(detail.manifest?.object_count)}</strong></div>
                </div>
                <div className="table-wrap">
                  <table>
                    <thead><tr><th>相对路径</th><th>类型</th><th>行数</th><th>大小</th><th>编号字段</th><th>SHA256</th></tr></thead>
                    <tbody>
                      {(detail.manifest?.objects || []).map((item) => (
                        <tr key={`${item.path}-${item.sha256}`}>
                          <td className="path-cell">{value(item.path)}</td>
                          <td>{value(item.asset_type)}</td>
                          <td>{value(item.rows)}</td>
                          <td>{value(item.bytes)}</td>
                          <td>{value(item.id_field)}</td>
                          <td className="mono-cell">{value(item.sha256)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            )}
          </aside>
        </div>
      )}
    </div>
  );
}
