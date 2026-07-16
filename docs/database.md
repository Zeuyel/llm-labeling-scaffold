# Scaffold 数据库与 RBAC

Scaffold 使用独立 PostgreSQL 保存身份、工作空间、授权和审计数据。Docker Compose 中的服务名为 `scaffold-postgres`，数据库与持久卷都不复用 `argilla-postgres`。

## Schema

初始迁移包含以下表：

- `principals`：用户和服务身份，唯一键固定为 `(issuer, subject)`；邮箱仅是可选展示属性。
- `workspaces`：租户和资源隔离边界。
- `role_bindings`：工作空间级或任务级角色绑定；任务绑定使用复合外键保证任务属于同一工作空间。
- `tasks`：只提供 workspace-scoped `TaskRef` 与 task ACL 所需基础，不定义 draft、revision 或可见状态。由于现有 `runs/<task_id>`、`tasks/<task_id>` 文件路径尚未按 workspace 分区，初始 schema 暂时强制 `task_key` 全局唯一；授权查询仍必须同时携带 workspace。
- `idempotency_records`、`workspace_settings`：工作空间级幂等记录和设置；数据库只保存 idempotency key 的 SHA-256，不保存或回显原 key，并同时绑定 actor、caller 和规范化 request fingerprint，重复键不能由其他主体重放。
- `audit_events`：带 actor 身份快照的追加式审计事件。
- `migration_runs`：`lls db upgrade` 的执行记录；Alembic revision 仍由 `alembic_version` 管理。

`idempotency_records.response_body`、`workspace_settings.setting_value` 和 `audit_events.details` 在 PostgreSQL 使用 `JSONB`，SQLite 测试环境保持通用 `JSON`。

不会创建密码、session、OAuth client、authorization code、access token 或 refresh token 表。

## 角色矩阵

| 角色 | 创建任务 | 读取任务 | 标注工作 | 标注复核 | 编辑任务配置 | 发布任务 | 提交导入 | 查看审计 | 管理工作空间 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `viewer` | 否 | 是 | 否 | 否 | 否 | 否 | 否 | 否 | 否 |
| `annotator` | 否 | 是 | 是 | 否 | 否 | 否 | 否 | 否 | 否 |
| `experimenter` | 是 | 是 | 是 | 是 | 是 | 是 | 是 | 是 | 否 |
| `admin` | 是 | 是 | 是 | 是 | 是 | 是 | 是 | 是 | 是 |

工作空间绑定对该工作空间内的任务生效，任务绑定只对指定任务生效；两个作用域的权限取并集。任何任务权限检查都会同时验证任务的 `workspace_id`，避免跨工作空间复用任务 ID。

## 应用层 façade

Panel、MCP 和后续认证层不直接接收 SQLAlchemy ORM 或 `Session`。公共入口是 `DatabaseService`：

- `resolve_identity`：只按 `(issuer, subject)` 查找 principal。
- `resolve_or_provision`：未知身份只创建零权限 principal；相同邮箱不会合并身份或授予 membership。
- `get_session`、`list_authorized_workspaces`：只返回不可变的 principal、workspace 级角色、`workspace_capabilities`，以及该 workspace role 可继承到任务的 `task_capabilities`；不枚举任务，也不创建数据库 session/token 表。
- `list_authorized_tasks`：按 workspace 和 task ACL 延迟加载任务，必须显式传入 `limit`，单页上限 100，并使用 `after_task_key` cursor 翻页。
- `authorize_workspace`、`authorize_task`：每次从数据库读取角色，不缓存放行结果。
- `claim_idempotency`、`complete_idempotency`：原子 claim/pending/replay，唯一范围固定为 workspace + operation + key hash；actor、caller 或 request fingerprint 任一不一致即返回稳定 conflict，绝不返回其他主体的 response。
- `grant_workspace_membership`、`change_workspace_membership`、`revoke_workspace_membership`：先通过标准 workspace 授权入口保持 `resource_not_visible` 语义，再按 role binding → workspace 顺序加锁并重新校验 `WORKSPACE_MANAGE`；原子修改 binding 并追加审计，重复操作返回稳定的 unchanged 结果。
- `transaction`、`append_audit`：在 façade 事务内组合授权和追加审计，不向调用方暴露 ORM session。

`TASK_CREATE` 是 workspace-scoped 权限，只授予 `experimenter` 和 `admin`。其他 task 权限只能传给 task 授权入口；`AUDIT_VIEW`、`WORKSPACE_MANAGE` 和 `TASK_CREATE` 不能通过 task role 获得。actor 与 caller 不同时，caller 必须是 active service principal；普通用户不能伪装成另一用户的调用方。

