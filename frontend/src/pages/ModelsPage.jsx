import React, { useCallback, useEffect, useMemo, useState } from "react";
import * as api from "./../api.js";
import { Link } from "./../router.jsx";
import {
  displayResourceValue,
  goldPathForTask,
  goldSummary,
  modelInferAction,
  modelResourceKey,
  modelSummary,
} from "./trainingResourceDisplay.js";

function DetailField({ label, value }) {
  return (
    <div>
      <span>{label}</span>
      <strong>{displayResourceValue(value)}</strong>
    </div>
  );
}

function statusBadgeClass(status) {
  if (status === "可用") return "badge-green";
  if (status === "失败" || status === "记录不完整") return "badge-red";
  return "badge-gray";
}

export default function ModelsPage({ task, taskId, onError }) {
  const [models, setModels] = useState([]);
  const [golds, setGolds] = useState([]);
  const [gold, setGold] = useState("");
  const [modelId, setModelId] = useState("");
  const [trainer, setTrainer] = useState("tfidf_sgd");
  const [trainerParams, setTrainerParams] = useState("{}");
  const [useMlflow, setUseMlflow] = useState(false);
  const [mlflowExperiment, setMlflowExperiment] = useState("");
  const [model, setModel] = useState("");
  const [corpus, setCorpus] = useState("");
  const [output, setOutput] = useState("");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [drawer, setDrawer] = useState("");
  const [selectedModelKey, setSelectedModelKey] = useState("");

  const reload = useCallback(async () => {
    if (!taskId) return;
    setLoading(true);
    setLoadError("");
    try {
      const [modelData, goldData] = await Promise.all([
        api.getTaskModels(taskId),
        api.getTaskGoldVersions(taskId),
      ]);
      setModels(modelData.models || []);
      setGolds(goldData.gold_versions || []);
    } catch (error) {
      const message = String(error);
      setLoadError(message);
      onError(message);
    } finally {
      setLoading(false);
    }
  }, [taskId, onError]);

  useEffect(() => {
    reload();
  }, [reload]);

  const selectedModel = useMemo(
    () => models.find((item) => modelResourceKey(item) === selectedModelKey),
    [models, selectedModelKey],
  );
  const selectedSummary = selectedModel ? modelSummary(selectedModel) : null;
  const selectedInferAction = selectedModel ? modelInferAction(selectedModel) : null;
  const selectedTrainingGold = selectedSummary
    ? golds.find((item) => goldPathForTask(item, taskId) === selectedSummary.goldPath)
    : null;

  function openTrainDrawer() {
    setDrawer("train");
  }

  function openInferDrawer(modelPath = "") {
    if (modelPath) setModel(modelPath);
    setDrawer("infer");
  }

  function openModelDetail(item) {
    setSelectedModelKey(modelResourceKey(item));
    setDrawer("detail");
  }

  async function train() {
    if (!task || !gold || !modelId) {
      onError("请选择训练集版本并填写模型编号");
      return;
    }
    let parsedParams = {};
    try {
      parsedParams = trainerParams.trim() ? JSON.parse(trainerParams) : {};
    } catch {
      onError("训练参数必须是 JSON 对象");
      return;
    }
    setBusy(true);
    try {
      const job = await api.startAction(task.path, "train", {
        gold,
        model_id: modelId,
        trainer,
        trainer_params: parsedParams,
        mlflow: useMlflow ? { experiment: mlflowExperiment || taskId } : null,
      });
      const finished = job?.id ? await api.waitForJob(taskId, job.id) : null;
      if (finished?.status === "failed") {
        throw new Error(finished.error || "执行失败");
      }
      setModelId("");
      setDrawer("");
      await reload();
    } catch (error) {
      onError(String(error));
    } finally {
      setBusy(false);
    }
  }

  async function infer() {
    if (!task || !model || !corpus || !output) {
      onError("请选择模型并填写语料路径、输出目录");
      return;
    }
    setBusy(true);
    try {
      const job = await api.startAction(task.path, "infer", { model, corpus, output });
      const finished = job?.id ? await api.waitForJob(taskId, job.id) : null;
      if (finished?.status === "failed") {
        throw new Error(finished.error || "执行失败");
      }
      setDrawer("");
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
        <Link to="/">全部任务</Link> /{" "}
        <Link to={`/task/${encodeURIComponent(taskId)}`}>{taskId}</Link> / 模型管理
      </div>
      <div className="page-header">
        <h2>模型管理</h2>
        <p>默认登记到本地文件目录，按需同步到外部模型记录服务</p>
      </div>

      <div className="card">
        <div className="toolbar">
          <div className="toolbar-stack">
            <h3>本地模型登记（{models.length}）</h3>
            <span className="status-line">点击行查看 manifest、metrics、训练来源和批量推理入口。</span>
          </div>
          <div className="action-row">
            <button className="btn btn-primary" type="button" disabled={busy} onClick={openTrainDrawer}>训练模型</button>
            <button className="btn" type="button" disabled={busy} onClick={() => openInferDrawer()}>批量推理</button>
            <button className="btn btn-sm" type="button" disabled={loading} onClick={reload}>刷新</button>
          </div>
        </div>
        {loading && <div className="status-line">正在读取模型登记...</div>}
        {!loading && loadError && <div className="empty">读取模型登记失败，请查看页面错误信息后重试。</div>}
        {!loading && !loadError && !models.length && (
          <div className="empty action-empty">
            <span>暂无模型</span>
            <button className="btn btn-primary" type="button" onClick={openTrainDrawer}>训练模型</button>
          </div>
        )}
        {!loading && !loadError && models.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>模型编号</th>
                  <th>状态</th>
                  <th>训练器标识</th>
                  <th>训练/测试行数</th>
                  <th>指标摘要</th>
                  <th>外部记录</th>
                  <th>标签</th>
                  <th>创建时间</th>
                  <th>路径</th>
                </tr>
              </thead>
              <tbody>
                {models.map((item) => {
                  const summary = modelSummary(item);
                  return (
                    <tr className="clickable-row" key={summary.key} onClick={() => openModelDetail(item)}>
                      <td><span className="badge badge-blue">{summary.modelId}</span></td>
                      <td><span className={`badge ${statusBadgeClass(summary.status)}`}>{summary.status}</span></td>
                      <td>{summary.trainer}</td>
                      <td>{summary.trainRows} / {summary.testRows}</td>
                      <td className="muted text-cell">{summary.metricSummary}</td>
                      <td>{summary.externalRecord}</td>
                      <td className="muted text-cell">{summary.labels}</td>
                      <td className="muted">{summary.createdAt}</td>
                      <td className="muted path-cell">{summary.path}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {drawer === "train" && (
        <div className="drawer-backdrop" onClick={() => setDrawer("")}>
          <aside className="drawer-panel" onClick={(event) => event.stopPropagation()}>
            <div className="drawer-head">
              <div>
                <h3>训练模型</h3>
                <p>选择 gold version 和训练器，任务会通过现有 job 流程执行。</p>
              </div>
              <button className="btn btn-sm" type="button" onClick={() => setDrawer("")}>关闭</button>
            </div>
            <div className="form-grid drawer-form-grid">
              <div className="field field-half">
                <label>训练集版本</label>
                <select value={gold} onChange={(event) => setGold(event.target.value)}>
                  <option value="">选择训练集版本</option>
                  {golds.map((item) => {
                    const summary = goldSummary(item, taskId);
                    return (
                      <option key={summary.key} value={summary.path}>
                        {summary.version} · {summary.rows} 行
                      </option>
                    );
                  })}
                </select>
              </div>
              <div className="field field-half">
                <label>模型编号</label>
                <input
                  value={modelId}
                  onChange={(event) => setModelId(event.target.value)}
                  placeholder="例如 bert_v001"
                />
              </div>
              <div className="field field-half">
                <label>训练器标识</label>
                <input
                  value={trainer}
                  onChange={(event) => setTrainer(event.target.value)}
                  placeholder="tfidf_sgd 或 package.module:function"
                />
                <span className="hint">内置 tfidf_sgd 是轻量基线；自定义训练器可填模块函数路径。</span>
              </div>
              <div className="field field-half">
                <label>外部模型记录服务（可选）</label>
                <select value={useMlflow ? "yes" : "no"} onChange={(event) => setUseMlflow(event.target.value === "yes")}>
                  <option value="no">仅本地文件登记</option>
                  <option value="yes">同步到外部记录服务</option>
                </select>
              </div>
              <div className="field field-half">
                <label>外部实验名称</label>
                <input
                  value={mlflowExperiment}
                  onChange={(event) => setMlflowExperiment(event.target.value)}
                  placeholder={taskId}
                  disabled={!useMlflow}
                />
              </div>
              <div className="field field-wide">
                <label>训练参数 JSON</label>
                <textarea
                  rows={5}
                  value={trainerParams}
                  onChange={(event) => setTrainerParams(event.target.value)}
                  placeholder='{"base_model":"bert-base-chinese","epochs":3,"learning_rate":2e-5}'
                />
              </div>
            </div>
            {!golds.length && <div className="stage-tip">暂无训练集版本，请先进入 Gold 页面构建训练集。</div>}
            <div className="drawer-actions">
              <button className="btn btn-primary" disabled={busy || !golds.length} onClick={train}>开始训练</button>
              <Link className="btn" to={`/task/${encodeURIComponent(taskId)}/gold`}>查看 Gold</Link>
            </div>
          </aside>
        </div>
      )}

      {drawer === "infer" && (
        <div className="drawer-backdrop" onClick={() => setDrawer("")}>
          <aside className="drawer-panel" onClick={(event) => event.stopPropagation()}>
            <div className="drawer-head">
              <div>
                <h3>批量推理</h3>
                <p>选择已登记模型、语料路径和输出目录。</p>
              </div>
              <button className="btn btn-sm" type="button" onClick={() => setDrawer("")}>关闭</button>
            </div>
            <div className="form-grid drawer-form-grid">
              <div className="field field-half">
                <label>模型</label>
                <select value={model} onChange={(event) => setModel(event.target.value)}>
                  <option value="">选择模型</option>
                  {models.map((item) => {
                    const summary = modelSummary(item);
                    return (
                      <option key={summary.key} value={summary.path}>
                        {summary.modelId}
                      </option>
                    );
                  })}
                </select>
              </div>
              <div className="field field-half">
                <label>语料路径</label>
                <input
                  value={corpus}
                  onChange={(event) => setCorpus(event.target.value)}
                  placeholder="examples/.../raw/sample.jsonl"
                />
              </div>
              <div className="field field-half">
                <label>输出目录</label>
                <input
                  value={output}
                  onChange={(event) => setOutput(event.target.value)}
                  placeholder={`runs/${taskId}/inference/model_v001`}
                />
              </div>
            </div>
            {!models.length && <div className="stage-tip">暂无可推理模型，请先训练或登记模型。</div>}
            <div className="drawer-actions">
              <button className="btn btn-primary" disabled={busy || !models.length} onClick={infer}>开始推理</button>
            </div>
          </aside>
        </div>
      )}

      {drawer === "detail" && selectedModel && selectedSummary && (
        <div className="drawer-backdrop" onClick={() => setDrawer("")}>
          <aside className="drawer-panel" onClick={(event) => event.stopPropagation()}>
            <div className="drawer-head">
              <div>
                <h3>{selectedSummary.modelId}</h3>
                <p>模型详情、训练来源、指标和批量推理入口。</p>
              </div>
              <button className="btn btn-sm" type="button" onClick={() => setDrawer("")}>关闭</button>
            </div>
            <div className="drawer-detail-grid">
              <DetailField label="模型编号" value={selectedSummary.modelId} />
              <DetailField label="状态" value={selectedSummary.status} />
              <DetailField label="训练器" value={selectedSummary.trainer} />
              <DetailField label="训练行数" value={selectedSummary.trainRows} />
              <DetailField label="测试行数" value={selectedSummary.testRows} />
              <DetailField label="指标摘要" value={selectedSummary.metricSummary} />
              <DetailField label="标签" value={selectedSummary.labels} />
              <DetailField label="外部记录" value={selectedSummary.externalRecord} />
              <DetailField label="创建时间" value={selectedSummary.createdAt} />
              <DetailField label="模型路径" value={selectedSummary.path} />
              <DetailField label="metrics" value={selectedSummary.metricsPath} />
            </div>
            <div className="drawer-actions">
              {selectedInferAction?.enabled ? (
                <button className="btn btn-primary" type="button" onClick={() => openInferDrawer(selectedInferAction.model)}>批量推理</button>
              ) : (
                <button className="btn btn-primary" type="button" disabled>{selectedInferAction?.reason || "不可推理"}</button>
              )}
              <button className="btn" type="button" onClick={openTrainDrawer}>训练新模型</button>
            </div>
            <div className="secondary-panel">
              <div className="toolbar"><h3>训练集来源</h3></div>
              <div className="resource-mini-row">
                <div>
                  <strong>{selectedTrainingGold ? `Gold ${selectedTrainingGold.version}` : "未匹配到 Gold 记录"}</strong>
                  <span>{selectedSummary.goldPath}</span>
                </div>
                <Link className="btn btn-sm" to={`/task/${encodeURIComponent(taskId)}/gold`}>查看 Gold</Link>
              </div>
            </div>
            <details className="advanced-panel">
              <summary>manifest</summary>
              <pre className="log-box">{JSON.stringify(selectedModel.manifest || {}, null, 2)}</pre>
            </details>
            <details className="advanced-panel">
              <summary>metrics</summary>
              <pre className="log-box">{JSON.stringify(selectedModel.metrics || {}, null, 2)}</pre>
            </details>
          </aside>
        </div>
      )}
    </div>
  );
}
