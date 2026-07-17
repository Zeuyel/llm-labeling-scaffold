# llm-labeling-scaffold

这是一个面向文本标注实验的轻量平台。当前产品边界先按两个核心角色收口：

1. **轻量控制台**：给实验人员管理任务、导入数据、生成样本、发起标注任务、拉回标注结果、构建训练集版本、创建训练任务、查看模型产物和执行状态。
2. **Argilla**：作为正式标注工作台，用来分发标注任务、收集标注结果和支持复核。

MLflow 不再是默认依赖。它只作为可选外部模型记录服务，适合团队已经需要集中记录训练参数、指标和模型产物时再启用。默认部署只依赖本地 `runs/` 目录保存实验产物。

## 快速启动

源码部署时直接使用内置脚本：

```bash
cp .env.example .env
owner_password="$(openssl rand -hex 32)"
app_password="$(openssl rand -hex 32)"
sed -i "s|^SCAFFOLD_POSTGRES_OWNER_PASSWORD=.*|SCAFFOLD_POSTGRES_OWNER_PASSWORD=${owner_password}|" .env
sed -i "s|^SCAFFOLD_POSTGRES_APP_PASSWORD=.*|SCAFFOLD_POSTGRES_APP_PASSWORD=${app_password}|" .env
unset owner_password app_password
./scripts/stack up
```

Compose 对 owner/app 两个数据库密码使用必填校验，任一缺失时直接退出。`migrate` 使用 schema owner，Panel 只使用受限 runtime app role；两个账号和密码必须不同。正式部署应由 secret manager 注入独立随机密码，不使用仓库默认凭据。

默认启动：

- 轻量控制台：`http://localhost:8765`；本地 `.env.example` 显式启用 `basic_dev`，账号 `admin` / `changeme`
- Argilla：`http://localhost:6900`，默认账号 `argilla` / `12345678`
- Scaffold 自有 PostgreSQL：默认仅绑定 `127.0.0.1:5433`
- 一次性 Alembic 迁移服务：数据库健康后执行并正常退出
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

首次部署必须显式创建首位管理员，身份键使用认证提供方的稳定 `issuer` 和 `subject`，不能使用邮箱代替：

```bash
docker compose run --rm migrate python -m llm_labeling_scaffold.cli db bootstrap \
  --issuer https://example.cloudflareaccess.com \
  --subject '<stable-access-subject>' \
  --workspace-slug default \
  --workspace-name 'Default Workspace'
```

数据库 schema、角色矩阵、迁移以及备份恢复说明见 [Scaffold 数据库与 RBAC](docs/database.md)。

需要模型记录服务时再启用 Docker Compose 的 mlflow profile：

```bash
./scripts/stack up --mlflow
./scripts/stack logs --mlflow
```

启用后会额外启动：

- 模型记录服务：`http://localhost:5000`

脚本会在 `--mlflow` 模式下临时把 `MLFLOW_TRACKING_URI=http://mlflow:5000` 传给控制台；默认模式不会注入这个地址。

需要让 Codex 或其他 MCP client 调用平台时，先在 `.env` 设置外部 bearer token 与内部服务 token，再启用 MCP profile：

```bash
./scripts/stack up --mcp
```

MCP endpoint 为 `http://localhost:8766/mcp`。Docker 默认只绑定本机回环地址，应通过 HTTPS 反向代理对外暴露。MCP 使用受限 Panel 服务身份，不持有 Panel 管理员密码，也不直接操作 R2 或本地数据目录。默认只注册只读工具；确需创建草稿、发布任务或提交数据湖导入时，才在受控部署中设置 `LLS_MCP_ENABLE_WRITES=1`。首版不是多用户 OAuth/RBAC，完整边界见 [MCP 接入说明](docs/mcp_integration.md)。

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

## 任务来源和部署配置

平台按 `LLS_TASK_SOURCE` 区分任务单权威来源，再单独配置 R2 数据湖：

1. **`r2`**：R2 registry 管理任务列表和远端 `task.yaml`。`task_registry_uri` 指向数据湖治理登记表，通常是 `governance/data_lake/v1/current/data_lake.yaml`；登记表中的 `tasks.<task_id>.task_uri` 才指向具体 `task.yaml`。
2. **`control`**：scaffold 控制面管理任务单、草稿和发布 revision。R2 不再作为任务单来源，只提供任务配置中声明的数据湖输入和产物读写。
3. **`local`**：直接读取本地 `tasks/`，仅适合开发或测试。
4. **panel settings 和本地执行目录**：`task_registry_uri` 与 `data_lake_r2_prefix` 配置 R2 数据湖连接和允许前缀；`runs/` 保存运行产物，`tasks/` 保存 `r2` 模式的同步缓存或 `control` 模式当前已发布的可执行任务配置。

