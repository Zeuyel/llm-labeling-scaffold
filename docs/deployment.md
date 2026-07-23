# 生产部署结构

生产部署使用 GHCR 中的 Panel 镜像，服务器只运行控制面和 Argilla 依赖；训练任务在独立的高性能计算服务器执行。Cloudflare Access 负责认证，Cloudflare Tunnel 负责把三个内部服务映射到各自 hostname。

```text
Cloudflare Access
        |
Cloudflare Tunnel (cloudflared)
   |          |           |
 label       argilla     mcp (可选)
 panel:8765  argilla:6900 mcp:8766
   |
   +-- scaffold-postgres  (平台成员、任务、权限、审计)
   +-- rclone -> R2       (数据湖输入和产物)

Argilla -> argilla-postgres / Elasticsearch / Redis
```

## 服务器目录

建议将仓库固定在 `/opt/llm-labeling-scaffold`，目录结构如下：

```text
/opt/llm-labeling-scaffold/
├── .env                         # 仅服务器，权限 600
├── docker-compose.yml
├── docker-compose.production.yml
├── docker-compose.tunnel.yml
├── docker-compose.rclone.example.yml
├── secrets/
│   └── rclone.conf               # 权限 600，只读挂载到 panel
├── runs/                         # 任务级缓存、标注结果、审计产物
├── tasks/                        # control revision 的可恢复缓存
├── configs/                      # 只读运行配置
└── backups/                      # PostgreSQL 备份，不进入镜像
```

R2 是数据权威；`runs/` 和 `tasks/` 只保存任务级缓存、不可变快照和运行产物。不要把上游大数据复制进服务器，也不要把 `rclone.conf`、Argilla API key 或 Tunnel token 放入 Git。

## 首次部署

先在 Cloudflare Tunnel 中配置以下 public hostname，目标服务使用 Compose 网络内的服务名：

| hostname | Tunnel service | Access application |
| --- | --- | --- |
| `label.example.com` | `http://panel:8765` | Panel application |
| `argilla.example.com` | `http://argilla:6900` | Argilla application |
| `mcp.example.com` | `http://mcp:8766` | MCP application，可选 |

Panel 和 Argilla 的 Access policy 至少允许内部成员；MCP 使用独立 application 和独立 AUD。服务器防火墙不开放 8765、6900、8766 到公网。

在服务器执行：

```bash
sudo mkdir -p /opt/llm-labeling-scaffold/{secrets,runs,tasks,configs,backups}
sudo chown -R "$USER":"$USER" /opt/llm-labeling-scaffold
cd /opt/llm-labeling-scaffold
git clone https://github.com/Zeuyel/llm-labeling-scaffold.git .
cp .env.example .env
chmod 600 .env
chmod 700 secrets
```

用服务器 secret manager 或以下方式生成一次性值。生产不要使用 `.env.example` 中的 Argilla 默认值：

```bash
openssl rand -hex 32
```

在 `.env` 至少设置：

```text
PANEL_IMAGE=ghcr.io/zeuyel/llm-labeling-scaffold/panel:integration
LLS_DEPLOYMENT_MODE=tunnel
LLS_PANEL_AUTH_MODE=cloudflare_access
LLS_CF_ACCESS_ISSUER=https://<team>.cloudflareaccess.com
LLS_CF_ACCESS_AUD=<panel application AUD>
CLOUDFLARE_TUNNEL_TOKEN=<secret manager value>

SCAFFOLD_POSTGRES_OWNER_PASSWORD=<随机值 A>
SCAFFOLD_POSTGRES_APP_PASSWORD=<随机值 B>

ARGILLA_USERNAME=<owner username>
ARGILLA_PASSWORD=<随机值 C>
ARGILLA_API_KEY=<随机值 D>
ARGILLA_WORKSPACE=<shared workspace>
ARGILLA_POSTGRES_USER=argilla_owner
ARGILLA_POSTGRES_PASSWORD=<随机值 E>
ARGILLA_POSTGRES_DB=argilla

LLS_TASK_SOURCE=control
LLS_TASK_REGISTRY_URI=r2:ai-innovation-data-lake/governance/data_lake/v1/current/data_lake.yaml
LLS_DATA_LAKE_R2_PREFIX=r2:ai-innovation-data-lake/
RCLONE_CONFIG_HOST=/opt/llm-labeling-scaffold/secrets/rclone.conf
```

将 rclone 配置写入服务器并限制权限：

```bash
install -m 600 /path/from/secret-manager/rclone.conf secrets/rclone.conf
```

如果 GHCR package 是私有的，先在服务器执行 `docker login ghcr.io`；不要把 token 写入 `.env`。

启动生产栈：

```bash
./scripts/stack up
./scripts/stack ps
docker compose logs --tail=200 cloudflared panel argilla
```

`PANEL_IMAGE` 使用 `ghcr.io/...` 时，脚本会先拉取镜像并使用 `--no-build`，不会在服务器执行前端构建。未设置 GHCR 镜像时才会进入源码本地构建路径。

## 首次初始化

迁移服务会自动执行数据库迁移。首次管理员必须使用 Cloudflare Access 签发的稳定 `subject` 显式 bootstrap：

```bash
docker compose run --rm migrate \
  python -m llm_labeling_scaffold.cli db bootstrap \
  --issuer 'https://<team>.cloudflareaccess.com' \
  --subject '<stable-access-subject>' \
  --workspace-slug default \
  --workspace-name 'Default Workspace'
```

之后管理员进入 Panel 的成员管理页创建邀请。Access policy 只允许登录，不自动授予 Scaffold workspace role。需要进入 Argilla 的用户还必须在成员管理页完成 Argilla annotator provision/bind/verify，并加入 cohort。

## 更新与回滚

更新前先备份两个 PostgreSQL 数据库和 `runs/`：

```bash
docker compose exec -T scaffold-postgres pg_dump -U "$SCAFFOLD_POSTGRES_OWNER_USER" "$SCAFFOLD_POSTGRES_DB" > backups/scaffold-$(date +%Y%m%d%H%M%S).sql
docker compose exec -T argilla-postgres pg_dump -U "$ARGILLA_POSTGRES_USER" "$ARGILLA_POSTGRES_DB" > backups/argilla-$(date +%Y%m%d%H%M%S).sql
```

再更新镜像：

```bash
export PANEL_IMAGE=ghcr.io/zeuyel/llm-labeling-scaffold/panel:integration
./scripts/stack restart
```

生产验收固定到不可变 SHA 标签，例如 `panel:sha-<short-sha>`。不要通过 `down -v` 回滚或清理生产卷；该命令会删除 PostgreSQL、Argilla 和 Elasticsearch 持久数据。

## 资源要求

- Elasticsearch 主机必须设置 `vm.max_map_count=262144`。
- 低配测试机至少预留约 4 GB 磁盘和 2 GB 内存；生产建议 8 GB 以上内存，并设置 `ELASTICSEARCH_JAVA_OPTS=-Xms512m -Xmx512m` 或更高。
- 训练 GPU、模型缓存和训练数据不放在这台控制面服务器；训练服务器只消费已登记任务输入并回写 R2 产物。
