# 数据湖接入说明

平台部署按三层理解：

1. **R2 数据湖 / registry 是权威层**：任务列表、远端 `task.yaml`、源数据集 manifest、任务级输入对象和需要回写的数据湖产物，都以 R2 registry 中登记的 URI 为准。`task_registry_uri` 指向数据湖治理登记表，通常是 `governance/data_lake/v1/current/data_lake.yaml`；登记表里的 `tasks.<task_id>.task_uri` 才指向具体 `task.yaml`。
2. **panel settings 是运行配置层**：每个部署在“系统设置”中保存当前要使用的 `task_registry_uri` 和 `data_lake_r2_prefix`。同一套镜像可以连接不同团队、不同环境或不同 bucket。
3. **本地 `tasks/` / `runs/` 是执行层**：`tasks/` 只缓存从 registry 同步下来的任务配置；`runs/` 保存本次部署产生的导入、样本、标注结果、训练集、模型、推理结果和审计日志。

不要把某个单一项目、业务域或固定 R2 bucket 写死到部署说明中。生产环境必须按自己的 R2 registry 填写配置。

## 首次部署

服务器部署后第一步不是新建本地任务，而是进入轻量控制台的“系统设置”：

1. 填写 `task_registry_uri`，例如 `r2:labeling-lake/governance/data_lake/v1/current/data_lake.yaml`。它是数据湖治理登记表地址，不是某个任务的 `task.yaml`。
2. 填写 `data_lake_r2_prefix`，例如 `r2:labeling-lake/`。
3. 保存设置。
4. 返回任务列表并同步任务配置，让面板从 registry 读取启用任务，把远端 `task.yaml` 缓存到本地 `tasks/<task_id>/task.yaml`。

`.env` 中的 `LLS_TASK_REGISTRY_URI` 和 `LLS_DATA_LAKE_R2_PREFIX` 是启动兜底配置，适合无人值守部署或系统设置初始化前使用。`LLS_TASK_REGISTRY_URI` 对应面板里的 `task_registry_uri`，应指向数据湖治理登记表，一般是 `data_lake.yaml`；具体 `task.yaml` 由登记表的 `tasks.<task_id>.task_uri` 指向。它们必须与当前部署自己的 R2 registry 保持一致，不要直接使用示例 bucket。

## 任务配置来源

生产环境使用 R2 registry 作为任务来源：

```text
LLS_TASK_SOURCE=r2
LLS_TASK_REGISTRY_URI=r2:labeling-lake/governance/data_lake/v1/current/data_lake.yaml
LLS_DATA_LAKE_R2_PREFIX=r2:labeling-lake/
```

登记表中任务段示例：

```yaml
tasks:
  text_labeling_v1:
    domain: general_text
    status: active
    task_uri: r2:labeling-lake/labels/general_text/text_labeling_v1/task_snapshot/created_date=2026-06-27/task.yaml
    source_dataset_id: text_labeling_v1_label_inputs
```

面板同步时会校验：

- `task_uri` 指向的 `task.yaml` 内部 `task_id` 必须等于登记表 key。
- 如果登记表写了 `source_dataset_id`，`task.yaml` 中的 `data_lake.source_dataset_id` 必须一致。
- 如果 `task.yaml` 写了 `data_lake.lake_registry_uri`，必须指向当前 registry。

本地 `tasks/<task_id>/task.yaml` 只是缓存。生产面板不允许在本地新建或归档任务配置；任务下线应修改 R2 registry 中的任务状态。

## task.yaml 字段

使用数据湖的任务可以配置：

```yaml
data_lake:
  lake_registry_uri: r2:labeling-lake/governance/data_lake/v1/current/data_lake.yaml
  source_dataset_id: text_labeling_v1_label_inputs
  source_manifest_uri: r2:labeling-lake/manifests/general_text/text_labeling_v1_label_inputs/manifest.json
  source_object_path: seed_500/v1/raw.jsonl
  default_import_id: text_labeling_seed_500_2026_06_27
  output_base_uri: r2:labeling-lake/labels/general_text/text_labeling_v1/
```

