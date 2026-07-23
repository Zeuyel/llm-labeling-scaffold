# 成员管理与权限闭环运维

本文是生产环境成员生命周期的操作主文档。它把 Cloudflare Access 身份认证、Scaffold 工作区授权、Panel 标注人员管理、Argilla 身份映射和 R2 数据湖访问拆成独立边界。

完整链路是：

```text
Cloudflare Access 认证
  -> Scaffold principal（issuer, subject）
  -> workspace membership / role
  -> Argilla annotator mapping（可选）
  -> verified annotator
  -> annotator cohort revision
```

## 1. 边界与权威来源

| 层 | 负责什么 | 不负责什么 |
| --- | --- | --- |
| Cloudflare Access | 登录、Access policy、请求到源站的认证 assertion | 不创建 Scaffold workspace membership，不授予业务角色，不创建 Argilla annotator |
| Scaffold principal/RBAC | 以 `(issuer, subject)` 识别用户，保存 workspace role、task ACL 和审计 | 不按邮箱合并身份，不把 Access policy 当作业务授权 |
| Panel | 展示 session/capability，执行工作区内的标注人员和人员组管理 | 不让浏览器提交 actor、caller、role 或 R2 凭据 |
| Argilla 2.8 | 保存独立的 annotator、personal workspace 和 Argilla workspace membership | owner 账号不是终端用户账号，也不是所有平台成员的共享账号 |
| R2 数据湖 | 保存登记的输入对象和发布产物 | 不向浏览器发放 access key、secret key 或 `rclone.conf` |

`email`、display name 和邮箱域名只用于显示或审计外的快照，身份权威始终是认证提供方的 `issuer` 与稳定 `subject`。同一个邮箱在不同 issuer 或 subject 下是不同 principal；邮箱相同也不会自动合并或获得 membership。

当前控制面已提供：

- `/api/session`：返回当前认证状态、可见 workspace、role 和 capability；
- `/api/members`：列出当前 workspace 成员和邀请记录，并创建邀请；
- `/api/members/invitations/{invitation_id}/revoke`：幂等撤销尚未认领的邀请；已认领邀请拒绝撤销，已撤销或已过期邀请保持原状态；
- `/api/members/{principal_id}/role`、`/api/members/{principal_id}/revoke`：幂等变更或撤销成员资格；
- `/api/annotators`、`/api/annotators/{id}`、`/api/cohorts` 等 Panel 控制路由；
- `DatabaseService` 的成员变更 façade，供控制面和受控后台调用。

成员管理页使用 `/api/members` 作为稳定的工作区成员生命周期入口。Cloudflare Access policy、直接 SQL 或 Argilla owner API 都不能替代该入口。

### 任务授权的强制不变量

task-scoped ACL 只能在 active principal、active workspace 和 active workspace-scoped membership 已确认后增加任务权限。它可以细化某个任务的能力，但不能脱离 workspace membership 单独授权；workspace membership revoke 必须立即阻止任务列表、读取、写入和管理操作。旧 task ACL 行可以随后清理，但不能作为继续放行的依据。

任务列表和判权查询都把 active workspace membership 作为前置条件；残留 task ACL 不能绕过该条件。发布验收仍需运行撤销回归测试。

## 2. 首次管理员 bootstrap

### 前置条件

1. 使用生产 `LLS_DEPLOYMENT_MODE=tunnel`，Panel 只通过 Cloudflare Tunnel 到达；生产认证模式为 `LLS_PANEL_AUTH_MODE=cloudflare_access`。
2. 配置 `LLS_CF_ACCESS_ISSUER` 和 Panel Access application 的 `LLS_CF_ACCESS_AUD`，并确认 Access policy 只允许预定的管理员进入 hostname。
3. 完成 `db-role-init`、Alembic migration 和 `db-role-verify`。Panel 使用 runtime app role，bootstrap/migration 使用 owner，不把 owner 凭据注入浏览器或 Panel 前端。
4. 从受控的 Access 管理日志或管理员操作流程取得首位管理员的稳定 `subject`。不要把 JWT 原文、Access assertion 或真实 subject 写入仓库、工单或文档。

