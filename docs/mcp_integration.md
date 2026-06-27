# MCP 接入边界

MCP 只作为 scaffold 的受控调用层。数据湖和任务治理仍以 R2 登记表、manifest 和 `task.yaml` 为权威，scaffold 只负责检查、导入任务级 JSONL，并读取本地执行状态。

## 推荐调用方式

MCP server 优先在仓库根目录下调用 CLI，并读取 stdout 中的 JSON：

```bash
PYTHONPATH=src python3 -m llm_labeling_scaffold.cli <command>
```

CLI 适合无 UI 的 MCP tool。面板 HTTP API 也能提供同等只读信息，但需要面板进程和 Basic Auth；若生产面板已常驻运行，可以复用 API。

## 允许动作

- 检查数据湖来源：只读取 registry、dataset manifest 和选中的对象元数据。
- 从数据湖导入：只把 `task.yaml` 指定的任务级 JSONL materialize 到 `runs/<task_id>/imports/<import_id>/`。
- 查询任务列表和任务阶段状态。
- 查询导入列表和导入详情。
- 读取面板 API 的等价只读端点。

## 禁止动作

- 不允许 MCP 修改 `task.yaml`、R2 registry、R2 manifest 或任务快照。
- 生产模式下不允许覆盖 `lake_registry_uri`、`source_dataset_id`、`source_manifest_uri`、`source_object_path`。
- 不允许调用删除、归档、任务新建、样本新建、Argilla 分发/拉回、训练、推理或 Docker 管理动作。
- 不允许绕过 manifest 直接传入任意 `storage_uri`。
- 不允许把 data lake 的上游大数据复制成 scaffold 的第二份权威数据。

## CLI 映射

| MCP tool | scaffold command | 说明 |
| --- | --- | --- |
| `scaffold_data_lake_check` | `PYTHONPATH=src python3 -m llm_labeling_scaffold.cli data-lake check --task tasks/<task_id>/task.yaml` | 只读检查 R2 registry、manifest 和对象选择。 |
| `scaffold_data_lake_import` | `PYTHONPATH=src python3 -m llm_labeling_scaffold.cli data-lake import --task tasks/<task_id>/task.yaml --runs-root runs [--import-id <id>]` | 写入本地 import 缓存；同 ID 同内容幂等复用，不同内容拒绝。 |
| `scaffold_task_list` | `PYTHONPATH=src python3 -m llm_labeling_scaffold.cli task list --tasks-root tasks` | 读取本地任务缓存，输出稳定 JSON。 |
| `scaffold_task_status` | `PYTHONPATH=src python3 -m llm_labeling_scaffold.cli task status --task tasks/<task_id>/task.yaml --runs-root runs` | 输出 profile 阶段状态。也可用 `--task-id <task_id> --tasks-root tasks`。 |
| `scaffold_import_list` | `PYTHONPATH=src python3 -m llm_labeling_scaffold.cli import list --task tasks/<task_id>/task.yaml --runs-root runs` | 输出该任务所有本地 import manifest 摘要。 |
| `scaffold_import_detail` | `PYTHONPATH=src python3 -m llm_labeling_scaffold.cli import detail --task tasks/<task_id>/task.yaml --runs-root runs --import-id <id>` | 输出单个 import 的 manifest、字段和依赖摘要。 |

MCP 可读取面板 settings 和任务 `task.yaml` 中的 `data_lake` 配置；任务文件可类似这样声明来源：

```yaml
data_lake:
  lake_registry_uri: r2:YOUR_BUCKET/registry.json
  source_dataset_id: example_labeling_v1
  source_object_path: example_label_inputs/v1/raw.jsonl
```

MCP 在生产调用 `data-lake import` 时不应传 `--source-object-path`。该参数只用于人工排查，并且必须由操作者明确授权。

## 面板 API 等价端点

面板 API 返回 JSON，并要求 Basic Auth：

| MCP tool | HTTP API |
| --- | --- |
| `scaffold_task_list` | `GET /api/tasks` |
| `scaffold_task_status` | `GET /api/task/profile?task_id=<task_id>` |
| `scaffold_import_list` | `GET /api/task/imports?task_id=<task_id>` |
| `scaffold_import_detail` | `GET /api/import/detail?task_id=<task_id>&import_id=<id>` |
| `scaffold_data_lake_check` | `GET /api/task/data_lake?task_id=<task_id>` |
| `scaffold_data_lake_import` | `POST /api/import/data_lake` with `{"task_id":"<task_id>","import_id":"<optional>"}` |

当 `LLS_TASK_SOURCE=r2` 时，面板 API 会先从 `LLS_TASK_REGISTRY_URI` 同步启用任务到本地 `tasks/` 缓存。CLI 不隐式同步 registry，只读取当前本地任务文件；因此 MCP 使用 CLI 前应确保任务缓存已存在并来自受控同步。

## 返回值和错误处理

所有推荐 CLI 命令成功时都在 stdout 输出 JSON。MCP 应把非零退出码视为调用失败，并把 stderr/stdout 摘要返回给操作者，不应自动重试会写入本地资产的导入命令，除非 import ID 和数据湖血缘完全相同。

`data-lake check` 和 `data-lake import` 需要运行环境可执行 `rclone` 且配置了 `r2` remote。生产环境应按“系统设置”或 `LLS_DATA_LAKE_R2_PREFIX` 配置允许访问的 R2 前缀，例如 `r2:YOUR_BUCKET/...`；本地路径和 `file://` 只允许在测试中显式设置 `LLS_ALLOW_LOCAL_DATA_LAKE_URIS=1`。
