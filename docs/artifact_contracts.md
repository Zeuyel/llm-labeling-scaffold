# 产物契约

平台只保留一条主链路：实验人员在控制台管理数据和模型，标注人员在 Argilla 完成标注。

## 核心对象

1. 数据导入（`import`）：任务级输入语料。生产模式下由 R2 数据湖 materialize 到本地；手动上传或粘贴只用于本地开发和测试。
2. 样本（`sample`）：从任务数据中抽取出来、准备分发标注的数据。
3. 标注分发记录（`annotation_job`）：一次推送到 Argilla 的标注任务记录。
4. 标注结果产物（`decision_artifact`）：从 Argilla 拉回的人工标注结果。
5. 训练集版本（`gold_version`）：由样本和标注结果构建出的训练数据版本。
6. 训练任务（`training_job`）：控制台创建、由训练服务器执行的模型训练请求。
7. 模型版本（`model_version`）：由训练集训练出的模型版本。
8. 推理记录（`inference_run`）：模型对语料执行批量推理后的结果。

## 目录约定

```text
runs/<任务编号>/
  imports/<导入编号>/raw.jsonl
  samples/<样本编号>/sample.jsonl
  annotation_jobs/<标注任务编号>/manifest.json
  decisions/<标注结果编号>/decisions.jsonl
  decisions/<标注结果编号>/manifest.json
  gold/gold_<版本>.jsonl
  gold/gold_<版本>.manifest.json
  training_jobs/<训练任务编号>/request.json
  training_jobs/<训练任务编号>/status.json
  training_jobs/<训练任务编号>/result.json
  models/<模型编号>/manifest.json
  inference/<推理编号>/predictions.jsonl
```

不同层级不能互相覆盖。每个 `manifest.json` 至少要记录任务编号、来源、输入路径、输出路径、行数和创建时间，保证之后可以追溯训练集与模型来源。

训练任务的完整契约见 [远程训练设计](remote_training_design.md)。