### 执行

生产值通过临时环境变量或 secret manager 注入；以下全部是占位符：

```bash
export LLS_BOOTSTRAP_ISSUER='<access-team-domain>'
export LLS_BOOTSTRAP_SUBJECT='<stable-access-subject>'
export LLS_BOOTSTRAP_WORKSPACE_SLUG='<workspace-slug>'
export LLS_BOOTSTRAP_WORKSPACE_NAME='<workspace-name>'
docker compose run --rm migrate \
  python -m llm_labeling_scaffold.cli db bootstrap
unset LLS_BOOTSTRAP_ISSUER LLS_BOOTSTRAP_SUBJECT \
  LLS_BOOTSTRAP_WORKSPACE_SLUG LLS_BOOTSTRAP_WORKSPACE_NAME
```

也可以使用 `--issuer`、`--subject`、`--workspace-slug` 和 `--workspace-name` 参数。命令按 `(issuer, subject)` 和 workspace slug 幂等执行：重复执行不会创建第二个 principal、workspace 或管理员绑定；发生变更时写入 `workspace.admin_bootstrapped` 审计事件。

bootstrap 是唯一的首位管理员提权入口。它不能用邮箱代替 subject，也不能以邮箱后缀、display name、Access group 名称或“第一个登录的人”自动授予业务权限。

## 3. Cloudflare Access 仅认证

生产请求必须经过 Cloudflare Access 和 Tunnel-only 源站边界。源站验证 `Cf-Access-Jwt-Assertion` 的签名、JWKS、issuer、audience、时间声明、`type=app` 和非空 `sub`；验证结果只建立用户的 `(issuer, subject)` 身份。

Access policy 允许用户登录，不等于以下任何一项：

- Scaffold workspace membership；
- `viewer`、`annotator`、`experimenter` 或 `admin` role；
- `task` ACL、数据导入权限或审计查看权限；
- Argilla 用户、Argilla workspace membership 或人员组成员资格。

因此，用户可以出现“Access 登录成功但 `/api/session` 没有 workspace”的正常状态。此时只能视为已认证、零业务权限。数据库授权每次请求重新读取；Access assertion 在有效期内仍不能绕过已撤销的 Scaffold membership。

禁止根据邮箱后缀自动授予任何业务权限。例如，`@example.org` 只能作为 Access policy 或人工审核的输入，不能直接映射为 `admin`、`experimenter`、`annotator` 或某个 workspace 的 membership。

## 4. 邀请、首次登录与成员绑定

“邀请用户”是三个可审计动作，不是一次 Access 配置：

1. **Access 放行**：管理员在 Cloudflare Access policy 中允许目标身份访问 Panel hostname。这一步只解决认证。
2. **创建邀请**：管理员在 Panel 成员管理页按邮箱和目标 role 创建邀请记录。该记录不会因邮箱后缀自动授予权限，也不会把邮箱当成 principal 主键。
3. **首次登录认领**：用户访问 Panel 并完成 Access 登录，系统仅在认证邮箱与未过期邀请精确匹配且邮箱已被可信验证时认领邀请，并以 `(issuer, subject)` 创建或解析 principal。默认要求 Access JWT 提供签名的 `email_verified=true`；使用已由 Access 验证邮箱的 One-time PIN 应用时，管理员必须显式开启 `LLS_CF_ACCESS_TRUST_EMAIL_CLAIM=1`。管理员再确认 `/api/session` 的 workspace、role 和 capability 与授权单一致；用户不需要、也不应提交 JWT、Access assertion 或自报 `X-Actor`。

管理员只能在自己拥有 `workspace:manage` 的 workspace 内执行成员变更。每次变更应记录目标 identity、workspace、旧 role、新 role、操作人、caller、channel 和 request ID。不要通过浏览器直接提交数据库主键、actor 或 caller 来扩大权限。成员管理写请求必须带 `Idempotency-Key`。

成员变更的语义如下：

