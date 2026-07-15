# MCP 接入说明

Scaffold 提供独立的 MCP companion service，供 Codex 和其他 MCP client 调用。生产入口由 Cloudflare Access Managed OAuth 保护；Scaffold 是受保护资源源站，不是 OAuth authorization server。它不直接读取或写入 R2、`runs/`、`tasks/`，所有工具操作都代理到 Panel HTTP API。

```text
MCP client
  -> opaque Authorization token
  -> Cloudflare Access Managed OAuth / policy
  -> Cf-Access-Jwt-Assertion
  -> MCP authentication middleware
  -> FastMCP
  -> Panel API with LLS_MCP_INTERNAL_TOKEN
  -> Scaffold 控制面 / 数据湖执行器 / Argilla
```

R2 仍是数据湖：保存已登记的输入对象和回写产物。`LLS_TASK_SOURCE=control` 时，任务单、草稿和 revision 由 Scaffold 控制面管理，R2 不作为任务单来源。

## 认证模式与 Transport

`LLS_MCP_AUTH_MODE` 只允许两个值：

- `cloudflare_access`：Streamable HTTP 的生产默认值。必须设置独立 MCP issuer/AUD 和 Panel 内部 service token。
- `static_dev`：仅用于本地开发。必须显式设置静态 bearer；stack/Compose 的宿主发布地址只允许 `127.0.0.1`，输入 `localhost` 时会归一化为该地址。

- `streamable-http`：面向部署后的 SaaS/服务端 client，地址为 `http(s)://<host>:8766/mcp`。
- `stdio`：面向同一台受控机器上的本地 agent，不开放网络端口。

Docker 默认不启动 MCP。只有显式启用 `mcp` profile 后才会发布端口，默认发布到宿主 `127.0.0.1`。生产由 `cloudflared` 连接该回环端口；如果 `cloudflared` 与 MCP 位于同一私有容器网络，应移除 MCP 的宿主端口发布，而不是改成公网监听。

## Managed OAuth 源站边界

Cloudflare Edge 负责 OAuth discovery、动态客户端注册、Authorization Code + PKCE、Access policy、opaque access token 和 refresh token。Scaffold 不实现 `/authorize`、`/token`、client registration 或 refresh，不要求 OAuth client secret，也不接收、保存、记录或回显 access/refresh token。

Cloudflare 接受客户端 opaque `Authorization` 后，在源站请求中注入 `Cf-Access-Jwt-Assertion`。MCP middleware 使用独立 MCP application 的 issuer/AUD 和 Cloudflare JWKS 验证 RS256 签名、`iss`、`aud`、`exp`、`iat`、`nbf`、`type=app` 与非空 `sub`。Panel assertion 因 AUD 不同而被拒绝；如果环境中 Panel AUD 与 MCP AUD 相同，服务会拒绝启动。

验证成功后，请求级 context 仅保存认证方式、稳定 `(issuer, subject)` 和原始 assertion。assertion 不进入 context 的 `repr`、错误、响应或日志。外部 `Authorization` 与 `Cf-Access-Jwt-Assertion` 请求头在进入 FastMCP 前删除；调用 Panel 时只会重新设置独立的 `LLS_MCP_INTERNAL_TOKEN`，调用方不能覆盖它。`/healthz` 不要求认证，但也会删除敏感认证头。

`cloudflare_access` 缺少 MCP issuer、MCP AUD 或内部 token 时拒绝启动，不会回退到 `LLS_MCP_BEARER_TOKEN`。JWKS 不可用、签名或声明无效时请求 fail closed。

MCP 默认只读。只有 Panel 与 MCP 同时设置 `LLS_MCP_ENABLE_WRITES=1` 时，才允许并注册任务草稿创建、草稿更新、任务发布和数据湖导入提交工具。即使写入已开启，服务身份仍不能调用删除、归档、Argilla、训练、推理或系统设置接口。

## Cloudflare 配置

1. 为 MCP hostname 创建独立的 self-hosted/MCP server Access application。记录其 Audience Tag，不能复用 Panel application AUD。
2. 用 Cloudflare Tunnel 将该 hostname 路由到 `http://127.0.0.1:8766`，并在主机防火墙中阻止公网绕过 Tunnel 访问源站端口。
3. 在该 application 的 Advanced settings 中启用 Managed OAuth。OAuth discovery、DCR、PKCE 和 token endpoint 全部由 Cloudflare 提供。
4. 只允许实际客户端需要的 redirect URI。`localhost` 和 `127.0.0.1` 动态注册开关只应按本地客户端需要启用；其他 URI 使用 HTTPS 并收紧路径范围。
5. 按风险配置 access token lifetime 与 grant session duration。Cloudflare 对 CLI/agent 的建议是较短 access token（5-15 分钟）配合较长 grant session（1-2 周）；refresh token 只保存在 MCP client 与 Cloudflare token endpoint 之间。
6. 配置 Access policy，并验证 Access hostname 可用、服务器公网 IP/端口不可直达。

