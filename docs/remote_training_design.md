# 远程训练设计

模型训练不是控制台服务器的附属能力，而是平台的一等计算面能力。控制台服务器只承担任务管理、标注分发、产物登记和状态展示；高性能训练服务器承担模型训练和推理计算。

## 角色边界

1. 控制台服务器
   - 管理任务、数据导入、样本、标注分发和标注结果。
   - 构建训练集版本。
   - 创建训练任务，登记训练状态和模型版本。
   - 保存轻量产物清单，不直接承担大模型训练。

2. 训练服务器
   - 按训练任务读取训练集版本和任务配置。
   - 使用指定训练器和参数训练模型。
   - 写出模型文件、指标、训练日志和模型清单。
   - 将模型版本回传到控制台服务器的 `runs/` 目录或共享对象存储。

3. 外部模型记录服务
   - 可选。
   - 只负责集中记录训练参数、指标和模型产物索引。
   - 不是平台闭环的默认依赖。

## 核心原则

- 控制面和计算面分离：控制台可以运行在低配机器上，训练任务默认不占用控制台机器资源。
- 文件产物是主事实源：训练服务器和控制台通过标准目录、清单和状态文件交接。
- 训练任务可复现：每次训练任务必须记录训练集版本、任务配置快照、训练器、参数、镜像、代码版本和资源需求。
- 训练结果可回传：模型版本目录必须能独立被控制台读取，不依赖训练服务器本地状态。

## 目录约定

```text
runs/<任务编号>/
  training_jobs/<训练任务编号>/
    request.json
    status.json
    logs.txt
    result.json
  models/<模型编号>/
    manifest.json
    metrics.json
    model.joblib
    summary.md
```

`request.json` 是控制台创建的训练任务请求：

```json
{
  "task_id": "toy_multiclass_v1",
  "training_job_id": "train_20260625_001",
  "model_id": "baseline_v001",
  "gold_version": "v001",
  "gold_path": "runs/toy_multiclass_v1/gold/gold_v001.jsonl",
  "task_path": "examples/toy_text_classification/task.yaml",
  "trainer": "tfidf_sgd",
  "trainer_params": {},
  "image": "ghcr.io/zeuyel/llm-labeling-scaffold/panel:main",
  "resources": {
    "cpu": 4,
    "memory_gb": 16,
    "gpu": 0
  },
  "created_at": "2026-06-25T00:00:00Z",
  "code_ref": "945a263"
}
```

`status.json` 记录训练状态：

```json
{
  "status": "queued",
  "updated_at": "2026-06-25T00:00:00Z",
  "worker": null,
  "message": ""
}
```

状态只使用以下枚举：

```text
queued
claimed
running
succeeded
failed
canceled
```

`result.json` 是训练服务器回传的训练结果索引：

```json
{
  "status": "succeeded",
  "model_id": "baseline_v001",
  "model_dir": "runs/toy_multiclass_v1/models/baseline_v001",
  "metrics_path": "runs/toy_multiclass_v1/models/baseline_v001/metrics.json",
  "manifest_path": "runs/toy_multiclass_v1/models/baseline_v001/manifest.json",
  "finished_at": "2026-06-25T00:10:00Z"
}
```

## 第一阶段：任务包模式

第一阶段不要求控制台直连训练服务器。控制台生成训练任务包，实验人员或自动脚本把任务包同步到租用的训练服务器，训练服务器执行后把模型目录同步回控制台服务器。

推荐流程：

1. 控制台创建训练任务，写出 `training_jobs/<训练任务编号>/request.json`。
2. 同步以下内容到训练服务器：
   - `examples/`
   - 对应任务的 `gold/`
   - `training_jobs/<训练任务编号>/request.json`
3. 训练服务器用同一个镜像执行训练命令。
4. 训练服务器写出 `models/<模型编号>/` 和 `training_jobs/<训练任务编号>/result.json`。
5. 将模型目录和结果文件同步回控制台服务器。
6. 控制台刷新后读取模型版本。

训练服务器示例命令：

```bash
docker run --rm \
  -v "$PWD/runs:/app/runs" \
  -v "$PWD/examples:/app/examples:ro" \
  ghcr.io/zeuyel/llm-labeling-scaffold/panel:main \
  python -m llm_labeling_scaffold.cli train \
    --task examples/toy_text_classification/task.yaml \
    --gold runs/toy_multiclass_v1/gold/gold_v001.jsonl \
    --model-id baseline_v001 \
    --trainer tfidf_sgd
```

## 第二阶段：拉取式训练 worker

第二阶段增加训练 worker。训练服务器主动拉取训练任务，不要求控制台保存训练服务器 SSH 凭据。

设计方向：

1. 控制台提供训练任务队列接口。
2. 训练服务器启动 worker，并持有访问令牌。
3. worker 轮询 `queued` 任务，声明任务后状态变为 `claimed`。
4. worker 下载或同步任务所需产物，执行训练。
5. worker 上传模型目录和结果文件。
6. 控制台展示训练状态和模型版本。

这个方向适合租用机器频繁变化的场景。训练服务器只需要拿到一次性令牌和镜像地址即可接入。

## 第三阶段：调度器

第三阶段再考虑资源调度。只有当训练任务数量和并发需求上来后，才需要接入 Kubernetes、批处理队列或云厂商训练服务。

调度器只负责把训练任务分配给计算资源，不改变平台的产物契约。无论后端是手动任务包、worker、Kubernetes 还是云训练服务，最终都必须写回同样的 `models/<模型编号>/` 和训练任务结果文件。

## 控制台展示

控制台的模型管理页应分成两类：

1. 训练任务
   - 训练任务编号
   - 训练集版本
   - 模型编号
   - 训练器
   - 状态
   - 训练服务器
   - 创建时间和完成时间

2. 模型版本
   - 模型编号
   - 来源训练任务
   - 指标摘要
   - 模型目录
   - 可选外部记录地址

本地训练只作为调试能力保留，不作为正式训练默认路径。