| 操作 | 条件 | 成功结果 | 审计事件 |
| --- | --- | --- | --- |
| grant | 目标 principal active，且没有 workspace binding | 创建 workspace-scoped role binding | `workspace.membership_granted` |
| change | 已有 binding，目标 principal active | 原子替换 role | `workspace.membership_changed` |
| revoke | 已有 binding；目标 inactive 也可清理 | 删除 workspace binding | `workspace.membership_revoked` |
| unchanged | 重复执行同一状态 | 返回 unchanged，不追加变更事件 | 无新增变更事件 |

最后一个 active workspace admin 不能被删除或降级。revoke 可以保留 task-scoped ACL 记录供后续清理，但这些记录不能继续授权；task list/read/write 必须在每次请求先确认 active workspace membership，因而 workspace revoke 必须立即阻断任务访问。

## 5. 角色矩阵

下表是 workspace-scoped role 的默认能力。只有在 active workspace membership 内，显式 task binding 才能在指定任务上增加 task 能力；它不能获得 `workspace:manage`、`audit:view` 或 `task:create`。

| Scaffold role | 可见/可做事项 | 工作区管理 | Panel 标注人员/人员组管理 | Argilla 映射 |
| --- | --- | --- | --- | --- |
| `viewer` | 读取 active published revision、读取任务 | 否 | 否 | 不自动创建 |
| `annotator` | 读取任务、`annotation:work` | 否 | 否 | 仍需单独创建/绑定并验证 Argilla annotator |
| `experimenter` | 创建任务、编辑/发布任务、提交导入、标注工作与复核、查看审计 | 否 | 否 | 不自动创建 |
| `admin` | 上述全部能力 | 是，`workspace:manage` | 是 | 可执行受控映射和人员组操作 |

Scaffold `annotator` role 与 Argilla `annotator` role 是两个系统中的两个授权记录，不能互相推断。只有同时满足 Scaffold workspace role、Argilla 用户 role、Argilla workspace membership 和 verification 条件，用户才应进入生产人员组。

## 6. Panel 标注人员与 Argilla 映射

### 操作顺序

1. 先按本章第 4 节给用户授予 Scaffold workspace `annotator` role，或确认已有更高的 workspace role。
2. 在 Panel 的“标注人员”页面选择明确的 Scaffold workspace。该页面的查询和写入均要求 `workspace:manage`。
3. 新建 Argilla 用户使用 `POST /api/annotators/provision`；已有 Argilla 用户使用 `POST /api/annotators/bind`。写入必须带有效 `Idempotency-Key`。
4. 重新验证 `POST /api/annotators/{annotator_id}/verify`。验证必须确认 Argilla user UUID、username、personal workspace UUID、workspace membership 和精确的 `annotator` role。
5. 只有 verification 为 `verified` 的记录才能进入人员组。`POST /api/cohorts` 创建人员组，修改成员使用 `/api/cohorts/{cohort_id}/members`；成员变更创建新的不可变 revision。

### 绑定规则

- Scaffold principal 的稳定键是 `(issuer, subject)`；Argilla user UUID 和 personal workspace UUID 首次绑定后不可替换。
- 不能按 username、邮箱或同名 workspace 回退匹配 UUID。身份漂移、role 错误、membership 缺失、重复 Argilla UUID 都必须 fail closed。
- Argilla Server 2.8 的 provisioning 使用 owner-only API。`ARGILLA_API_KEY` 只能由 Panel 后端/secret manager 使用，不能放进浏览器、前端构建产物、任务参数或日志。
- `initial_password` 仅可在首次 provisioning 的内存边界中出现；不进入响应、数据库、幂等 fingerprint、审计 details、日志或通用 job。浏览器提交后立即丢弃，不持久化。
- Argilla owner 账号只承担受控 provisioning/verification 操作，不能作为所有平台成员登录 Argilla 的共享账号，也不能被映射为某个 Scaffold 用户的 `annotator`。每个实际标注者必须有独立 Argilla annotator 身份和独立 personal workspace。