字段含义：

- `lake_registry_uri`：数据湖登记表。可省略，默认读取当前部署的 `task_registry_uri`。若只写到目录，系统会补 `data_lake.yaml`。
- `source_dataset_id`：registry 中的数据集编号。
- `source_manifest_uri`：源数据集 manifest。可省略；如果显式填写，必须和 registry 中的 manifest 完全一致。
- `source_object_path`：安全相对路径。优先精确匹配 manifest `objects[].path`；若登记表的 dataset 写了 `canonical_uri`，也可填写相对该 `canonical_uri` 的路径。平台不接受任意 `storage_uri` 作为导入源。
- `default_import_id`：面板从数据湖生成本地导入时使用的默认导入编号。
- `output_base_uri`：后续标注结果、训练集、预测结果回写 R2 的根路径。

## labels 层结构

labels 层要区分输入和输出，推荐使用稳定的 `<domain>/<task_id>/` 结构：

```text
labels/<domain>/<task_id>/inputs/...
labels/<domain>/<task_id>/annotation_jobs/...
labels/<domain>/<task_id>/decisions/...
labels/<domain>/<task_id>/gold/...
labels/<domain>/<task_id>/predictions/...
```

建议 `dataset_id` 明确表达资产类型：

```text
<task_id>_label_inputs
<task_id>_gold
<task_id>_predictions
```

## 导入流程

在“数据导入”页：

1. 点击“检查配置”，面板会通过 `rclone` 读取 registry 和 manifest。
2. 确认源数据集、源对象和对象大小。
3. 点击“从数据湖生成导入”，页面创建异步导入 job，并显示 job 状态。
4. job 只把选中的任务级 JSONL 下载到临时位置，完成校验后原子提交到 `runs/<task_id>/imports/<import_id>/raw.jsonl` 和 `manifest.json`。
5. job 成功后，profile 下一步是从该导入中抽取样本。

选择源对象的规则是：

```text
source_dataset_id
  -> registry 找 manifest
  -> manifest.objects[] 精确匹配 source_object_path，或按 dataset.canonical_uri 匹配相对路径
  -> 得到 storage_uri / bytes / sha256 / rows
  -> 创建异步导入 job
  -> rclone copyto 到本地临时文件
  -> 校验 bytes、sha256、rows、id_field、unique_ids
  -> 原子提交为本地 import
```

如果 manifest 中有多个 JSONL，而任务没有指定 `source_object_path`，平台必须失败，不做猜测。

R2 导入不是把 data lake 复制成本地第二份权威数据。它只是下载、校验并 materialize 当前任务需要的输入对象；页面应通过 job 的排队、运行、成功或失败状态反馈进度。

## 输入对象 manifest

任务级输入对象应在 manifest 中记录：

```json
{
  "path": "seed_500/v1/raw.jsonl",
  "storage_uri": "r2:labeling-lake/labels/general_text/text_labeling_v1/inputs/seed_500/v1/raw.jsonl",
  "asset_type": "label_import_jsonl",
  "rows": 500,
  "id_field": "record_id",
  "unique_ids": 500,
  "bytes": 123456,
  "sha256": "...",
  "created_by": "scripts/export_label_units.py",
  "upstream_uri": ["..."],
  "sampling_strategy": "stratified_manual_seed"
}
```

生成的本地导入 manifest 会记录：

- `lake_registry_uri`
- `source_dataset_id`
- `source_manifest_uri`
- `source_object_uri`
- `source_object_path`
- `source_object_bytes`
- `source_object_sha256`
- `source_content_sha256`
- `source_rows`
- `source_asset_type`
- `source_id_field`
- `source_unique_ids`
- `source_created_by`
- `source_upstream_uri`
- `source_sampling_strategy`

同一导入编号、同一内容且同一数据湖血缘会幂等复用；同一编号但内容或血缘不同会拒绝写入。

