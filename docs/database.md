# Scaffold 数据库与 RBAC

Scaffold 使用独立 PostgreSQL 保存身份、工作空间、授权和审计数据。Docker Compose 中的服务名为 `scaffold-postgres`，数据库与持久卷都不复用 `argilla-postgres`。

## Schema

初始迁移包含以下表：

- `principals`：用户和服务身份，唯一键固定为 `(issuer, subject)`；邮箱仅是可选展示属性。
- `workspaces`：租户和资源隔离边界。
- `role_bindings`：工作空间级或任务级角色绑定；任务绑定使用复合外键保证任务属于同一工作空间。
- `tasks`、`task_revisions`：数据库任务及 revision 模型；当前迁移不导入现有文件任务。
- `idempotency_records`、`workspace_settings`：工作空间级幂等记录和设置；幂等键同时保留 actor、caller 和 request fingerprint，重复键不能由其他主体重放。
- `audit_events`：带 actor 身份快照的追加式审计事件。
- `migration_runs`：`lls db upgrade` 的执行记录；Alembic revision 仍由 `alembic_version` 管理。

`task_revisions.definition`、`idempotency_records.response_body`、`workspace_settings.setting_value` 和 `audit_events.details` 在 PostgreSQL 使用 `JSONB`，SQLite 测试环境保持通用 `JSON`。

不会创建密码、session、OAuth client、authorization code、access token 或 refresh token 表。

## 角色矩阵

| 角色 | 读取任务 | 标注工作 | 标注复核 | 编辑任务配置 | 发布任务 | 提交导入 | 查看审计 | 管理工作空间 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `viewer` | 是 | 否 | 否 | 否 | 否 | 否 | 否 | 否 |
| `annotator` | 是 | 是 | 否 | 否 | 否 | 否 | 否 | 否 |
| `experimenter` | 是 | 是 | 是 | 是 | 是 | 是 | 是 | 否 |
| `admin` | 是 | 是 | 是 | 是 | 是 | 是 | 是 | 是 |

工作空间绑定对该工作空间内的任务生效，任务绑定只对指定任务生效；两个作用域的权限取并集。任何任务权限检查都会同时验证任务的 `workspace_id`，避免跨工作空间复用任务 ID。

## 应用层 façade

Panel、MCP 和后续认证层不直接接收 SQLAlchemy ORM 或 `Session`。公共入口是 `DatabaseService`：

- `resolve_identity`：只按 `(issuer, subject)` 查找 principal。
- `resolve_or_provision`：未知身份只创建零权限 principal；相同邮箱不会合并身份或授予 membership。
- `get_session`、`list_authorized_workspaces`：只返回不可变的 principal、workspace 级角色和 capabilities，不枚举任务，也不创建数据库 session/token 表。
- `list_authorized_tasks`：按 workspace 和 task ACL 延迟加载任务，必须显式传入 `limit`，单页上限 100，并使用 `after_task_key` cursor 翻页。
- `authorize_workspace`、`authorize_task`：每次从数据库读取角色，不缓存放行结果。
- `transaction`、`append_audit`：在 façade 事务内组合授权和追加审计，不向调用方暴露 ORM session。

没有可见 membership/ACL 时，workspace 和 task 判权统一返回 `resource_not_visible`，且不返回 `WorkspaceRef` 或 `TaskRef`，避免枚举资源。未知身份和无 membership 始终是零权限。数据库连接、schema 或事务异常会抛出 `AuthorizationUnavailable`；上层必须 fail closed，不能沿用旧的允许结果。

## 迁移

Docker Compose 会等待 `scaffold-postgres` 健康，再由一次性 `migrate` 服务执行：

```bash
python -m llm_labeling_scaffold.cli db upgrade
```

本机安装项目后也可以直接运行 Alembic：

```bash
export LLS_DATABASE_URL='postgresql+psycopg://scaffold:<url-encoded-password>@127.0.0.1:5433/scaffold'
alembic upgrade head
```

`LLS_DATABASE_URL` 必须指向 Scaffold 数据库，不能指向 Argilla 的 `argilla` 数据库。迁移前应先备份生产数据库；不要在多个发布任务中同时执行 downgrade。

Compose 不提供数据库密码默认值。开发环境可生成 URL 安全的十六进制随机密码并写入本地 `.env`：

```bash
cp .env.example .env
db_password="$(openssl rand -hex 32)"
sed -i "s|^SCAFFOLD_POSTGRES_PASSWORD=.*|SCAFFOLD_POSTGRES_PASSWORD=${db_password}|" .env
unset db_password
```

生产环境应从 secret manager 注入 `SCAFFOLD_POSTGRES_PASSWORD`。该变量为空时 Compose 会 fail fast；Panel、migration 和 PostgreSQL 从同一组 `SCAFFOLD_POSTGRES_*` 变量构造连接，不会使用 `scaffold/scaffold` 静默启动。

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

应用通过 `append_audit_event` 写入事件，SQLAlchemy ORM 会拒绝更新或删除已存在事件。PostgreSQL migration 安装 `BEFORE UPDATE OR DELETE` 触发器；SQLite migration 和 `Base.metadata.create_all` 测试路径也安装 UPDATE/DELETE 触发器，因此 bulk SQL 不能覆盖或删除审计历史。管理员读取权限不包含修改审计表的能力；数据库所有者仅应在受控恢复流程中使用。

## 备份与恢复

备份默认 Compose 数据库：

```bash
docker compose exec -T scaffold-postgres \
  pg_dump -U scaffold -d scaffold -Fc > scaffold.dump
```

恢复前停止 Panel 和 migration 写入，并先创建空数据库。确认备份来源后执行：

```bash
docker compose exec -T scaffold-postgres \
  pg_restore -U scaffold -d scaffold --clean --if-exists < scaffold.dump
docker compose run --rm migrate
```

自定义数据库用户名或库名时，替换命令中的 `scaffold`。备份文件包含身份和审计数据，应按敏感生产数据加密、限制访问并设置保留周期。

## 兼容边界

本数据层尚未迁移 `runs/_system/task_control/` 或 `tasks/` 中的现有任务和 revision，也不修改 Panel、MCP、Argilla 或文件流水线的业务行为。后续接入必须显式把已有资源映射到 workspace，并为迁移 actor 写入审计事件。