没有可见 membership/ACL 时，workspace 和 task 判权统一返回 `resource_not_visible`，且不返回 `WorkspaceRef` 或 `TaskRef`，避免枚举资源。未知身份和无 membership 始终是零权限。数据库连接、schema 或事务异常会抛出 `AuthorizationUnavailable`；上层必须 fail closed，不能沿用旧的允许结果。

## Task revision 物化

`20260714_0002` 已提供 task draft、immutable revision、`current_revision_id` 和 materialization outbox。本实现不修改 task revision authority；membership lifecycle 使用后续独立的 `20260715_0003`。worker 使用已有 `state`、`available_at`、lease、worker 和 `attempt_count` 字段完成领取、超时回收、fencing 与重试。

Compose 中的 `materializer` 服务使用 runtime app role，持续运行：

```bash
python -m llm_labeling_scaffold.cli db materialize \
  --runs-root /app/runs \
  --tasks-root /app/tasks
```

PostgreSQL 领取使用 `FOR UPDATE SKIP LOCKED`。snapshot 写入同文件系统 staging，校验 definition、rendered task 与联合 `content_hash` 后，以不可覆盖目录提交到：

```text
runs/_system/task_control/task_snapshots/<revision_id>/<content_hash>/
```

目标已存在时必须逐文件一致才能幂等复用；缺失、半文件或相异内容会 fail closed。ready 状态先独立提交，随后 worker 在新的事务中锁定 task 行，只允许更大的 `revision_number` 更新 `current_revision_id`，并在同一事务写入 `task.revision_activated` audit。失败或被较新 revision supersede 时不会回退 active revision。

`ControlTaskSnapshotLoader` 从数据库 current revision 对应 snapshot 读取 `TaskConfig.raw`，但使用显式 `tasks_root/<task_key>/task.yaml` 作为稳定逻辑 path，保持所有相对 task 路径的既有解析基址。`tasks/<task_key>/task.yaml` 和 `.task_source.json` 只是兼容缓存；目录和目标文件都拒绝 symlink。

缓存刷新先原子替换 task file，再原子替换包含 task hash 的 metadata。两者不是原子目录 snapshot，中途崩溃会形成可检测 mismatch，worker 启动恢复会按数据库 current snapshot 重建。缓存错误不会回退 activation 或阻塞其他 outbox，恢复扫描会记录并跳过仍不安全的路径。刷新不会删除任务目录中的其他相对资源，loader 也不读取缓存内容决定执行配置；immutable snapshot 始终是唯一执行配置来源。

publish 的 #51 `202 task_publish_v1` 幂等响应不会被 worker 改写。实时状态通过 materialization ID 独立读取：

```bash
python -m llm_labeling_scaffold.cli db materialization-status <materialization-id> \
  --runs-root runs

python -m llm_labeling_scaffold.cli db materialize \
  --materialization-id <materialization-id> \
  --drain-seconds 1 \
  --runs-root runs \
  --tasks-root tasks
```

短 drain 只协助处理并轮询独立 status，不改变 publish operation 的成功 replay 语义。完整状态机和故障矩阵见 [Task revision 物化状态机](task_revision_materialization.md)。

## Membership lifecycle

membership grant/change/revoke 在取得 workspace 锁后重新读取 actor 权限；已有 binding 的变更统一使用 role binding → workspace 锁序，首次 grant 初读不存在 binding 时以 workspace 锁串行化并锁后重读。grant/change 要求 target principal 仍为 active；revoke 允许清理 inactive target。最后管理员只统计 active principal 的 workspace-scoped `admin`，服务层和 SQLite/PostgreSQL 触发器都会拒绝删除或降级最后一个 active admin，直接 SQL 也不能绕过。存在 role binding 的 principal 不能直接删除，必须先通过 revoke 解除成员关系。mutation 与对应审计事件在同一事务内提交；unchanged 不追加事件。task-scoped ACL 独立存在，撤销 workspace membership 不会隐式删除显式 task ACL。

## 迁移

迁移链为 `20260713_0001` → `20260714_0002`（task revision 与 materialization）→ `20260715_0003`（membership lifecycle）。`20260714_0002` 保留为 task revision authority，membership lifecycle 只作为其后续迁移。

Docker Compose 会等待 `scaffold-postgres` 健康，再由一次性 `migrate` 服务执行：

```bash
python -m llm_labeling_scaffold.cli db upgrade
```

本机安装项目后也可以直接运行 Alembic：

```bash
export LLS_DATABASE_URL='postgresql+psycopg://scaffold_owner:<url-encoded-owner-password>@127.0.0.1:5433/scaffold'
alembic upgrade head
```

`LLS_DATABASE_URL` 必须指向 Scaffold 数据库，不能指向 Argilla 的 `argilla` 数据库。迁移前应先备份生产数据库；不要在多个发布任务中同时执行 downgrade。

