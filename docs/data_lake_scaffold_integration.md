# 数据湖接入说明

R2 数据湖是数据权威，scaffold 只是标注执行器。数据湖治理文件的权威位置是：

```text
r2:ai-innovation-data-lake/governance/data_lake/v1/current/
```

本仓库不复制维护数据湖路径规则，只按任务配置读取登记表和 manifest。

## task.yaml 字段

使用数据湖的任务可以配置：

```yaml
data_lake:
  lake_registry_uri: r2:ai-innovation-data-lake/governance/data_lake/v1/current/data_lake.yaml
  source_dataset_id: patent_boundary_v0_1_label_inputs
  source_manifest_uri: r2:ai-innovation-data-lake/manifests/patent/patent_boundary_v0_1_label_inputs/manifest.json
  source_object_path: inputs/manual_seed_500/v1/raw.jsonl
  default_import_id: patent_boundary_manual_seed_500_2026_06_26
  output_base_uri: r2:ai-innovation-data-lake/labels/patent/patent_boundary_v0_1/
```

字段含义：

- `lake_registry_uri`：数据湖登记表。可省略，默认读取 R2 当前登记表。若只写到目录，系统会补 `data_lake.yaml`。
- `source_dataset_id`：登记表中的数据集编号。
- `source_manifest_uri`：源数据集 manifest。可省略；如果显式填写，必须和登记表中的 manifest 完全一致。
- `source_object_path`：manifest `objects[].path` 中的安全相对路径。scaffold 不接受任意 `storage_uri` 作为导入源。
- `default_import_id`：面板从数据湖生成本地导入时使用的默认导入编号。
- `output_base_uri`：后续标注结果、训练集、预测结果回写 R2 的根路径。

## labels 层结构

labels 层要区分输入和输出：

```text
labels/patent/patent_boundary_v0_1/inputs/...
labels/patent/patent_boundary_v0_1/annotation_jobs/...
labels/patent/patent_boundary_v0_1/decisions/...
labels/patent/patent_boundary_v0_1/gold/...
labels/patent/patent_boundary_v0_1/predictions/...
```

建议 dataset_id 明确表达资产类型：

```text
patent_boundary_v0_1_label_inputs
patent_boundary_v0_1_gold
patent_boundary_v0_1_predictions
```

## 导入流程

在“数据导入”页：

1. 点击“检查配置”，面板会通过 `rclone` 读取登记表和 manifest。
2. 确认源数据集、源对象和对象大小。
3. 点击“从数据湖生成导入”。
4. 面板只把选中的任务级 JSONL 原样缓存到 `runs/<task_id>/imports/<import_id>/raw.jsonl`。

选择源对象的规则是：

```text
source_dataset_id
  -> registry 找 manifest
  -> manifest.objects[] 精确匹配 source_object_path
  -> 得到 storage_uri / bytes / sha256 / rows
  -> rclone copyto 到本地临时文件
  -> 校验 bytes、sha256、rows、id_field、unique_ids
  -> 原子提交为本地 import
```

如果 manifest 中有多个 JSONL，而任务没有指定 `source_object_path`，scaffold 必须失败，不做猜测。

## 输入对象 manifest

任务级输入对象应在 manifest 中记录：

```json
{
  "path": "inputs/manual_seed_500/v1/raw.jsonl",
  "storage_uri": "r2:...",
  "asset_type": "label_import_jsonl",
  "rows": 500,
  "id_field": "patent_id",
  "unique_ids": 500,
  "bytes": 123456,
  "sha256": "...",
  "created_by": "scripts/export_patent_boundary_labeling_units.py",
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

面板运行环境必须能执行 `rclone`，并且 rclone 配置中要有 `r2` remote。

宿主机直接运行时，使用当前用户的 rclone 配置即可。

Docker 部署时，不要把密钥写进镜像。使用 compose override、secret 或额外 volume 把只读配置映射到容器：

```yaml
services:
  panel:
    environment:
      - RCLONE_CONFIG=/run/secrets/rclone/rclone.conf
    volumes:
      - ~/.config/rclone/rclone.conf:/run/secrets/rclone/rclone.conf:ro
```

也可以设置：

```text
RCLONE_CONFIG=/run/secrets/rclone/rclone.conf
LLS_RCLONE_TIMEOUT_SECONDS=120
```

## 边界

大数据抽样在 data lake / ETL 层完成。任务级 JSONL 在 R2 `labels/.../inputs/...` 中权威保存。scaffold 只 materialize 任务输入并做标注执行缓存。

scaffold 不长期保存上游大数据，不把 `raw`、`bronze`、`silver` 或 `mart` 数据复制成第二份权威。它只缓存：

- 当前任务导入文件
- 当前任务样本
- 标注分发和拉回结果
- 训练集、预测结果和模型产物

这些本地产物仍要按数据操作规范管理，不覆盖、可追溯、可归档。回写 R2 时必须先生成本地 manifest，再上传产物和 manifest。
