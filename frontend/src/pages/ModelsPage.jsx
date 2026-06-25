import React, { useCallback, useEffect, useState } from "react";
import * as api from "./../api.js";
import { Link } from "./../router.jsx";

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

  const reload = useCallback(async () => {
    if (!taskId) return;
    try {
      const [modelData, goldData] = await Promise.all([
        api.getTaskModels(taskId),
        api.getTaskGoldVersions(taskId),
      ]);
      setModels(modelData.models || []);
      setGolds(goldData.gold_versions || []);
    } catch (error) {
      onError(String(error));
    }
  }, [taskId, onError]);

  useEffect(() => {
    reload();
  }, [reload]);

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

      <div className="card" style={{ marginBottom: 16 }}>
        <h3>训练模型</h3>
        <div className="form-grid">
          <div className="field">
            <label>训练集版本</label>
            <select value={gold} onChange={(event) => setGold(event.target.value)}>
              <option value="">选择训练集版本</option>
              {golds.map((item) => (
                <option key={item.version} value={item.path || `runs/${taskId}/gold/gold_${item.version}.jsonl`}>
                  {item.version} · {item.rows ?? "-"} 行
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label>模型编号</label>
            <input
              value={modelId}
              onChange={(event) => setModelId(event.target.value)}
              placeholder="例如 bert_v001"
            />
          </div>
          <div className="field">
            <label>训练器标识</label>
            <input
              value={trainer}
              onChange={(event) => setTrainer(event.target.value)}
              placeholder="tfidf_sgd 或 package.module:function"
            />
            <span className="hint">内置 tfidf_sgd 是轻量基线；自定义训练器可填模块函数路径</span>
          </div>
          <div className="field">
            <label>训练参数 JSON</label>
            <textarea
              rows={4}
              value={trainerParams}
              onChange={(event) => setTrainerParams(event.target.value)}
              placeholder='{"base_model":"bert-base-chinese","epochs":3,"learning_rate":2e-5}'
            />
          </div>
          <div className="field">
            <label>外部模型记录服务（可选）</label>
            <select value={useMlflow ? "yes" : "no"} onChange={(event) => setUseMlflow(event.target.value === "yes")}>
              <option value="no">仅本地文件登记</option>
              <option value="yes">同步到外部记录服务</option>
            </select>
          </div>
          <div className="field">
            <label>外部实验名称</label>
            <input
              value={mlflowExperiment}
              onChange={(event) => setMlflowExperiment(event.target.value)}
              placeholder={taskId}
              disabled={!useMlflow}
            />
          </div>
        </div>
        <button className="btn btn-primary" disabled={busy} onClick={train}>
          开始训练
        </button>
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <h3>模型推理</h3>
        <div className="form-grid">
          <div className="field">
            <label>模型</label>
            <select value={model} onChange={(event) => setModel(event.target.value)}>
              <option value="">选择模型</option>
              {models.map((item) => (
                <option key={item.model_id} value={item.path}>
                  {item.model_id}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label>语料路径</label>
            <input
              value={corpus}
              onChange={(event) => setCorpus(event.target.value)}
              placeholder="examples/.../raw/sample.jsonl"
            />
          </div>
          <div className="field">
            <label>输出目录</label>
            <input
              value={output}
              onChange={(event) => setOutput(event.target.value)}
              placeholder={`runs/${taskId}/inference/model_v001`}
            />
          </div>
        </div>
        <button className="btn" disabled={busy} onClick={infer}>
          开始推理
        </button>
      </div>

      <div className="card">
        <div className="toolbar">
          <h3>本地模型登记（{models.length}）</h3>
          <button className="btn btn-sm" onClick={reload}>
            刷新
          </button>
        </div>
        {!models.length && <div className="empty">暂无模型</div>}
        {models.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>模型编号</th>
                  <th>训练器标识</th>
                  <th>测试行数</th>
                  <th>外部记录</th>
                  <th>标签</th>
                  <th>路径</th>
                </tr>
              </thead>
              <tbody>
                {models.map((item) => (
                  <tr key={item.model_id}>
                    <td>
                      <span className="badge badge-blue">{item.model_id}</span>
                    </td>
                    <td>{item.metrics ? item.metrics.trainer : item.manifest ? item.manifest.trainer : "-"}</td>
                    <td>{item.metrics ? item.metrics.test_rows : "-"}</td>
                    <td>{item.manifest && item.manifest.mlflow ? item.manifest.mlflow.run_id : "仅本地"}</td>
                    <td className="muted">{item.metrics ? (item.metrics.labels || []).join(", ") : "-"}</td>
                    <td className="muted path-cell">{item.path}</td>
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
