# llm-labeling-scaffold

这是一个面向文本标注实验的轻量平台。当前产品边界先按两个核心角色收口：

1. **轻量控制台**：给实验人员管理任务、导入数据、生成样本、发起标注任务、拉回标注结果、构建训练集版本、创建训练任务、查看模型产物和执行状态。
2. **Argilla**：作为正式标注工作台，用来分发标注任务、收集标注结果和支持复核。

MLflow 不再是默认依赖。它只作为可选外部模型记录服务，适合团队已经需要集中记录训练参数、指标和模型产物时再启用。默认部署只依赖本地 `runs/` 目录保存实验产物。

## 快速启动

源码部署时直接使用内置脚本：

```bash
./scripts/stack up
```

默认启动：

- 轻量控制台：`http://localhost:8765`，默认账号 `admin` / `changeme`
- Argilla：`http://localhost:6900`，默认账号 `argilla` / `12345678`
- Argilla 依赖服务：PostgreSQL、Elasticsearch、Redis

Argilla 依赖 Elasticsearch。低配测试机如果看到 Argilla 日志反复提示 Elasticsearch 不可用，先检查：

```bash
docker compose logs --tail=200 elasticsearch
docker compose ps
```

如果 Elasticsearch 日志提示 `vm.max_map_count` 过低，在服务器执行：

```bash
sudo sysctl -w vm.max_map_count=262144
echo 'vm.max_map_count=262144' | sudo tee /etc/sysctl.d/99-elasticsearch.conf
```

默认 `.env.example` 给 Elasticsearch 配置了 `-Xms256m -Xmx256m`，用于 1GB 左右内存的临时测试机。正式环境建议至少 2GB 内存，并把 `ELASTICSEARCH_JAVA_OPTS` 调到 `-Xms512m -Xmx512m` 或更高。

常用命令：

```bash
./scripts/stack logs
./scripts/stack ps
./scripts/stack restart
./scripts/stack down
```

需要模型记录服务时再启用 profile：

```bash
./scripts/stack up --mlflow
./scripts/stack logs --mlflow
```

启用后会额外启动：

- 模型记录服务：`http://localhost:5000`

脚本会在 `--mlflow` 模式下临时把 `MLFLOW_TRACKING_URI=http://mlflow:5000` 传给控制台；默认模式不会注入这个地址。

## 平台流程

推荐的实验闭环：

1. 实验人员在轻量控制台选择任务并导入数据。
2. 轻量控制台从数据中生成待标注样本。
3. 轻量控制台把样本推送到 Argilla。
4. 标注人员在 Argilla 中完成标注或复核。
5. 实验人员从轻量控制台拉回 Argilla 标注结果，生成标注结果产物。
6. 轻量控制台基于样本和标注结果产物构建训练集版本。
7. 实验人员在轻量控制台创建训练任务。
8. 高性能训练服务器读取训练任务并产出模型版本。
9. 模型产物、指标和 manifest 默认写入 `runs/`。
10. 如果启用了 MLflow，训练记录可同步到外部模型记录服务。

这个边界下，控制台不再承担正式逐行标注台职责；正式人工标注和复核都交给 Argilla。

模型训练按“控制面和计算面分离”设计。控制台服务器可以是低配机器，高性能训练服务器作为计算面接入。详细设计见 [远程训练设计](docs/remote_training_design.md)。

## 服务器测试

有两种部署方式。

### 方式一：从 GitHub Container Registry 拉取控制台镜像

GitHub Actions 会在 push 到 `main` 或 tag 时构建控制台镜像并推送到：

```text
ghcr.io/zeuyel/llm-labeling-scaffold/panel
```

`main` 分支会推送 `main` 和 `latest` 标签；tag push 会推送对应 tag。

在服务器上可以这样测试：

```bash
git clone <repo-url>
cd llm-labeling-scaffold
cp .env.example .env
export PANEL_IMAGE=ghcr.io/zeuyel/llm-labeling-scaffold/panel:main
docker compose pull panel
docker compose up -d --no-build
```

如果要同时测试可选模型记录服务：

```bash
export PANEL_IMAGE=ghcr.io/zeuyel/llm-labeling-scaffold/panel:main
export MLFLOW_TRACKING_URI=http://mlflow:5000
docker compose --profile mlflow pull panel
docker compose --profile mlflow build mlflow
docker compose --profile mlflow up -d --no-build
```

### 方式二：在服务器本地构建

```bash
git clone <repo-url>
cd llm-labeling-scaffold
./scripts/stack up
```

可选模型记录服务：

```bash
./scripts/stack up --mlflow
```

## 环境变量

默认配置在 `.env.example`：