`r2` 模式首次部署后，应在“系统设置”填写 `task_registry_uri` 和 `data_lake_r2_prefix`，再同步任务配置。`control` 模式不从 R2 同步任务；其中的 R2 配置只在任务访问数据湖时使用。不要把示例 bucket 当成生产配置；同一套镜像应能连接任意符合约定的 R2 数据湖。

## 控制面任务生命周期

`LLS_TASK_SOURCE=control` 下，任务单先以 draft 保存。创建 draft 时 revision 为 `0`，草稿的最新内容保存在 `runs/_system/task_control/registry.json`，此时不会生成可执行的 `task.yaml`。修改已发布任务会保留当前已发布 revision，并把状态改为 `published_with_draft`。

发布会校验 draft，生成递增的 revision，并把当前可执行配置写到 `tasks/<task_id>/task.yaml`；如果配置包含 prompt，同时写入同目录的 `prompt.revision_<六位编号>.md`。每次发布还会保留不可覆盖快照：

```text
runs/_system/task_control/task_snapshots/<task_id>/revision_<六位编号>/task.yaml
runs/_system/task_control/task_snapshots/<task_id>/revision_<六位编号>/prompt.md
runs/_system/task_control/task_snapshots/<task_id>/revision_<六位编号>/draft_spec.json
```

`registry.json` 保留 draft、当前发布信息和 revision 历史；流水线只使用当前已发布的 `tasks/<task_id>/task.yaml`。因此 draft 不会直接改变正在执行的已发布任务。

## 认证边界

生产 Panel 使用 Cloudflare Access 注入的 `Cf-Access-Jwt-Assertion`，源站验证 RS256 签名、JWKS、issuer、显式 application AUD、时间声明和 `type=app`。稳定用户身份使用 `(issuer, subject)`，邮箱与显示名只作为显示快照；客户端自报 actor 或邮箱头不会建立身份。

