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

需要模型记录服务时再启用 Docker Compose 的 mlflow profile：

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

在 panel 工作流中，`profile` 是执行模板，不是备注。任务的 `task.yaml` 可以通过 `profile: {preset: manual_labeling_cv_v1}` 绑定预设流程，面板据此展开导入、抽样、Argilla 分发、结果回收、质量门槛、训练集构建、训练和推理的默认动作与参数；每个阶段都必须写 manifest，下一阶段只消费上游 manifest 中登记的产物。术语和 `manual_labeling_cv_v1` 示例见 [Profile 预设](docs/profile_presets.md)。

这个边界下，控制台不再承担正式逐行标注台职责；正式人工标注和复核都交给 Argilla。

模型训练按“控制面和计算面分离”设计。控制台服务器可以是低配机器，高性能训练服务器作为计算面接入。详细设计见 [远程训练设计](docs/remote_training_design.md)。

## 部署配置分层

平台部署时按三层管理配置和数据：

1. **R2 数据湖 / registry 是权威层**：任务列表、任务快照、源数据 manifest、任务级输入对象以及需要回写的数据湖产物，都以 R2 registry 中登记的 URI 为准。
2. **panel settings 是运行配置层**：当前部署在“系统设置”中保存 `task_registry_uri` 和 `data_lake_r2_prefix`，决定本控制台连接哪一个 R2 registry 和允许访问哪个 R2 前缀。
3. **本地 `tasks/` / `runs/` 是执行层**：`tasks/` 只缓存从 registry 同步下来的任务配置；`runs/` 保存导入、样本、标注结果、训练集、模型、推理结果和审计日志。

服务器部署后，第一步是在轻量控制台的“系统设置”填写 `task_registry_uri` 和 `data_lake_r2_prefix`，保存后同步任务配置。不要把示例 bucket 当成生产配置；同一套镜像应能连接任意符合约定的 R2 数据湖。

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
docker compose -f docker-compose.yml -f docker-compose.rclone.example.yml pull panel
docker compose -f docker-compose.yml -f docker-compose.rclone.example.yml up -d --no-build
```

启动后先访问轻量控制台，进入“系统设置”填写本部署的 `task_registry_uri` 和 `data_lake_r2_prefix`，再返回任务列表同步任务配置。R2 访问只通过 rclone 完成，`docker-compose.rclone.example.yml` 只读挂载宿主机的 `rclone.conf`，不要把密钥写进镜像或 compose 文件。

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
LLS_TASK_SOURCE=r2
LLS_TASK_REGISTRY_URI=r2:YOUR_BUCKET/governance/data_lake/v1/current/data_lake.yaml
LLS_DATA_LAKE_R2_PREFIX=r2:YOUR_BUCKET/
LLS_RCLONE_TIMEOUT_SECONDS=120
```

`YOUR_BUCKET` 是占位格式，必须替换成自己的 R2 bucket 和 registry 路径；也可以在面板“系统设置”中保存当前部署的 `task_registry_uri` 和 `data_lake_r2_prefix`。

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
- rclone，用于按任务配置读取 R2 数据湖

因此默认部署可以直接运行内置 `tfidf_sgd` 基线训练器。MLflow 客户端只提供可选记录能力；不启用 Docker Compose 的 mlflow profile 时不会启动 MLflow 服务。

容器挂载：

- `./runs:/app/runs`：保存样本、标注结果、训练集、模型和推理产物
- `./tasks:/app/tasks`：R2 任务配置的本地执行缓存
- `./configs:/app/configs:ro`：配置示例

生产面板默认使用 `LLS_TASK_SOURCE=r2`。当前部署应在“系统设置”保存 `task_registry_uri` 和 `data_lake_r2_prefix`；启动兜底值可由 `LLS_TASK_REGISTRY_URI` 和 `LLS_DATA_LAKE_R2_PREFIX` 提供。刷新或同步任务时，面板会从 registry 读取 `tasks.<任务编号>.task_uri`，把远端 `task.yaml` 同步到 `tasks/<任务编号>/task.yaml` 作为本地缓存。面板不允许新建或归档本地任务配置；任务下线应在 R2 registry 中把对应任务标记为非启用状态。`examples/` 只保留给本地开发和测试命令使用，不会在正式面板中默认显示。任务可以在 `task.yaml` 中写 `profile: {preset: manual_labeling_cv_v1}`，让面板按预设模板预填阶段参数并执行质量门槛，而不是把流程写成说明文字。

数据导入页支持上传 JSONL/NDJSON 文件，也支持粘贴数据。导入数据按不可覆盖资产管理：同一导入编号和同一内容会幂等复用，同一编号但内容不同会拒绝写入。面板支持导入详情、字段清单、ID 唯一性检查、分页查看、搜索、下载和归档；归档不会物理删除原始文件，且已被样本使用的导入数据不能归档。样本同样按不可覆盖资产管理，已被本地标注、Argilla 分发、标注结果或训练集使用时不能归档。数据操作规范见 [数据操作规范](docs/data_governance.md)。

配置了 `data_lake` 的任务可以直接从 R2 数据湖 manifest 生成本地导入。scaffold 只缓存任务级输入和标注产物，不维护上游大数据的第二份路径体系。生产面板默认不能覆盖数据湖来源，只按 `task.yaml` 中的治理登记表配置导入；`LLS_ALLOW_DATA_LAKE_OVERRIDES=1` 只用于开发排查。Docker 部署时需要叠加 `docker-compose.rclone.example.yml`，把 rclone 配置以只读方式映射到面板容器。接入规则见 [数据湖接入说明](docs/data_lake_scaffold_integration.md)。

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