```text
PANEL_PORT=8765
LLS_PANEL_PASSWORD=changeme

ARGILLA_PORT=6900
ARGILLA_USERNAME=argilla
ARGILLA_PASSWORD=12345678
ARGILLA_API_KEY=argilla.apikey
ARGILLA_WORKSPACE=argilla

MLFLOW_PORT=5000
```

`MLFLOW_TRACKING_URI` 默认不设置。只有需要把训练记录同步到可选模型记录服务时，才设置：

```bash
export MLFLOW_TRACKING_URI=http://mlflow:5000
```

## Docker 镜像说明

控制台镜像会把前端构建产物打进后端镜像，并安装：

- 核心流水线依赖
- Argilla 集成依赖
- 基线训练依赖：`scikit-learn`、`joblib`
- MLflow 客户端依赖

因此默认部署可以直接运行内置 `tfidf_sgd` 基线训练器。MLflow 客户端只提供可选记录能力；不启用 profile 时不会启动 MLflow 服务。

容器挂载：

- `./runs:/app/runs`：保存样本、标注结果、训练集、模型和推理产物
- `./tasks:/app/tasks`：控制台中新建的业务任务
- `./configs:/app/configs:ro`：配置示例

控制台默认只读取 `tasks/`。实验人员在控制台中新建的任务会写入 `tasks/<任务编号>/task.yaml`。如果任务已经有复杂配置，也仍然可以直接把任务目录放到 `tasks/` 下。`examples/` 只保留给本地开发和测试命令使用，不会在正式面板中默认显示。

数据导入页支持上传 JSONL/NDJSON 文件，也支持粘贴数据。导入接口会校验每一行是否为 JSON 对象；解析失败时不会静默丢行。

推送到 Argilla 时，平台会把任务配置中的 `labels.primary` 和 `labels.auxiliary` 都同步为标注问题。拉回标注结果时，这些字段会完整写入 `human_label`，再进入训练集版本构建。

## 本地命令开发

下面命令用于开发和排查，不是服务器默认部署路径：

```bash
python -m llm_labeling_scaffold.cli schema build --task examples/toy_text_classification/task.yaml
python -m llm_labeling_scaffold.cli sample --task examples/toy_text_classification/task.yaml --rows 12 --sample-id toy_seed
python -m llm_labeling_scaffold.cli batch --task examples/toy_text_classification/task.yaml --sample runs/toy_multiclass_v1/samples/toy_seed/sample.jsonl --batch-size 5
python -m llm_labeling_scaffold.cli annotate --task examples/toy_text_classification/task.yaml --provider local_stub --run-id demo --sample runs/toy_multiclass_v1/samples/toy_seed/sample.jsonl --batch-size 5 --skip-existing
python -m llm_labeling_scaffold.cli audit --task examples/toy_text_classification/task.yaml --run runs/toy_multiclass_v1/demo
python -m llm_labeling_scaffold.cli merge --task examples/toy_text_classification/task.yaml --run runs/toy_multiclass_v1/demo
python -m llm_labeling_scaffold.cli gold build --task examples/toy_text_classification/task.yaml --run runs/toy_multiclass_v1/demo --version v001
python -m llm_labeling_scaffold.cli train --task examples/toy_text_classification/task.yaml --gold runs/toy_multiclass_v1/gold/gold_v001.jsonl --model-id baseline_v001 --trainer tfidf_sgd
python -m llm_labeling_scaffold.cli infer --task examples/toy_text_classification/task.yaml --model runs/toy_multiclass_v1/models/baseline_v001/model.joblib --corpus examples/toy_text_classification/raw/sample.jsonl --output runs/toy_multiclass_v1/inference/baseline_v001
```

本地 Python 开发安装：

```bash
pip install -e ".[baseline,argilla]"
```

如果本地也要调试可选模型记录服务：

```bash
pip install -e ".[baseline,argilla,mlflow]"
export MLFLOW_TRACKING_URI=http://localhost:5000
```

前端本地开发：

```bash
cd frontend
npm install
npm run dev
npm run build
```

`5173` 端口只用于前端开发模式。Docker 默认通过 panel 的 `8765` 端口同时提供 API 和前端页面。

## 当前能力

已经具备：

- 任务 YAML 加载
- JSON Schema 生成
- 数据采样和批处理
- 本地 stub 标注 provider
- schema、ID、约束检查
- missing、duplicate、conflict pool 生成
- 标注结果产物到训练集版本的构建
- 版本化训练集
- 本地 `tfidf_sgd` 基线训练调试
- 远程训练任务设计
- JSONL 全量推理
- 本地清单、指标和摘要产物
- Argilla push / pull 集成
- 控制台新建任务和上传数据文件
- Argilla 完整标签字段同步
- 可选 MLflow 训练记录

后续应优先继续收口产物契约，而不是在控制台中重做正式标注界面。
