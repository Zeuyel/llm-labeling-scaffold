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

Issue #44 的 RBAC 数据库尚未接入，因此 Access 身份只证明“谁已登录”，不授予管理员能力：

- `/api/health`、`/api/version`、`/api/capabilities`、`/api/session` 可在 assertion 验证通过后访问。
- 其他业务 API，包括业务数据读取和写操作，统一返回 `503` 与 `code=authorization_unavailable`。
- `/api/session` 只返回 `authenticated=true`、经过验证的用户显示快照、认证方式和 `authorization.state=unavailable`，不返回角色、workspace、capability、token、claims 或 JWKS。
- 现有静态 MCP service bearer 继续按受限路由白名单工作。路由判断使用 `ActorContext.caller`；当前静态 MCP 的 actor 和 caller 都是 service。

`basic_dev` 仅用于本地开发和迁移：

```text
LLS_PANEL_AUTH_MODE=basic_dev
LLS_PANEL_PASSWORD=<local-only-password>
```

`scripts/stack up` 和 `scripts/stack restart` 会拒绝将 `basic_dev` 绑定到 `127.0.0.1`、`localhost`、`::1` 之外的地址。restart 使用强制重建容器，以应用认证和 MCP 环境变量变更。生产模式不会在 Access 配置缺失或验证失败时回退到 Basic Auth。

## Tunnel-only 源站

生产源站必须只能通过 Cloudflare Tunnel 到达，不能把 Panel 端口暴露到公网后仅依赖请求头。Compose 默认把 Panel 绑定到 `127.0.0.1`；部署时应保持回环绑定，或让 `cloudflared` 与 Panel 位于同一私有容器网络并移除宿主机端口发布。

主机和云防火墙应拒绝来自公网的 Panel 入站连接，只允许 `cloudflared` 所需的出站连接。部署后同时验证：Access hostname 可以访问；服务器公网 IP 和公开端口不能绕过 Access 直达源站。

Cloudflare 官方参考：

- [Validate JWTs](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/)
- [Application token](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/application-token/)
- [Tunnel with firewall](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/tunnel-with-firewall/)
