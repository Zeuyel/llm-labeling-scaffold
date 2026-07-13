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
- `transaction`、`append_audit`：在 façade 事务内组合授权和追加审计，不向调用方暴露 ORM session。

`TASK_CREATE` 是 workspace-scoped 权限，只授予 `experimenter` 和 `admin`。其他 task 权限只能传给 task 授权入口；`AUDIT_VIEW`、`WORKSPACE_MANAGE` 和 `TASK_CREATE` 不能通过 task role 获得。actor 与 caller 不同时，caller 必须是 active service principal；普通用户不能伪装成另一用户的调用方。

没有可见 membership/ACL 时，workspace 和 task 判权统一返回 `resource_not_visible`，且不返回 `WorkspaceRef` 或 `TaskRef`，避免枚举资源。未知身份和无 membership 始终是零权限。数据库连接、schema 或事务异常会抛出 `AuthorizationUnavailable`；上层必须 fail closed，不能沿用旧的允许结果。

## 迁移

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

生产环境应从 secret manager 分别注入 `SCAFFOLD_POSTGRES_OWNER_PASSWORD` 与 `SCAFFOLD_POSTGRES_APP_PASSWORD`。任一变量为空时 Compose 会 fail fast；账号或密码相同时，角色初始化也会拒绝执行。`migrate` 只使用 owner，Panel 只使用 app role，不会使用弱口令静默启动。

首次初始化时，PostgreSQL entrypoint 会从脚本自身目录或 `LLS_RUNTIME_ROLE_SQL_PATH` 读取角色 SQL；已有数据卷则由一次性 `db-role-init` 服务重复执行同一份幂等脚本：

```bash
docker compose run --rm db-role-init
docker compose run --rm migrate
```

app role 只获得当前和未来 `public` 表的 `SELECT/INSERT/UPDATE/DELETE`、sequence 使用权，不获得 schema/database DDL、临时表或 `TRUNCATE` 权限。已有数据库若由外部 PostgreSQL 托管，应由具备创建/修改 role 权限的 DBA 运行 `docker/postgres/init-runtime-role.sh`，并确保 `POSTGRES_USER` 是后续执行迁移、拥有 schema 对象的 owner。

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

应用通过授权 façade 追加普通审计，bootstrap/system 审计只走内部路径。SQLAlchemy ORM 会拒绝更新或删除已存在事件；PostgreSQL migration 安装 `BEFORE UPDATE OR DELETE` 和 `BEFORE TRUNCATE` 触发器，SQLite migration 与 `Base.metadata.create_all` 测试路径也安装 UPDATE/DELETE 触发器，因此 bulk SQL 不能覆盖或删除审计历史。数据库 owner 仅用于迁移、备份和受控恢复，Panel 不得持有 owner 凭据。

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
```

恢复后必须重新执行 role 初始化，确保 runtime app 对恢复出的当前对象拥有 DML 且仍无 DDL/TRUNCATE 权限。备份文件包含身份和审计数据，应按敏感生产数据加密、限制访问并设置保留周期；定期在隔离数据库验证恢复与 Alembic revision，而不是只验证 `pg_dump` 返回码。

## 兼容边界

本数据层尚未迁移 `runs/_system/task_control/` 或 `tasks/` 中的现有任务，也不修改 Panel、MCP、Argilla 或文件流水线的业务行为。Task revision、draft/version/reason、可见状态和编辑状态全部由 #46 定义；后续接入必须显式把已有资源映射到 workspace，并为迁移 actor 写入审计事件。只有当 `runs/` 与 `tasks/` 的物理路径完成 workspace 分区、迁移和碰撞验证后，才能把 `task_key` 从全局唯一放宽为 workspace 内唯一。