Argilla mapping 不是 Scaffold role binding 的替代品；取消 Argilla membership 后，映射应进入未验证/拒绝状态，不能继续进入新的 cohort revision。历史已封存 revision 保留其快照，不通过覆盖历史行来修复成员变化。

## 7. 撤销访问

人员离开项目、身份失效或发生权限事故时按以下顺序处理：

1. 在 Cloudflare Access policy 中移除或拒绝该身份，阻止新的认证入口。
2. 在 Panel 成员管理入口调用 `revoke_workspace_membership`，撤销目标 workspace binding。先确认 workspace 仍有另一名 active admin；最后管理员保护失败时操作必须被拒绝。
3. 检查该 principal 的所有 task-scoped ACL。workspace revoke 不要求先删除这些记录，但每条任务判权必须立即因缺少 active workspace membership 而拒绝；随后再按任务授权流程清理残留绑定。
4. 在 Argilla 中移除对应 workspace membership 或按组织流程停用该独立 annotator，并重新验证 mapping。不得用 owner 共享账号顶替被撤销的成员。
5. 从未封存的 cohort 草稿中移除该 mapping，并创建新的 cohort revision；已封存 revision 只保留为历史快照，不修改或删除。
6. 用该用户的现有认证请求验证 `/api/session` 不再返回目标 workspace，任务和管理 API 返回预期的不可见/无权结果。数据库授权的实时检查应使尚未过期的 assertion 也不能继续访问业务资源。
7. 保留并检查 `workspace.membership_revoked`、Argilla verification 结果和相关 cohort revision 审计，不删除审计历史。

如果撤销操作返回 unchanged，仍要确认 Access policy、task ACL、Argilla membership 和 cohort 状态，因为 unchanged 只表示指定 workspace binding 已不存在。

## 8. R2 凭据与浏览器边界

浏览器只调用 Panel API，并接收授权后的任务/作业状态、逻辑 URI 和必要的业务数据；浏览器不直接访问 R2，不接收以下任何值：

- R2 access key、secret key 或临时凭据；
- `rclone.conf` 内容或路径中的敏感值；
- Scaffold PostgreSQL owner/app 密码；
- Argilla owner API key；
- Cloudflare Access assertion、OAuth token 或 MCP 内部 token。

Panel 后端通过 `rclone` 读取或发布 R2。Docker 部署使用 `docker-compose.rclone.example.yml` 将宿主机配置只读挂载到容器，密钥由 secret manager 或受控主机文件提供；不得把配置 COPY 进镜像、写入 compose、`.env`、任务配置或前端 bundle。`LLS_DATA_LAKE_R2_PREFIX` 只限制允许的 URI 前缀，不是凭据，也不构成浏览器直接访问授权。

## 9. 生产验收步骤

验收使用占位符和测试身份，不把真实 token、密码、subject、JWT、API key 或 R2 密钥写入仓库。

### 1. 管理员首次进入

- 完成迁移、`db-role-verify` 和 Panel 启动检查。
- 确认生产使用 Cloudflare Access + Tunnel-only；公网 IP 和非预期端口不能绕过 Access。
- 执行一次 bootstrap，记录返回的幂等结果，不记录真实 subject 或 assertion。
- 管理员首次登录 Panel，`/api/session` 返回 `authenticated=true`、`authorization.state=ready`、目标 workspace、`admin` role、`workspace:manage` 和预期 workspace capability。
- 重复 bootstrap，确认不生成第二个 binding 或第二条 bootstrap 变更事件。

### 2. 邀请用户

- 在 Access policy 中允许测试身份，但不要修改其 Scaffold role。
- 用户完成首次 Access 登录后，确认能认证但没有 workspace/task 权限；不能因为邮箱后缀自动出现 `viewer` 或 `admin`。
- 通过实际成员管理入口按稳定 `(issuer, subject)` 解析/创建 zero-role principal，并显式 grant 一个预定 role。
- 检查 grant 响应的 workspace、target identity 和 role 与授权单一致。

### 3. 用户首次登录绑定

