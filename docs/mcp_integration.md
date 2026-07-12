# MCP 接入说明

Scaffold 提供独立的 MCP companion service，供 Codex 和其他 MCP client 调用。它不直接读取或写入 R2、`runs/`、`tasks/`，所有操作都代理到已认证的 Panel HTTP API，因此任务、数据湖、异步 job 和审计仍遵循同一套边界。

```text
MCP client
  -> Streamable HTTP /mcp
  -> MCP companion service
  -> Panel API
  -> Scaffold 控制面 / 数据湖执行器 / Argilla
```

R2 仍是数据湖：保存已登记的输入对象和回写产物。`LLS_TASK_SOURCE=control` 时，任务单、草稿和 revision 由 Scaffold 控制面管理，R2 不作为任务单来源。

## Transport

- `streamable-http`：面向部署后的 SaaS/服务端 client，地址为 `http(s)://<host>:8766/mcp`。
- `stdio`：面向同一台受控机器上的本地 agent，不开放网络端口。

Docker 默认不启动 MCP。只有显式启用 `mcp` profile 后才会暴露端口。

Docker 默认将 MCP 端口绑定到 `127.0.0.1`，由 HTTPS 反向代理对外暴露。只有明确配置 `MCP_BIND_HOST=0.0.0.0` 时才直接监听公网接口。

## 安全边界

首版使用 `LLS_MCP_BEARER_TOKEN` 保护 Streamable HTTP endpoint。token 只用于单租户受控部署，必须至少 32 个非空白字符，并通过 secret manager 或服务器 `.env` 注入，不能写入仓库、任务单、提示词、日志或返回结果。`LLS_MCP_INTERNAL_TOKEN` 是 MCP 到 Panel 的独立内部服务凭据，长度要求相同，不能与 bearer token 复用。

生产环境必须在 HTTPS 反向代理之后公开 `/mcp`。`/healthz` 仅供容器健康检查，不要求 token；其余 MCP 请求必须带：

```http
Authorization: Bearer <LLS_MCP_BEARER_TOKEN>
```

这不是 OAuth/OIDC 或多用户 RBAC。用户身份、组织隔离、任务级权限和真实审计主体由 [#41](https://github.com/Zeuyel/llm-labeling-scaffold/issues/41) 单独交付；在那之前，不应把 bearer token 当作多用户授权方案。

## 启动

在服务器 `.env` 设置高熵 token：

```text
LLS_MCP_BEARER_TOKEN=<由服务器 secret manager 生成的随机值>
LLS_MCP_INTERNAL_TOKEN=<另一条由服务器 secret manager 生成的随机值>
MCP_PORT=8766
```

然后启动：

```bash
./scripts/stack up --mcp
```

或者：

```bash
docker compose --profile mcp up -d
```

MCP companion 通过 Docker 内网访问 `http://panel:8765`，使用与 Panel 相同的部署账号，并使用内部服务凭据让控制面审计记录 actor 为 `mcp`。它不挂载 R2 凭据、`runs/` 或 `tasks/`，因此不能绕过 Panel 的 API 边界。

本地 stdio 调用示例：

```bash
LLS_MCP_PANEL_URL=http://127.0.0.1:8765 \
LLS_MCP_PANEL_USER=admin \
LLS_MCP_PANEL_PASSWORD='<从本机 secret manager 读取>' \
LLS_MCP_INTERNAL_TOKEN='<从本机 secret manager 读取的内部服务凭据>' \
lls mcp --transport stdio
```

## MCP Client 配置

不同 client 的配置键可能不同。核心要求是连接 `/mcp`，并传入 bearer header。概念示例：

```json
{
  "mcpServers": {
    "llm-labeling-scaffold": {
      "url": "https://labeling.example.com/mcp",
      "headers": {
        "Authorization": "Bearer <从 secret manager 读取的 token>"
      }
    }
  }
}
```

不要把真实 token 提交到 MCP client 配置仓库或共享任务文件。

## 工具范围

| MCP tool | 类型 | Panel API | 说明 |
| --- | --- | --- | --- |
| `scaffold_platform_status` | 只读 | health/version/settings | 读取服务健康和非敏感配置。 |
| `scaffold_task_list` | 只读 | tasks list | 列出任务及发布状态。 |
| `scaffold_task_detail` | 只读 | task detail | 读取已发布任务摘要。 |
| `scaffold_task_draft_detail` | 只读 | control detail | 读取草稿和 revision，仅 control 模式。 |
| `scaffold_task_check` | 只读 | task check | 检查 profile 与数据湖来源。 |
| `scaffold_import_list` | 只读 | imports list | 列出本地导入资产。 |
| `scaffold_import_detail` | 只读 | import detail | 读取 manifest、字段和依赖摘要。 |
| `scaffold_data_lake_preview` | 只读 | data lake status | 预览受登记的数据湖对象。 |
| `scaffold_job_status` | 只读 | jobs | 读取异步任务状态。 |
| `scaffold_task_draft_create` | 写入 | create draft | 创建不可执行草稿。 |
| `scaffold_task_draft_update` | 写入 | update draft | 更新草稿，不覆盖已发布 revision。 |
| `scaffold_task_publish` | 写入 | publish | 必须 `confirm=true` 与稳定 `idempotency_key`。 |
| `scaffold_data_lake_import_dry_run` | 只读 | import dry-run | 验证将要 materialize 的受登记对象。 |
| `scaffold_data_lake_import_submit` | 写入 | import submit | 必须 `confirm=true` 与稳定 `idempotency_key`。 |

任务发布使用 task draft 内容生成指纹。同一个 key 和同一草稿会返回原 revision；同一个 key 作用于不同草稿会拒绝，防止 MCP client 网络重试重复发布版本。

数据湖导入的 idempotency 由现有 import job 记录保证：同 key、同请求复用同一 job；同 key、不同请求拒绝。

## 明确禁止

MCP 首版不暴露以下操作：

- 删除或归档任务、导入、样本、标注任务。
- 手动上传/粘贴数据。
- 修改 R2 registry、manifest、`current` 或任意上游源对象。
- 绕过 manifest 传入任意 `storage_uri`、覆盖数据湖来源或自动 promotion。
- Argilla 分发/拉回、gold 构建、训练、批量推理和 Docker 管理。

这些操作具有更高业务或数据风险，需在真实身份/RBAC 与专门审计能力完成后再单独开放。

## 验收

MCP server 的单元测试覆盖：工具白名单、Panel API 代理、写入确认门、bearer token 拒绝逻辑和无 token 启动拒绝。部署后可检查：

```bash
curl -fsS http://127.0.0.1:8766/healthz
```

该健康检查只说明 MCP 进程存活。实际任务和数据湖连通性仍应通过 `scaffold_platform_status`、`scaffold_task_check` 和数据湖 dry-run 逐项验证。
