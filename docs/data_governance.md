# 数据操作规范

平台中的导入数据、样本、标注结果、训练集和模型都按数据资产处理。默认操作必须满足：

1. 不覆盖：同一编号已有不同内容时必须拒绝写入。
2. 幂等：同一编号和同一内容重复提交，应返回已有资产，不重复写入。
3. 可追溯：资产 manifest 必须记录来源、行数、字段、ID 字段、唯一 ID 数、内容哈希和创建时间。
4. 不物理删除：面板上的删除类操作默认是归档或停用，不直接删除原始数据文件。
5. 依赖保护：资产已被下游样本、标注、训练集或模型使用时，禁止归档上游资产。
6. 可检查：导入数据必须支持详情查看、分页浏览、搜索和下载。
7. 原子写入：关键 JSON/JSONL 文件必须先写临时文件，再通过原子替换提交；新资产目录必须先写入 `_staging`，完整后再发布到正式目录。
8. 审计日志：资产创建、复用、归档和失败必须写入 `runs/<task_id>/_audit/events.jsonl`。

## 配置与权威源

平台按三层划分配置和数据权威：

- R2 数据湖 / registry 是任务与数据权威。任务启用状态、远端 `task.yaml`、源数据 manifest、源对象 URI 和需要回写的数据湖产物，都以 registry 登记内容为准。
- panel settings 是当前部署的运行配置。服务器部署后应先在“系统设置”填写 `task_registry_uri` 和 `data_lake_r2_prefix`，再同步任务配置。
- 本地 `tasks/` 和 `runs/` 是执行缓存与产物目录。`tasks/` 可以从 registry 重建；`runs/` 记录本部署产生的导入、样本、标注、训练集、模型、推理结果和审计日志。

R2 访问只通过 `rclone` 完成。应用不保存 R2 密钥，Docker 镜像不内置 `rclone.conf`，compose 只允许把宿主机 `rclone.conf` 只读挂载到容器。

## 导入数据

导入数据保存在：

```text
runs/<task_id>/imports/<import_id>/raw.jsonl
runs/<task_id>/imports/<import_id>/manifest.json
```

保存规则：

- 新导入编号：写入 `raw.jsonl` 和 `manifest.json`。
- 同一导入编号、同一内容：幂等复用，返回已有导入。
- 同一导入编号、不同内容：拒绝写入，要求使用新的导入编号。
- 归档导入：移动到 `runs/<task_id>/_archive/imports/`，不删除数据。
- 已归档导入编号不能复用。
- 导入数据必须包含任务的 `id_field` 和 `text_fields`。
- `id_field` 缺失、重复，或文本字段全为空时，导入必须失败。
- 默认上传上限为 100MB，可通过 `LLS_MAX_IMPORT_BYTES` 调整。
- 来自 R2 数据湖的导入只是任务级执行缓存。源数据集、源 manifest、源对象和内容哈希必须写入导入 manifest，R2 数据湖仍是上游数据权威。

导入 manifest 至少应包含：

- `task_id`
- `import_id`
- `path`
- `rows`
- `fields`
- `id_field`
- `unique_ids`
- `duplicate_ids`
- `missing_ids`
- `content_sha256`
- `created_at`
- `state`

## 样本依赖

从导入数据创建样本时，样本 manifest 必须记录：

```json
{
  "source_import_id": "<import_id>",
  "input_path": "runs/<task_id>/imports/<import_id>/raw.jsonl"
}
```

如果导入数据已经被样本使用，面板必须阻止归档该导入数据。

## 样本数据

样本保存在：

```text
runs/<task_id>/samples/<sample_id>/sample.jsonl
runs/<task_id>/samples/<sample_id>/manifest.json
```

保存规则：

- 新样本编号：写入 `sample.jsonl` 和 `manifest.json`。
- 同一样本编号、同一内容：幂等复用，返回已有样本。
- 同一样本编号、不同内容：拒绝写入，要求使用新的样本编号。
- 样本 manifest 必须包含 `content_sha256`。
- 样本归档移动到 `runs/<task_id>/_archive/samples/`，不删除数据。
- 已归档样本编号不能复用。
- 样本已被本地标注运行、Argilla 分发、标注结果或训练集使用时，禁止归档。

批次切分必须写入样本目录下的 `batches/` 子目录，不能覆盖样本自身的 `manifest.json`。

## 任务配置

生产环境中，任务配置的权威来源是当前 `task_registry_uri` 指向的 R2 registry，不是本地 `tasks/` 目录。规则：

- 新任务必须先写入 R2 任务快照，再在 registry 的 `tasks` 段登记。
- 面板只把 R2 的 `task.yaml` 同步为本地执行缓存。
- 本地任务缓存可以重建，不作为长期权威资产。
- 任务下线应在 R2 registry 中把状态改为非启用状态，不能只删除本地缓存。

本地开发模式下，任务配置归档而不是删除。归档规则：

- 示例任务不可归档。
- 任务已有导入、样本、标注、训练集或模型产物时，不允许归档任务配置。
- 归档任务移动到 `tasks/_archive/`。
- 已归档任务编号不能复用。