Scaffold 已有独立的用户、工作空间、任务 ACL 和 RBAC 持久化层，但尚未接入 Panel 业务授权路径。#46 完成接线和资源迁移前，Access 用户只能读取 `/api/session` 等系统认证态端点，所有业务 API 都会以 `503 authorization_unavailable` fail closed，不能因为“已登录”而获得原有管理员能力。现有 Basic Auth 只在显式 `LLS_PANEL_AUTH_MODE=basic_dev` 时用于本地开发；生产默认 `cloudflare_access`，配置或验证失败不会回退到 Basic。完整配置与 Tunnel-only 源站要求见 [Cloudflare Access 身份验证](docs/cloudflare_access.md)。

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
owner_password="$(openssl rand -hex 32)"
app_password="$(openssl rand -hex 32)"
sed -i "s|^SCAFFOLD_POSTGRES_OWNER_PASSWORD=.*|SCAFFOLD_POSTGRES_OWNER_PASSWORD=${owner_password}|" .env
sed -i "s|^SCAFFOLD_POSTGRES_APP_PASSWORD=.*|SCAFFOLD_POSTGRES_APP_PASSWORD=${app_password}|" .env
unset owner_password app_password
export PANEL_IMAGE=ghcr.io/zeuyel/llm-labeling-scaffold/panel:main
docker compose -f docker-compose.yml -f docker-compose.rclone.example.yml pull panel
docker compose -f docker-compose.yml -f docker-compose.rclone.example.yml up -d --no-build
```

`r2` 模式启动后，进入“系统设置”填写本部署的 `task_registry_uri` 和 `data_lake_r2_prefix`，再返回任务列表同步任务配置。R2 访问只通过 rclone 完成，`docker-compose.rclone.example.yml` 只读挂载宿主机的 `rclone.conf`，不要把密钥写进镜像或 compose 文件。只要任务需要 R2 数据湖，compose 启动都必须包含 rclone override 或等价 secret 挂载。

如果要同时测试可选模型记录服务：

```bash
export PANEL_IMAGE=ghcr.io/zeuyel/llm-labeling-scaffold/panel:main
export MLFLOW_TRACKING_URI=http://mlflow:5000
docker compose -f docker-compose.yml -f docker-compose.rclone.example.yml --profile mlflow pull panel
docker compose -f docker-compose.yml -f docker-compose.rclone.example.yml --profile mlflow build mlflow
docker compose -f docker-compose.yml -f docker-compose.rclone.example.yml --profile mlflow up -d --no-build
```

### 方式二：在服务器本地构建

```bash
git clone <repo-url>
cd llm-labeling-scaffold
cp .env.example .env
owner_password="$(openssl rand -hex 32)"
app_password="$(openssl rand -hex 32)"
sed -i "s|^SCAFFOLD_POSTGRES_OWNER_PASSWORD=.*|SCAFFOLD_POSTGRES_OWNER_PASSWORD=${owner_password}|" .env
sed -i "s|^SCAFFOLD_POSTGRES_APP_PASSWORD=.*|SCAFFOLD_POSTGRES_APP_PASSWORD=${app_password}|" .env
unset owner_password app_password
./scripts/stack up
```

可选模型记录服务：

```bash
./scripts/stack up --mlflow
```

### 启用控制面任务来源

默认 compose 使用 `LLS_TASK_SOURCE=control`。如部署文件覆盖了该变量，请在 `.env` 中明确设置：

```text
LLS_TASK_SOURCE=control
```

控制面模式不会从 R2 registry 同步任务。草稿和 revision 会写入持久化的 `./runs` 挂载，发布后才写入 `./tasks` 挂载供流水线执行。控制面任务仍可使用 R2 数据湖；此时保留 `LLS_TASK_REGISTRY_URI`、`LLS_DATA_LAKE_R2_PREFIX` 和只读 rclone 配置，但它们不决定任务单列表。

### SaaS smoke 验收

真实部署启动后，可以在服务器本地运行 scaffold smoke runner。不要把真实 URL、token、Basic Auth 密码、rclone 配置路径或 secret 路径写入仓库文件；使用 shell 环境变量或服务器 secret manager 注入。

Basic Auth 示例（仅 `basic_dev` 本地开发或迁移）：

```bash
export LLS_SMOKE_SERVER_URL=http://127.0.0.1:8765
export LLS_SMOKE_BASIC_USER=admin
export LLS_SMOKE_BASIC_PASSWORD='<read-from-local-secret-manager>'
PYTHONPATH=src python3 -m llm_labeling_scaffold.cli smoke --format markdown
```

Bearer token 示例：

```bash
export LLS_SMOKE_SERVER_URL='<deployment-url>'
export LLS_SMOKE_TOKEN='<read-from-local-secret-manager>'
PYTHONPATH=src python3 -m llm_labeling_scaffold.cli smoke --format json
```

runner 默认检查：

- discovery：`/api/health`、`/api/version`、`/api/capabilities`、`/api/settings/public`
- task check：`patent_boundary_v0_1`
- import dry-run：`patent_boundary_manual_seed_500_2026_06_27`

任务和导入编号可以按部署覆盖：

```bash
PYTHONPATH=src python3 -m llm_labeling_scaffold.cli smoke \
  --task-id patent_boundary_v0_1 \
  --import-id patent_boundary_manual_seed_500_2026_06_27 \
  --format markdown
```

import dry-run 只会在 `/api/capabilities` 声明了 side-effect-free dry-run contract 时发送请求；当前服务若只暴露会启动真实导入的 `POST /api/import/data_lake`，runner 会把该项标记为 `not_supported`，不会伪造通过。输出摘要会对 token、rclone config、Argilla key、数据库密码和 secret path 做脱敏；任何 `not_supported`、`missing` 或失败项都会让命令返回非零退出码。

## 环境变量

默认配置在 `.env.example`：

```text
PANEL_PORT=8765
PANEL_BIND_HOST=127.0.0.1
LLS_PANEL_AUTH_MODE=basic_dev
LLS_PANEL_PASSWORD=changeme

# 生产改为 cloudflare_access，并配置：
# LLS_CF_ACCESS_ISSUER=https://YOUR_TEAM.cloudflareaccess.com
# LLS_CF_ACCESS_AUD=<Access application Audience Tag>