## rclone 要求

R2 操作层只使用 `rclone`。应用、镜像和 compose 文件都不直接保存 R2 access key / secret key。

面板运行环境必须能执行 `rclone`，并且 rclone 配置中要有可访问目标 bucket 的 remote，例如 `r2`。Docker 部署时使用 `docker-compose.rclone.example.yml` 把宿主机 `rclone.conf` 只读映射到容器：

```bash
docker compose -f docker-compose.yml -f docker-compose.rclone.example.yml up -d --no-build
```

相关运行配置：

```text
RCLONE_CONFIG=/run/secrets/rclone/rclone.conf
LLS_RCLONE_TIMEOUT_SECONDS=120
```

不要把 `rclone.conf` 写进镜像，不要在 `.env` 或 compose 文件中暴露 R2 密钥。`.env` 只保存 registry URI、允许访问的 R2 前缀和 rclone 配置文件路径。

生产环境只允许访问 `data_lake_r2_prefix` / `LLS_DATA_LAKE_R2_PREFIX` 下的 R2 URI。本地路径和 `file://` 只在单测或开发排查时允许，需显式设置：

```text
LLS_ALLOW_LOCAL_DATA_LAKE_URIS=1
```

## 边界

大数据抽样在 data lake / ETL 层完成。任务级 JSONL 在 R2 `labels/.../inputs/...` 中权威保存。平台只把任务输入 materialize 成本地执行缓存，不维护上游大数据的第二份路径体系；导入完成后的样本抽取是从本地任务级 import 中抽取标注样本。

生产面板默认不允许实验人员覆盖 `lake_registry_uri`、`source_dataset_id`、`source_manifest_uri` 或 `source_object_path`。这些字段应由治理配置写入 `task.yaml`；`LLS_ALLOW_DATA_LAKE_OVERRIDES=1` 只用于开发排查。

生产面板默认关闭手动上传文件和粘贴导入，只允许从 R2 数据湖登记内容生成任务级导入。上传和粘贴能力只能在本地开发或测试模式下开启。

平台不长期保存上游大数据，不把 `raw`、`bronze`、`silver` 或 `mart` 数据复制成第二份权威。它只缓存：

- 当前任务导入文件
- 当前任务样本
- 标注分发和拉回结果
- 训练集、预测结果和模型产物

这些本地产物仍要按数据操作规范管理，不覆盖、可追溯、可归档。回写 R2 时必须先生成本地 manifest，再上传产物和 manifest。

## 本地产物发布边界

生产发布不使用 `data-lake export` 作为验收闭环。发布 decisions、gold、predictions 和 model metadata 时，应使用 artifact-aware 的 plan/submit 边界：

```bash
PYTHONPATH=src python3 -m llm_labeling_scaffold.cli data-lake publish plan \
  --task tasks/<task_id>/task.yaml \
  --runs-root runs \
  --kind decisions \
  --artifact-id <decision_id>

PYTHONPATH=src python3 -m llm_labeling_scaffold.cli data-lake publish submit \
  --task tasks/<task_id>/task.yaml \
  --runs-root runs \
  --kind decisions \
  --artifact-id <decision_id> \
  --confirm \
  --idempotency-key <operator-issued-key>
```

`plan` 默认是 dry-run，只读取本地产物并返回本地路径、目标 R2 URI、publish manifest URI、bytes 和 sha256，不写 R2，也不调用 R2 写操作。`submit` 必须显式传入 `--confirm` 和 `--idempotency-key`，会上传本地产物和生成的 publish manifest，并在上传后回读校验 bytes 与 sha256。

发布目标只能位于任务 `data_lake.output_base_uri` 下，并且生产 R2 URI 必须位于当前 `data_lake_r2_prefix` / `LLS_DATA_LAKE_R2_PREFIX` 允许前缀内。发布过程不写 registry，不更新 `current`，不做自动 promotion；如需把版本提升为权威对象，应由数据湖治理流程另行完成。