官方配置入口见 [Managed OAuth](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/managed-oauth/)、[Secure MCP servers](https://developers.cloudflare.com/cloudflare-one/access-controls/ai-controls/secure-mcp-servers/) 和 [Validate JWTs](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/)。

### 无人值守客户端

无人值守调用应使用单独的 Cloudflare Access service token 与专用 Access policy。service token ID/secret 只保存在调用方 secret manager，由 Cloudflare Edge 消费；它不是 OAuth client secret，也不能复用为 `LLS_MCP_INTERNAL_TOKEN`。源站仍只信任经过 MCP issuer/AUD 验证的 Access assertion，Panel 仍只信任内部 service bearer。

## 启动

生产 `.env` 最少需要：

```text
MCP_BIND_HOST=127.0.0.1
LLS_MCP_AUTH_MODE=cloudflare_access
LLS_MCP_CF_ACCESS_ISSUER=https://YOUR_TEAM.cloudflareaccess.com
LLS_MCP_CF_ACCESS_AUD=<MCP application Audience Tag>
LLS_MCP_INTERNAL_TOKEN=<由服务器 secret manager 生成的高熵值>
LLS_MCP_ENABLE_WRITES=0
MCP_PORT=8766
```

`LLS_MCP_CF_ACCESS_AUD` 必须与 `LLS_CF_ACCESS_AUD` 不同。`LLS_MCP_INTERNAL_TOKEN` 必须是至少 32 个非空白字符的高熵 secret，只用于 MCP 到 Panel；不得把 OAuth token、Access assertion、service token secret 或 static-dev bearer 填入该变量。

```bash
./scripts/stack up --mcp
```

或者：

```bash
docker compose --profile mcp up -d
```

MCP companion 通过 Docker 内网访问 `http://panel:8765`，使用内部 bearer 服务身份访问 Panel 的 MCP 路由白名单。它不持有 Panel 管理员密码，也不挂载 R2 凭据、`runs/` 或 `tasks/`。需要写工具时，将 Panel 与 MCP 的 `LLS_MCP_ENABLE_WRITES` 显式改为 `1` 后强制重建容器。

## `static_dev`

本地 Streamable HTTP 调试必须显式启用并监听回环：

```bash
LLS_MCP_AUTH_MODE=static_dev \
LLS_MCP_BEARER_TOKEN='<本地高熵随机值>' \
LLS_MCP_INTERNAL_TOKEN='<独立的 Panel 内部凭据>' \
lls mcp --transport streamable-http --host 127.0.0.1 --port 8766
```

使用 Compose 时同样保持 `MCP_BIND_HOST=127.0.0.1`。`scripts/stack` 会拒绝非回环发布、缺失 token、复用内部 token 或未知认证模式。生产不能设置 `static_dev`，也不会在 Managed OAuth 配置失败时自动回退。

本地 stdio 调用示例：

```bash
LLS_MCP_PANEL_URL=http://127.0.0.1:8765 \
LLS_MCP_INTERNAL_TOKEN='<从本机 secret manager 读取的内部服务凭据>' \
lls mcp --transport stdio
```

stdio 默认同样只读。调试写工具时需同时为 Panel 和 MCP 进程设置 `LLS_MCP_ENABLE_WRITES=1`。

## MCP Client 配置

Managed OAuth client 只需连接 Access 保护的 HTTPS `/mcp` URL。Cloudflare Edge 向非浏览器 client 返回 OAuth discovery 信息，client 完成浏览器授权和后续 refresh；生产配置不要硬编码自定义 `Authorization` header：

```json
{
  "mcpServers": {
    "llm-labeling-scaffold": {
      "url": "https://mcp.example.com/mcp"
    }
  }
}
```

不同 client 的键名可能不同，但 OAuth access/refresh token 应由 client 自身的安全存储管理，不能提交到配置仓库、任务文件或提示词。只有 `static_dev` 本地调试才使用手工 bearer header。

## 当前身份与授权边界

MCP middleware 已建立请求级 `(issuer, subject)` context，但本议题不把该用户身份下沉到 Panel。Panel 当前只看到受限 `mcp` service 身份；Panel RBAC、workspace ACL、tool listing/execution filtering、真实用户审计和最终端到端授权由后续议题完成。因此，验证通过只表示请求来自允许的 Access 身份，不表示已经获得任意 workspace 或任务权限。

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
| `scaffold_task_draft_create` | 写入 | create draft | 仅启用写操作时注册；创建不可执行草稿。 |
| `scaffold_task_draft_update` | 写入 | update draft | 仅启用写操作时注册；必须提交当前 `draft_fingerprint`。 |
| `scaffold_task_publish` | 写入 | publish | 仅启用写操作时注册；必须提交草稿指纹、`confirm=true` 与稳定 `idempotency_key`。 |
| `scaffold_data_lake_import_dry_run` | 只读 | import dry-run | 验证将要 materialize 的受登记对象。 |
| `scaffold_data_lake_import_submit` | 写入 | import submit | 仅启用写操作时注册；必须 `confirm=true` 与稳定 `idempotency_key`。 |

任务草稿详情返回 `draft_fingerprint`。更新和发布必须提交读取时获得的指纹；草稿被其他页面或 client 修改后，旧指纹会得到 `409 Conflict`，不会静默覆盖。同一个发布 key 和同一草稿会返回原 revision；同一个 key 作用于不同草稿会拒绝。

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

MCP server 的单元测试覆盖：独立 issuer/AUD、签名与时间/type/sub 声明、Panel AUD 混用、并发 context 隔离、外部 Authorization 剥离、Panel 内部 token 固定、敏感值不泄漏、生产 fail closed、`static_dev` 回环限制、受限服务身份和工具白名单。部署后可检查：

```bash
curl -fsS http://127.0.0.1:8766/healthz
```

该健康检查只说明 MCP 进程存活。实际任务和数据湖连通性仍应通过 `scaffold_platform_status`、`scaffold_task_check` 和数据湖 dry-run 逐项验证。