Compose 不提供数据库密码默认值。开发环境可生成 URL 安全的十六进制随机密码并写入本地 `.env`：

```bash
cp .env.example .env
owner_password="$(openssl rand -hex 32)"
app_password="$(openssl rand -hex 32)"
sed -i "s|^SCAFFOLD_POSTGRES_OWNER_PASSWORD=.*|SCAFFOLD_POSTGRES_OWNER_PASSWORD=${owner_password}|" .env
sed -i "s|^SCAFFOLD_POSTGRES_APP_PASSWORD=.*|SCAFFOLD_POSTGRES_APP_PASSWORD=${app_password}|" .env
unset owner_password app_password
```

生产环境应从 secret manager 分别注入 `SCAFFOLD_POSTGRES_OWNER_PASSWORD` 与 `SCAFFOLD_POSTGRES_APP_PASSWORD`。任一变量为空时 Compose 会 fail fast；账号或密码相同时，角色初始化也会拒绝执行。app 密码只通过环境传入，`psql` 在 SQL 内用 `\getenv` 读取，不会拼入进程 argv。数据库初始化容器、`migrate`、`db-role-init` 与 `db-role-verify` 可读取 owner secret；Panel 和 MCP 只读取 app secret。Scaffold PostgreSQL 位于独立的 internal `scaffold-db` 网络，只有 Panel 和一次性数据库作业加入；宿主机端口默认仅绑定 `127.0.0.1`，生产无需本机访问时应删除端口映射。

首次初始化时，PostgreSQL entrypoint 会从脚本自身目录或 `LLS_RUNTIME_ROLE_SQL_PATH` 读取角色 SQL；已有数据卷则由一次性 `db-role-init` 服务重复执行同一份幂等脚本。迁移完成后，`db-role-verify` 会重新应用权限并执行 catalog 验证，任一权限或对象白名单偏离都会阻止 Panel 启动：

```bash
docker compose run --rm db-role-init
docker compose run --rm migrate
docker compose run --rm db-role-verify
```

app role 只在目标数据库获得 `CONNECT`，没有任何数据库的 `CREATE`/`TEMP`、schema `CREATE`、role membership、sequence、函数或未来对象的默认权限。当前白名单仅允许读取 principal/workspace/task/role binding，创建和更新指定展示列的 principal，创建和更新指定完成列的 idempotency record，创建 workspace setting，以及读取和追加 audit event；migration 与 Alembic 表不可见。除 `pg_catalog`、`information_schema`、`pg_toast` 及 PostgreSQL 临时 schema 外，用户 schema 目录必须精确等于 `public`；新增用户 schema 即使没有授予任何权限也会 fail closed。初始化会先撤销 app/PUBLIC 在所有用户 schema 及其中 table/column/sequence/function 上的权限，verifier 再对用户 schema 目录、每列的有效 `SELECT`/`INSERT`/`UPDATE`/`REFERENCES`、runtime 相关 column ACL、relation ACL、空 sequence 集合，以及函数名、identity argument signature、类型、返回值和语言做等值校验。

ownership 也属于启动前白名单：目标数据库和迁移产生的 public relation、审计 trigger function 必须由 `owner_user`（即预期迁移 owner）所有；PG16 默认的 `public` schema owner `pg_database_owner` 是唯一额外允许值。app 或未知角色成为这些对象的 owner 会使 verifier fail closed。初始化只执行权限和 role 收敛，并在已有对象上先做 ownership preflight，不会把漂移对象重新改回 owner；因此 ownership drift 仍会阻止启动。

初始化会撤销 migration owner 的全局及 `public` schema 级 table/sequence/function 默认授权，特别是 PostgreSQL 默认授予 `PUBLIC` 的 function `EXECUTE`。verifier 同时检查 `pg_default_acl` 的全局规则与 schema 增量，任何非 owner 的未来对象授权都会阻止启动。

跨数据库隔离依赖专用 PostgreSQL 集群。PostgreSQL 没有 ACL `DENY`，runtime role 会继承其他数据库默认授予 `PUBLIC` 的 `CONNECT`/`TEMP`；因此初始化必须以该专用集群的超级用户执行，并撤销当前集群所有数据库对 `PUBLIC` 和 app 的权限，再只向目标数据库授予 app `CONNECT`。非超级 owner、共享托管集群或不能修改所有数据库 ACL 的环境会直接初始化失败，不得把该脚本描述为已经提供集群级最小权限。此类环境必须由 DBA 提供独立集群或等价的 `pg_hba.conf` 与数据库 ACL 隔离后再接入。

数据库 ACL 验证是当前 catalog 的时点保证。验证后新建数据库会重新获得 PostgreSQL 默认的 `PUBLIC CONNECT/TEMP`；专用集群应禁止发布流程外创建数据库，确需创建时必须在 app 再次启动前重跑 `db-role-init` 与 `db-role-verify`。

