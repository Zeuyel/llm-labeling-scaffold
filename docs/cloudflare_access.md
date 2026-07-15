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

Scaffold 已包含 #44 的 RBAC 数据层，但它尚未接入 Panel 业务授权路径。#46 完成接线和资源迁移前，Access 身份只证明“谁已登录”，不授予管理员能力：

- `/api/health`、`/api/version`、`/api/capabilities`、`/api/session` 可在 assertion 验证通过后访问。
- 其他业务 API，包括业务数据读取和写操作，统一返回 `503` 与 `code=authorization_unavailable`。
- `/api/session` 只返回 `authenticated=true`、经过验证的用户显示快照、认证方式和 `authorization.state=unavailable`，不返回角色、workspace、capability、token、claims 或 JWKS。
- 现有静态 MCP service bearer 继续按受限路由白名单工作。路由判断使用 `ActorContext.caller`；当前静态 MCP 的 actor 和 caller 都是 service。

`basic_dev` 仅用于本地开发和迁移：

```text
LLS_PANEL_AUTH_MODE=basic_dev
LLS_PANEL_PASSWORD=<local-only-password>
```

直接运行 `lls panel` 时使用 `host` 部署模式。`basic_dev` 和 `cloudflare_access` 都只能绑定 IPv4/IPv6 loopback 地址；`127.0.0.0/8`、`localhost` 和 `::1` 可用，`0.0.0.0`、`::`、私网地址和普通主机名会在创建监听 socket 前被拒绝。IPv6 literal 会使用 IPv6 HTTP server。

容器部署显式区分两种边界：

- `LLS_DEPLOYMENT_MODE=loopback`：`scripts/stack` 叠加 `docker-compose.loopback.yml`。Panel 进程在容器内绑定 `0.0.0.0:8765`，Docker 只把它固定发布到宿主机 `127.0.0.1:${PANEL_PORT}`。该 override 不接受可改成 `0.0.0.0` 的 bind-host 变量。
- `LLS_DEPLOYMENT_MODE=tunnel`：只使用 base Compose。Panel 进程仍绑定容器内 `0.0.0.0:8765`，但没有 `ports` 发布；同一 `lls` Docker network 中的 `cloudflared` sidecar 可访问 `http://panel:8765`。该模式只允许 `cloudflare_access`，`basic_dev` 会在服务启动时失败。

本地直接 Compose 启动必须显式叠加 loopback override：

```bash
docker compose -f docker-compose.yml -f docker-compose.loopback.yml up -d
```

Tunnel sidecar 部署使用 base Compose，不叠加 loopback override。把 `cloudflared` 加入 `lls` network，并将 Tunnel service URL 配置为 `http://panel:8765`。如果 `cloudflared` 运行在宿主机而不是 Compose network 中，则使用 loopback 模式，将 service URL 指向 `http://127.0.0.1:${PANEL_PORT}`。

`scripts/stack up` 和 `scripts/stack restart` 要求显式设置 `LLS_DEPLOYMENT_MODE`，并校验部署模式、认证配置、开发密码和数据库凭据组合。restart 使用强制重建容器，以应用认证和 MCP 环境变量变更。生产模式不会在 Access 配置缺失或验证失败时回退到 Basic Auth。

## Tunnel-only 源站

生产源站必须只能通过 Cloudflare Tunnel 到达，不能把 Panel 端口暴露到公网后仅依赖请求头。Base Compose 不发布 Panel 端口；loopback override 只发布到 `127.0.0.1`。自定义 Compose override 不得为 Panel 增加 `0.0.0.0`、`::`、私网或公网宿主接口的 `ports` 映射。

`Cf-Access-Jwt-Assertion` 是 bearer assertion。签名验证证明 token 由配置的 Access issuer 签发且 claims 适用于当前 application，但不证明当前 HTTP 请求实际经过 Cloudflare Tunnel。若源站可被客户端直接访问，合法或被窃取的 assertion 可以绕过 Tunnel 路径直接重放到源站，Cloudflare 对该请求的边缘路径控制也不再成立。因此 Scaffold 只在受控 Tunnel-only 源站边界内把已验证 assertion 作为用户身份依据；仅有正确 JWT 验证不能替代网络隔离。

Panel 启动校验能约束直接主机启动和官方容器模式，但容器内进程无法自行验证 Docker 在宿主机创建的 NAT/port publish。官方 base 与 loopback override 是部署安全策略的一部分；拥有 Compose 文件或 Docker daemon 修改权限的操作者仍可用自定义 `ports` 绕过该策略，必须通过代码审阅、主机防火墙和部署检查共同控制。

主机和云防火墙应拒绝来自公网的 Panel 入站连接，只允许 `cloudflared` 所需的出站连接。部署后同时验证：Access hostname 可以访问；服务器公网 IP 和公开端口不能绕过 Access 直达源站。

Cloudflare 官方参考：

- [Validate JWTs](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/)
- [Application token](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/application-token/)
- [Tunnel with firewall](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/tunnel-with-firewall/)
