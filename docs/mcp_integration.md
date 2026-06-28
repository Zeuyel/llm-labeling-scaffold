# MCP 接入边界

MCP 只作为 scaffold 的受控调用层。数据湖和任务治理仍以 R2 数据湖登记表、manifest 和登记表指向的 `task.yaml` 为权威，scaffold 只负责检查、导入任务级 JSONL，并读取本地执行状态。`task_registry_uri` 指向数据湖治理登记表，一般是 `data_lake.yaml`；它不是 `task.yaml`，具体任务文件由 `tasks.<task_id>.task_uri` 指向。

## 推荐调用方式

MCP server 优先在仓库根目录下调用 CLI，并读取 stdout 中的 JSON：

```bash
PYTHONPATH=src python3 -m llm_labeling_scaffold.cli <command>
```

CLI 适合无 UI 的 MCP tool。面板 HTTP API 也能提供同等只读信息，但需要面板进程和 Basic Auth；若生产面板已常驻运行，可以复用 API。

#14 的 SaaS/MCP mutating submit 验收路径使用面板 HTTP API：先 dry-run，再带 `confirm: true` 和 `idempotency_key` submit。CLI `data-lake import` 只作为本地 operator direct import 命令。

## 允许动作

- 检查数据湖来源：只读取 registry、dataset manifest 和选中的对象元数据。
- 从数据湖导入：先 dry-run 检查将要读取的 task/source/manifest 和 import id；真实提交只把 `task.yaml` 指定的任务级 JSONL materialize 到 `runs/<task_id>/imports/<import_id>/`。通过面板 API 发起 mutating submit 时必须携带 `confirm: true` 和 `idempotency_key`，返回异步 job，并通过 job 状态查询进度。
- 查询任务列表和任务阶段状态。
- 查询导入列表和导入详情。
- 查询 annotation job、decision artifact、gold version 的只读 list/detail/status，用于 smoke contract 检查。
- 读取面板 API 的等价只读端点。

## 禁止动作

- 不允许 MCP 修改 `task.yaml`、R2 registry、R2 manifest 或任务快照。
- 生产模式下不允许覆盖 `lake_registry_uri`、`source_dataset_id`、`source_manifest_uri`、`source_object_path`。
- 不允许调用删除、归档、任务新建、样本新建、Argilla 分发/拉回、训练、推理或 Docker 管理动作。
- 不允许把只读 status smoke 自动升级为 `POST /api/action`。`argilla_push`、`argilla_pull`、`gold` 都是操作者授权后的写入动作。
- 不允许绕过 manifest 直接传入任意 `storage_uri`。
- 不允许把 data lake 的上游大数据复制成 scaffold 的第二份权威数据。

## CLI 映射

| MCP tool | scaffold command | 说明 |
| --- | --- | --- |
| `scaffold_data_lake_check` | `PYTHONPATH=src python3 -m llm_labeling_scaffold.cli data-lake check --task tasks/<task_id>/task.yaml` | 只读检查 R2 registry、manifest 和对象选择。 |
| `operator_data_lake_import` | `PYTHONPATH=src python3 -m llm_labeling_scaffold.cli data-lake import --task tasks/<task_id>/task.yaml --runs-root runs [--import-id <id>]` | 本地 operator 命令，直接写入本地 import 缓存；不作为 #14 SaaS/MCP mutating submit 验收路径。 |
| `scaffold_task_list` | `PYTHONPATH=src python3 -m llm_labeling_scaffold.cli task list --tasks-root tasks` | 读取本地任务缓存，输出稳定 JSON。 |
| `scaffold_task_status` | `PYTHONPATH=src python3 -m llm_labeling_scaffold.cli task status --task tasks/<task_id>/task.yaml --runs-root runs` | 输出 profile 阶段状态。也可用 `--task-id <task_id> --tasks-root tasks`。 |
| `scaffold_import_list` | `PYTHONPATH=src python3 -m llm_labeling_scaffold.cli import list --task tasks/<task_id>/task.yaml --runs-root runs` | 输出该任务所有本地 import manifest 摘要。 |
| `scaffold_import_detail` | `PYTHONPATH=src python3 -m llm_labeling_scaffold.cli import detail --task tasks/<task_id>/task.yaml --runs-root runs --import-id <id>` | 输出单个 import 的 manifest、字段和依赖摘要。 |

MCP 可读取面板 settings 和任务 `task.yaml` 中的 `data_lake` 配置；任务文件可类似这样声明来源：

```yaml
data_lake:
  lake_registry_uri: r2:YOUR_BUCKET/governance/data_lake/v1/current/data_lake.yaml
  source_dataset_id: example_labeling_v1
  source_object_path: example_label_inputs/v1/raw.jsonl
```

MCP 在生产调用 `data-lake import` 时不应传 `--source-object-path`。该参数只用于人工排查，并且必须由操作者明确授权。

## 面板 API 等价端点

面板 API 返回 JSON，并要求 Basic Auth：

### Contract discovery endpoints

这些端点是 scaffold server 对 MCP 暴露的第一阶段稳定 contract。它们不要求 MCP 读取或修改本地文件：