- 用户刷新或重新进入 Panel，检查 `/api/session` 只返回被授予的 workspace 和 capability。
- 用不同 workspace selector 和另一测试身份验证资源不可见时不会泄露 workspace/task ID。
- 确认用户无法通过请求体中的 actor、caller、email 或 role 字段改变自身授权。

### 4. 授予标注人员权限

- 管理员把用户的 Scaffold workspace role 设置为 `annotator`；验证其得到 `annotation:work`，但没有 `workspace:manage`。
- 通过 Panel 标注人员页面执行 provision 或 bind，再执行 verify。
- 验证 Argilla role 精确为 `annotator`、不是 owner/admin，workspace membership 和 personal workspace UUID 均存在，映射状态为 `verified`。
- 确认 owner API key、一次性密码和 Access assertion 没有出现在前端响应、日志、审计 details 或持久化记录中。

### 5. 创建人员组

- 使用具有 `workspace:manage` 的管理员在明确 workspace 下创建 cohort。
- 只有已验证的 `annotator` mapping 可以被选中；未验证、role 错误、membership 缺失和 owner 账号必须被拒绝。
- 修改成员或容量后检查 revision number 增加，旧的封存 revision 仍可复现且不可覆盖。
- 如人员组供 allocation 使用，确认 plan 引用的是同一 workspace、connection binding 和已封存 cohort revision。

### 6. 撤销访问

- 移除 Access policy，撤销 Scaffold workspace membership，检查 task-scoped ACL，并移除/停用 Argilla membership。
- 用撤销前已取得但仍在有效期内的测试请求验证业务路由仍被数据库授权拒绝。
- 验证 `/api/session` 不再返回该 workspace；该用户不能读取任务、调用标注人员/人员组管理或进入 Argilla 工作区。
- 即使保留撤销前的 task-scoped ACL 行，任务列表、读取、写入和任务级管理也必须立即失败；若仍能访问，验收必须失败并阻断发布。
- 确认最后一个 active admin 不能被撤销或降级；确认历史 cohort revision 和 audit event 未被删除。

### 7. 审计检查

- 检索 `workspace.admin_bootstrapped`、`workspace.membership_granted`、`workspace.membership_changed`、`workspace.membership_revoked`。
- 检索 `annotator.mapping.created`/`reused`、`annotator.mapping.verified` 或 `annotator.mapping.verification_rejected`，以及 `annotator.cohort.created`/`reused`、`annotator.cohort.updated`、`annotator.cohort.revision.created`/`reused`。
- 对每个事件核对 workspace、actor、caller、channel、target principal、旧 role、新 role、request ID 和资源 ID。
- 确认审计是追加式的；更新、删除和 truncate 历史事件都会失败。
- 扫描日志、HTTP response、幂等记录、审计 details、compose 配置和前端 bundle，确认没有真实 token、密码、subject、JWT、API key、R2 密钥或 Argilla owner 凭据。

## 10. 失败处理

- Access 登录成功但没有 workspace：按“已认证、未授权”处理，检查 principal `(issuer, subject)` 和显式 membership，不扩大 Access policy 作为临时修复。
- 标注人员为 `unverified`、`identity_drift`、`role_error` 或 `membership_missing`：停止加入 cohort，先修复对应 Argilla UUID、role、personal workspace 或 membership，再重新 verify。
- Panel/数据库授权不可用：按 `503` fail closed，不使用缓存 role、不直接写 `role_bindings`。
- 发现 task-scoped ACL 在没有 active workspace membership 时仍能放行：标记为发布阻断，修正 `list_authorized_tasks` 和 `authorize_task` 的 membership 前置检查并补回归测试。
- R2 访问失败：只检查 Panel 后端 rclone/secret manager 和允许前缀，不向浏览器下发 R2 凭据作为绕过方案。

## 相关文档

- [Scaffold 数据库与 RBAC](database.md)
- [Cloudflare Access 身份验证](cloudflare_access.md)
- [标注人员与人员组 API 适配层](panel_annotators.md)
- [Argilla owner-side provisioning](argilla_admin.md)
- [数据湖接入说明](data_lake_scaffold_integration.md)