## 首位管理员

首位管理员必须由一次性命令或等价显式配置创建，不会根据邮箱域名或邮箱地址自动提权：

```bash
docker compose run --rm migrate python -m llm_labeling_scaffold.cli db bootstrap \
  --issuer https://example.cloudflareaccess.com \
  --subject '<stable-access-subject>' \
  --workspace-slug default \
  --workspace-name 'Default Workspace'
```

也可以设置 `LLS_BOOTSTRAP_ISSUER`、`LLS_BOOTSTRAP_SUBJECT`、`LLS_BOOTSTRAP_WORKSPACE_SLUG` 和 `LLS_BOOTSTRAP_WORKSPACE_NAME` 后省略对应参数。重复执行会返回相同 principal、workspace 和 role binding，不会重复创建管理员或 bootstrap 审计事件。

## 审计不可变性

应用通过授权 façade 追加普通审计，bootstrap/system 审计只走内部路径。SQLAlchemy ORM 会拒绝更新或删除已存在事件；PostgreSQL migration 安装固定的 `BEFORE UPDATE OR DELETE ... FOR EACH ROW` 与 `BEFORE TRUNCATE ... FOR EACH STATEMENT` 触发器，且二者必须调用同一个 owner-owned `lls_reject_audit_event_mutation()`。verifier 会精确比较 trigger 名称、事件/timing/level、启用状态和函数 OID，并检查函数语言、返回类型、关键属性及规范化函数体；额外 trigger、错误事件或 timing、禁用 trigger、函数体替换都会 fail closed。SQLite migration 与 `Base.metadata.create_all` 测试路径也安装 UPDATE/DELETE 触发器，因此 bulk SQL 不能覆盖或删除审计历史。数据库 owner 仅用于迁移、备份和受控恢复，Panel 不得持有 owner 凭据。

## 生产信任边界

Scaffold RBAC 用于约束终端用户、跨 workspace 访问和普通应用路径；应用进程、runtime app credential、数据库 owner/运维入口、secret manager 与数据库网络边界都属于 trusted computing base。runtime credential 不是逐用户数据库身份，不能用它抵御 Panel 进程或该凭据完全失陷。

凭据失陷者仍可读取和写入白名单允许的数据，包括伪造新的 audit event；既有 audit event 仍受触发器和权限保护。权限脚本、catalog 验证、internal 网络与 owner credential 隔离减少误配置和横向访问，但不证明应用二进制没有被攻破。需要抵御 runtime credential 完全失陷时，应把敏感授权或审计写入放到持有独立凭据的服务，或采用数据库可验证的逐请求身份。

## 凭据轮换

轮换 app 密码时，先在 secret manager 或 `.env` 更新 `SCAFFOLD_POSTGRES_APP_PASSWORD`，再运行角色初始化并重启 Panel：

```bash
docker compose run --rm db-role-init
docker compose restart panel
```

轮换 owner 密码时，先用当前 owner 凭据进入 `psql` 并执行 `\password scaffold_owner`（自定义用户名时替换），再同步更新 `SCAFFOLD_POSTGRES_OWNER_PASSWORD`，最后运行 `db-role-init` 和 `migrate` 验证连接。不要先改 Compose secret，否则已有数据库不会自动修改 owner 密码。

## 备份与恢复

备份默认 Compose 数据库：

```bash
docker compose exec -T scaffold-postgres \
  sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > scaffold.dump
```

恢复前停止 Panel 和 migration 写入，并先创建空数据库。确认备份来源后执行：

```bash
docker compose exec -T scaffold-postgres \
  sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists' < scaffold.dump
docker compose run --rm db-role-init
docker compose run --rm migrate
docker compose run --rm db-role-verify
```

恢复后必须重新执行 role 初始化，确保 runtime app 对恢复出的当前对象拥有 DML 且仍无 DDL/TRUNCATE 权限。备份文件包含身份和审计数据，应按敏感生产数据加密、限制访问并设置保留周期；定期在隔离数据库验证恢复与 Alembic revision，而不是只验证 `pg_dump` 返回码。

## 兼容边界

本数据层尚未迁移 `runs/_system/task_control/` 或 `tasks/` 中的现有任务，也不修改 Panel、MCP、Argilla 或文件流水线的业务行为。Task revision、draft/version/reason、可见状态和编辑状态全部由 #46 定义；后续接入必须显式把已有资源映射到 workspace，并为迁移 actor 写入审计事件。只有当 `runs/` 与 `tasks/` 的物理路径完成 workspace 分区、迁移和碰撞验证后，才能把 `task_key` 从全局唯一放宽为 workspace 内唯一。