| HTTP API | 说明 |
| --- | --- |
| `GET /api/health` | 返回服务存活状态。 |
| `GET /api/version` | 返回 scaffold 包版本和 API contract 版本。 |
| `GET /api/capabilities` | 返回机器可读 endpoint/action/schema 概要，用于 MCP 能力发现。 |
| `GET /api/settings/public` | 只返回非敏感运行状态：任务来源模式、是否允许手工导入/数据湖覆盖、registry/R2/rclone 是否已配置。不会返回 token、rclone 配置内容、rclone 配置路径、Argilla 密钥或具体 R2 URI。 |
| `GET /api/tasks/{task_id}` | 返回单个任务的稳定摘要，包括 profile、字段、标签和 data lake 配置摘要。 |
| `POST /api/tasks/{task_id}/check` | 只读检查任务是否可加载、profile 是否有效、data lake 配置是否存在且可 preview。R2/rclone 不可访问时返回 `ok: false`、`checks[]` 和结构化 `errors[]`。 |

`task_check` 不接受 data lake override。生产环境中的数据来源必须来自受控 `task.yaml` / R2 registry。

| MCP tool | HTTP API |
| --- | --- |
| `scaffold_task_list` | `GET /api/tasks` |
| `scaffold_task_status` | `GET /api/task/profile?task_id=<task_id>` |
| `scaffold_import_list` | `GET /api/task/imports?task_id=<task_id>` |
| `scaffold_import_detail` | `GET /api/import/detail?task_id=<task_id>&import_id=<id>` |
| `scaffold_annotation_job_list` | `GET /api/task/annotation_jobs?task_id=<task_id>` |
| `scaffold_annotation_job_detail` | `GET /api/annotation_job/detail?task_id=<task_id>&annotation_id=<id>` |
| `scaffold_decision_artifact_list` | `GET /api/task/decision_artifacts?task_id=<task_id>` |
| `scaffold_decision_artifact_detail` | `GET /api/decision_artifact/detail?task_id=<task_id>&decision_id=<id>` |
| `scaffold_gold_version_list` | `GET /api/task/gold_versions?task_id=<task_id>` |
| `scaffold_gold_version_detail` | `GET /api/gold_version/detail?task_id=<task_id>&version=<version>` |
| `scaffold_data_lake_check` | `GET /api/task/data_lake?task_id=<task_id>` |
| `scaffold_data_lake_import` dry-run | `POST /api/import/data_lake` with `{"task_id":"<task_id>","import_id":"<optional>","dry_run":true}`，返回 `{"ok":true/false,"dry_run":true,"result":{...}}`，其中 `result` 包含 task/source/manifest 摘要、import id 和 validation |
| `scaffold_data_lake_import` submit | `POST /api/import/data_lake` with `{"task_id":"<task_id>","import_id":"<optional>","confirm":true,"idempotency_key":"<stable-key>"}`，返回 job；同 key 同请求返回同一 job，同 key 不同请求拒绝 |
| `scaffold_job_status` | `GET /api/jobs?task_id=<task_id>` |

annotation / decisions / gold 的 list/detail 端点只读取本地 manifest 和文件存在性。返回字段会区分：

- `local_dispatch_file` / `local_dispatch_file_exists`：本地分发 JSONL 是否存在。
- `argilla_published`：已有 annotation manifest 是否能证明已发布到 Argilla。
- `decisions_pulled`：已有 decision manifest 或结果文件是否能证明已拉回标注结果。
- `gold_generated`：已有 gold manifest 或训练集文件是否能证明已生成 gold。
- `state`、`linked_decision_ids`、`linked_gold_versions`：从本地 manifest 可安全推导的当前状态和下游关联。

这些端点适合 MCP 做空状态、fixture 状态和回归 smoke：空列表、404 detail 或 fixture manifest 只能说明 API contract 可执行，不能说明生产标注已完成，也不能替代真实 Argilla 发布、人工提交、结果拉回或 gold 构建。

`POST /api/action` 是 operator-gated 写入入口，当前可用于 `argilla_push`、`argilla_pull`、`gold` 等动作。MCP 只有在操作者明确授权并提供参数时才应调用；调用后应通过 job/status 和上述只读 list/detail 端点复核产物。

当 `LLS_TASK_SOURCE=r2` 时，面板 API 会先从 `LLS_TASK_REGISTRY_URI` 指向的数据湖治理登记表同步启用任务到本地 `tasks/` 缓存。CLI 不隐式同步 registry，只读取当前本地任务文件；因此 MCP 使用 CLI 前应确保任务缓存已存在并来自受控同步。

## 返回值和错误处理

所有推荐 CLI 命令成功时都在 stdout 输出 JSON。MCP 应把非零退出码视为调用失败，并把 stderr/stdout 摘要返回给操作者。CLI `data-lake import` 是本地 operator direct import 命令，不带 SaaS/API 的 submit gate，不作为 #14 的 MCP mutating submit 验收路径。通过面板 API 发起 R2 导入时，MCP 应先调用 dry-run；真实 submit 必须携带 `confirm: true` 和稳定 `idempotency_key`。MCP 应记录返回的 job，并轮询 job 状态；导入成功后再触发 profile 的样本抽取阶段。

`data-lake check` 和 `data-lake import` 需要运行环境可执行 `rclone` 且配置了 `r2` remote。生产环境应按“系统设置”或 `LLS_DATA_LAKE_R2_PREFIX` 配置允许访问的 R2 前缀，例如 `r2:YOUR_BUCKET/...`；本地路径和 `file://` 只允许在测试中显式设置 `LLS_ALLOW_LOCAL_DATA_LAKE_URIS=1`。
