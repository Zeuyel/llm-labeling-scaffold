# Cloudflare Access 身份验证

生产 Panel 使用 Cloudflare Access 完成用户登录，源站只验证 Cloudflare 注入的 `Cf-Access-Jwt-Assertion`。Scaffold 不签发 OAuth access token、refresh token，也不实现本地密码系统或 OAuth Authorization Server。

## 认证配置

```text
LLS_PANEL_AUTH_MODE=cloudflare_access
LLS_CF_ACCESS_ISSUER=https://YOUR_TEAM.cloudflareaccess.com
LLS_CF_ACCESS_AUD=<Access application Audience Tag>
LLS_CF_ACCESS_JWKS_TTL_SECONDS=300
LLS_CF_ACCESS_HTTP_TIMEOUT_SECONDS=5
LLS_CF_ACCESS_CLOCK_SKEW_SECONDS=0
```

`LLS_CF_ACCESS_ISSUER` 必须是 Cloudflare One team domain。Panel 从 `${LLS_CF_ACCESS_ISSUER}/cdn-cgi/access/certs` 获取 JWKS，按 `kid` 选择 RS256 公钥，并验证签名、issuer、显式配置的 application audience、`exp`、`iat`、`nbf`、`sub` 和 `type=app`。JWKS 缓存同时受 TTL 和 key 数量上限约束；刷新采用 single-flight，网络 I/O 不持有 key 状态锁。未知 `kid` 和失败刷新使用全局 cooldown 与有界负缓存限制重试频率，cooldown 后仍可发现轮换 key。刷新失败、响应无效或 token 校验失败时请求不会继续进入业务处理。

稳定身份键是 `(issuer, subject)`。邮箱和显示名只保存为可更新的显示快照，不参与唯一身份判断。`X-Actor`、`X-User-Email`、`Cf-Access-Authenticated-User-Email` 等客户端可提交头不会建立 actor。

## 当前授权边界

Panel control API 已接入 PostgreSQL workspace/RBAC 和数据库 task draft/revision 服务：

- `/api/session` 只有在数据库 schema、control 路由和 active task loader 都就绪时返回 `authorization.state=ready`；否则返回 `503`。响应可列出当前 actor 可见的 workspace 与 capability，但不返回 token、claims 或 JWKS。
- workspace 应显式选择；Panel 浏览器兼容期只在 actor 恰好有一个 workspace 时允许省略。未知身份没有 workspace 或权限。
- viewer/annotator 只能读取 active published revision；experimenter/admin 按数据库 ACL 读取或修改 draft、发布 revision、检查任务和提交数据湖导入。
- 不可见资源返回 `404`，权限不足返回 `403`，数据库或 active materialization 不可用返回 `503`，定义错误返回 `422`。
- MCP 内部 bearer 只确定 caller。只有同时通过 MCP 专用 Access verifier 的 assertion 才能建立用户 actor；审计记录真实 actor、service caller 和服务端推导的 MCP channel。

`basic_dev` 仅用于本地开发和迁移：

```text
LLS_PANEL_AUTH_MODE=basic_dev
LLS_PANEL_PASSWORD=<local-only-password>
```

`scripts/stack up` 和 `scripts/stack restart` 会拒绝将 `basic_dev` 绑定到 `127.0.0.1`、`localhost`、`::1` 之外的地址。restart 使用强制重建容器，以应用认证和 MCP 环境变量变更。生产模式不会在 Access 配置缺失或验证失败时回退到 Basic Auth。

## Tunnel-only 源站

生产源站必须只能通过 Cloudflare Tunnel 到达，不能把 Panel 端口暴露到公网后仅依赖请求头。Compose 默认把 Panel 绑定到 `127.0.0.1`；部署时应保持回环绑定，或让 `cloudflared` 与 Panel 位于同一私有容器网络并移除宿主机端口发布。

MCP 使用独立的 Access application 和 `LLS_MCP_CF_ACCESS_AUD`，不能复用 Panel 的 `LLS_CF_ACCESS_AUD`。Managed OAuth 的 opaque `Authorization` token 只由 Cloudflare Edge 消费；MCP 源站验证 Edge 注入的 `Cf-Access-Jwt-Assertion`，随后在进入 FastMCP 前从 ASGI headers 删除外部 `Authorization` 和 assertion。验证后的 assertion 只保存在 request-scoped context，并与 `LLS_MCP_INTERNAL_TOKEN` 一起发送到 Panel。Panel 用独立 MCP verifier 再次验证 assertion 以确定 actor，内部 Bearer 只确定 caller；任一凭据缺失或无效都不会降级为用户身份。完整配置见 [MCP 接入说明](mcp_integration.md)。

主机和云防火墙应拒绝来自公网的 Panel 入站连接，只允许 `cloudflared` 所需的出站连接。部署后同时验证：Access hostname 可以访问；服务器公网 IP 和公开端口不能绕过 Access 直达源站。

Cloudflare 官方参考：

- [Validate JWTs](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/)
- [Application token](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/application-token/)
- [Managed OAuth](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/managed-oauth/)
- [Secure MCP servers](https://developers.cloudflare.com/cloudflare-one/access-controls/ai-controls/secure-mcp-servers/)
- [Tunnel with firewall](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/tunnel-with-firewall/)