SCAFFOLD_POSTGRES_BIND_HOST=127.0.0.1
SCAFFOLD_POSTGRES_PORT=5433
SCAFFOLD_POSTGRES_OWNER_USER=scaffold_owner
SCAFFOLD_POSTGRES_OWNER_PASSWORD=
SCAFFOLD_POSTGRES_APP_USER=scaffold_app
SCAFFOLD_POSTGRES_APP_PASSWORD=
SCAFFOLD_POSTGRES_DB=scaffold

ARGILLA_PORT=6900
ARGILLA_USERNAME=argilla
ARGILLA_PASSWORD=12345678
ARGILLA_API_KEY=argilla.apikey
ARGILLA_WORKSPACE=argilla

MLFLOW_PORT=5000
MCP_PORT=8766
MCP_BIND_HOST=127.0.0.1
LLS_MCP_BEARER_TOKEN=<由服务器 secret manager 生成的随机值>
LLS_MCP_INTERNAL_TOKEN=<另一条由服务器 secret manager 生成的随机值>
LLS_MCP_ENABLE_WRITES=0
LLS_MCP_TIMEOUT_SECONDS=15
LLS_TASK_SOURCE=control
LLS_TASK_REGISTRY_URI=r2:YOUR_BUCKET/governance/data_lake/v1/current/data_lake.yaml
LLS_DATA_LAKE_R2_PREFIX=r2:YOUR_BUCKET/
LLS_RCLONE_TIMEOUT_SECONDS=120
```

`.env.example` 的 `basic_dev` 只服务于本地快速启动。生产部署必须设置 `LLS_PANEL_AUTH_MODE=cloudflare_access`，保持源站回环/私网绑定，并通过 Cloudflare Tunnel 暴露 Access 应用；不得开放 Panel 公网端口让请求绕过 Access。

`LLS_TASK_SOURCE` 可设为 `r2`、`control` 或 `local`。`YOUR_BUCKET` 是占位格式，必须替换成自己的 R2 bucket 和 registry 路径；也可以在面板“系统设置”中保存当前部署的 `task_registry_uri` 和 `data_lake_r2_prefix`。在 `r2` 模式中，`LLS_TASK_REGISTRY_URI` 对应 `task_registry_uri`，应指向数据湖治理登记表，通常是 `data_lake.yaml`；具体任务文件由登记表的 `tasks.<task_id>.task_uri` 指向。在 `control` 模式中，它只作为数据湖 registry 的默认配置，不参与任务单同步。

`MLFLOW_TRACKING_URI` 默认不设置。只有需要把训练记录同步到可选模型记录服务时，才设置：

```bash
export MLFLOW_TRACKING_URI=http://mlflow:5000
```

## Docker 镜像说明

控制台镜像会把前端构建产物打进后端镜像，并安装：

- 核心流水线依赖
- SQLAlchemy 2、Alembic 和 PostgreSQL 驱动
- Argilla 集成依赖
- 基线训练依赖：`scikit-learn`、`joblib`
- MLflow 客户端依赖
- MCP Streamable HTTP 服务依赖
- rclone，用于按任务配置读取 R2 数据湖

因此默认部署可以直接运行内置 `tfidf_sgd` 基线训练器。MLflow 客户端只提供可选记录能力；不启用 Docker Compose 的 mlflow profile 时不会启动 MLflow 服务。

容器挂载：

- `./runs:/app/runs`：保存样本、标注结果、训练集、模型、推理产物，以及控制面的 registry 和 revision 快照
- `./tasks:/app/tasks`：`r2` 模式的任务配置缓存，或 `control` 模式当前已发布的可执行任务配置
- `./configs:/app/configs:ro`：配置示例

生产面板默认使用 `LLS_TASK_SOURCE=control`。任务单由控制面创建、编辑和发布，当前已发布 revision 写入 `tasks/<任务编号>/task.yaml` 供流水线执行；`runs/` 保留草稿和 revision 快照。启动兜底值可由 `LLS_TASK_REGISTRY_URI` 和 `LLS_DATA_LAKE_R2_PREFIX` 提供，它们只配置 R2 数据湖。需要兼容既有上游任务登记表时，可显式设置 `LLS_TASK_SOURCE=r2`；该模式从登记表读取 `tasks.<任务编号>.task_uri` 并缓存远端 `task.yaml`。`examples/` 只保留给本地开发和测试命令使用，不会在正式面板中默认显示。任务可以在 `task.yaml` 中写 `profile: {preset: manual_labeling_cv_v1}`，让面板按预设模板预填阶段参数并执行质量门槛，而不是把流程写成说明文字。

无论任务来源，只要任务配置了 `data_lake`，R2 数据湖就是任务输入的权威来源；面板只把登记表和 manifest 指定的任务级 JSONL materialize 到本地 `runs/<task_id>/imports/`。手动上传文件和粘贴导入默认关闭，只能在本地开发或测试模式下开启。导入数据按不可覆盖资产管理：同一导入编号和同一内容会幂等复用，同一编号但内容不同会拒绝写入。面板支持导入详情、字段清单、ID 唯一性检查、分页查看、搜索、下载和归档；归档不会物理删除原始文件，且已被样本使用的导入数据不能归档。样本同样按不可覆盖资产管理，已被本地标注、Argilla 分发、标注结果或训练集使用时不能归档。数据操作规范见 [数据操作规范](docs/data_governance.md)。

配置了 `data_lake` 的任务可以从 R2 数据湖 manifest 生成本地导入。R2 导入是下载、校验和原子提交过程，应作为异步 job 执行；页面通过 job 状态反馈排队、运行、成功或失败。导入成功后，profile 的下一步是从该导入中抽取样本。scaffold 只缓存任务级输入和标注产物，不维护上游大数据的第二份路径体系。生产面板默认不能覆盖数据湖来源，只按 `task.yaml` 中的治理登记表配置导入；`LLS_ALLOW_DATA_LAKE_OVERRIDES=1` 只用于开发排查。Docker 部署时需要叠加 `docker-compose.rclone.example.yml`，把 rclone 配置以只读方式映射到面板容器。接入规则见 [数据湖接入说明](docs/data_lake_scaffold_integration.md)。

推送到 Argilla 时，平台会把任务配置中的 `labels.primary` 和 `labels.auxiliary` 都同步为标注问题，并在 annotation manifest 中记录 server/SDK 版本、workspace/dataset UUID、settings schema 与 task/sample/batch/plan fingerprint。拉回时通过 Argilla 2.8 的分页 iterator 单遍扫描，只接受问题完整、用户属于目标 workspace、role 精确为 `annotator` 的 `submitted` response，并保留回答者 UUID、用户名、角色和 workspace UUID。draft/discarded 只计为 skipped；mixed/unknown status、重复问题、缺少 required 问题、无效或未知用户以及 owner/admin/未知 role 会写入 quarantine。accepted 与 quarantine 先写入同一不可变 generation，稳定路径只是兼容快照；原子 commit marker 是唯一发布点，内部 loader 会校验两份 generation 文件及哈希后读取，失败更新继续使用上一 committed generation。

相同 contract 的恢复只补写远端缺失的 record ID，不重传已经存在的记录。完整重复重试不会产生 record 写请求。创建 dataset 时会在 live settings 中写入对 annotator 不可见的 intent fingerprint；如果进程在 `dataset.create()` 后、首批 record 写入前中断，下一次请求只有在 workspace/name、settings、`min_submitted` 和 intent 全部精确匹配时才自动续传。任一项不匹配都会拒绝按同名 dataset 恢复。`if_exists=replace` 不会删除任何既有 dataset：资源不存在时可创建，相同 contract 时按 resume 处理，其他情况必须使用新 dataset name。Argilla `api_url` 不允许 URL userinfo 或敏感 query/fragment，运行凭据只能通过环境变量或 secret 注入。

标注者账号、稳定个人 workspace 和 membership 的 owner-side 幂等 provisioning 规则见 [Argilla owner-side provisioning](docs/argilla_admin.md)。一次性创建密码只能走该内存内 adapter，不能进入通用 Job/action params。

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

本地调试 MCP：

```bash
pip install -e ".[mcp]"
LLS_MCP_PANEL_URL=http://127.0.0.1:8765 \
LLS_MCP_INTERNAL_TOKEN='<从本机 secret manager 读取的内部服务凭据>' \
lls mcp --transport stdio
```

stdio 默认同样只读。调试写工具时需同时为 Panel 和 MCP 进程设置 `LLS_MCP_ENABLE_WRITES=1`。

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
- 控制台本地开发模式的新建任务和上传数据文件
- Argilla 完整标签字段同步
- 可选 MLflow 训练记录

后续应优先继续收口产物契约，而不是在控制台中重做正式标注界面。
