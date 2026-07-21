# 平台成员权限安全验收矩阵

本文档是 Scaffold 多用户控制面的成员权限验收契约。目标基线为
`origin/integration/multiuser-control-plane`。验收对象是一次请求从身份进入、平台授权、外部标注身份映射到数据访问的完整边界。

## 结论摘要

- Cloudflare Access 只负责证明请求携带了配置 application 的有效身份断言，不授予 workspace 或 task 权限。
- Scaffold RBAC 以不可变的 `(issuer, subject)` principal 为授权主体。workspace role 与 task ACL 是两个独立 scope，但 task ACL 只有在 active workspace membership 内才生效；未知主体、无 membership 和不可见资源必须默认拒绝并避免枚举。
- 当前标注人员 API 管理的是 Argilla 外部身份映射，不等于 Scaffold 平台成员。Argilla annotator、Argilla workspace membership 和平台 workspace membership 不能互相替代。
- 平台成员的 grant/change/revoke 当前只有 `DatabaseService` Python façade 可作为稳定公共契约。目标基线没有平台成员管理 HTTP 路由或成员管理页；因此不能把 `/api/annotators` 契约当作平台成员生命周期契约。
- R2 是任务与数据湖的对象权威，不是成员身份源。R2 URI、Argilla user UUID、邮箱和客户端 headers 都不能直接授予数据访问；应用必须先完成 Scaffold task authorization，再执行受控的 R2 读写。

## 四层边界

| 层 | 权威输入 | 该层负责 | 该层不负责 | 失败要求 |
| --- | --- | --- | --- | --- |
| Cloudflare Access | 验签后的 `Cf-Access-Jwt-Assertion`、配置 issuer/audience、`sub` | 验证签名、issuer、audience、时间 claims、`type=app`，形成 `(issuer, subject)` 外部身份 | 不创建 workspace membership、role、task ACL 或 R2 授权 | JWKS、JWT、issuer/audience、Tunnel-only 边界失败时 fail closed；客户端 `X-Actor`、邮箱 headers 不得建立 actor |
| Scaffold RBAC | principal、workspace role、task ACL、真实 actor/caller | 决定 workspace/task capability，执行 membership 生命周期、最后 admin 保护、审计和幂等上下文 | 不把 email 合并为身份，不把 Argilla mapping 当平台成员，不把 R2 URI 当权限 | 未知 principal、无 membership、跨 workspace、scope 错误或数据库不可用不得继续业务处理 |
| Argilla 映射 | Scaffold principal 与 Argilla user/workspace/membership 的 UUID 快照 | 校验或创建 Argilla annotator、个人 workspace 和外部 membership，生成标注人员/人员组的外部映射 | 不授予 Scaffold workspace role，不替代平台成员管理，不提供 Argilla 2.8 未公开的 active/status | role、UUID、username、workspace 或 membership 漂移必须拒绝；外部 DTO 只返回白名单字段 |
| R2 数据访问 | 已通过 RBAC 的 task/workspace 上下文、registry/manifest、受控运行时凭据 | 保存任务与数据湖对象，按 manifest 和 hash 执行 materialize、导入和产物写回 | 不验证 Cloudflare 身份，不解析 email/Argilla UUID 授权，不接受客户端任意 R2 URI 作为权限证明 | 未经 task authorization、registry/manifest 校验或对象完整性校验不得读写；R2/凭据不可用时 fail closed |

## 请求闭环

```text
Cloudflare Access JWT
        |
        v
verified ExternalIdentity(issuer, subject)
        |
        v
Scaffold principal lookup/provision, default zero permissions
        |
        v
workspace role and/or explicit task ACL authorization
        |
        +--> platform member management (grant/change/revoke)
        |
        +--> Argilla annotator mapping and verification
        |
        v
task-scoped R2 operation with manifest/hash checks
```

每个业务请求都必须重新依据当前数据库状态判权。HTTP session、浏览器 workspace 选择、Argilla mapping 或旧的 capability snapshot 不能在 membership revoke 后继续作为授权依据。

## 验收矩阵

| 编号 | 场景 | 预期行为 | 当前测试证据 | 状态 |
| --- | --- | --- | --- | --- |
| RBAC-01 | 未知 principal、未知身份、无 membership | 零权限；不可返回 `WorkspaceRef`/`TaskRef`；不可枚举存在性 | `tests/test_db_service.py::test_unknown_identity_and_missing_membership_have_zero_permissions`、`test_nonmember_cannot_enumerate_workspace_or_task_existence` | 已覆盖 |
| RBAC-02 | viewer/annotator/experimenter/admin 角色矩阵 | 只获得角色允许的 capability；workspace-only permission 不得从 task scope 获得 | `test_role_matrix_task_acl_and_workspace_isolation`、`test_permission_scopes_fail_closed_and_filter_capabilities` | 已覆盖 |
| RBAC-03 | 跨 workspace task、workspace 和 membership 操作 | 返回不可见，不泄露目标 workspace/task；同一 task key 不能跨 workspace 绑定 | `test_role_matrix_task_acl_and_workspace_isolation`、`test_task_key_is_globally_unique_and_task_acl_stays_workspace_scoped` | 已覆盖 |
| RBAC-04 | task ACL 与 workspace role 分离 | task ACL 只能在 active workspace membership 内增加 task capability；不能单独打开 task access，也不能获得 `TASK_CREATE`、`AUDIT_VIEW` 或 `WORKSPACE_MANAGE` | `test_permission_scopes_fail_closed_and_filter_capabilities`；新增撤销闭环测试 | 已覆盖，需后端保持 membership 前置检查 |
| RBAC-05 | 最后一个 active workspace admin | service façade、SQLite/PostgreSQL 触发器和直接 SQL 都不得降级、删除或撤销最后 admin | `test_last_workspace_admin_cannot_be_downgraded_or_deleted`、`test_principal_with_membership_requires_revoke_before_delete`、数据库迁移测试 | 已覆盖 |
| RBAC-06 | 相同 email，不同 issuer/subject | 必须创建不同 principal；email 只作可更新快照，不得合并或自动授予 membership | `test_external_identity_uses_issuer_and_subject_only`、`test_resolve_or_provision_never_merges_by_email_or_grants_membership` | 已覆盖 |
| RBAC-07 | actor/caller 伪造 | actor 必须来自验证后的请求上下文；caller 只能是同一用户或 active service principal；客户端 body 不得提交授权上下文 | Cloudflare header spoof、MCP delegated actor、audit caller 测试；新增 membership façade 和 annotator HTTP 负向测试 | 已覆盖 |
| RBAC-08 | membership revoke 后立即判权 | 新事务/新查询立即拒绝 workspace 与 task 访问；不得依赖旧 session。残留 task ACL 不能绕过 active workspace membership | 新增 `test_membership_revoke_is_immediate_while_task_acl_remains_explicit` | 高风险，必须由后端实现并通过 |
| RBAC-09 | 审计上下文 | grant/change/revoke 与审计在同一事务；事件带 actor/caller/channel/request_id，不保存 email/password/token；审计不可更新或删除 | `test_workspace_membership_lifecycle_is_atomic_idempotent_and_audited`、`test_membership_audit_failure_rolls_back_mutation`、append-only tests | 已覆盖 |
| RBAC-10 | 幂等上下文 | key 只保存 hash；workspace + operation + hash 唯一；actor、caller、fingerprint、permission、resource、channel 不一致时 conflict；撤销后 completion 重新判权 | `test_idempotency_key_is_bound_to_actor_caller_and_fingerprint`、`test_idempotency_claim_conflicts_and_successful_replay`、`test_idempotency_completion_rejects_revoked_membership`、forged claim tests | 已覆盖 |
| EXT-01 | Argilla annotator mapping | 仅验证/创建外部 annotator 与 Argilla membership；不得由 Argilla role 或 verified mapping 推导 Scaffold platform role | `tests/test_argilla_admin.py`、`tests/test_annotator_repository.py::test_binding_mapping_verification_and_sensitive_boundary`、`tests/test_panel_annotators.py` | 已覆盖外部映射边界 |
| DATA-01 | R2 任务/数据访问 | 先通过 Scaffold task authorization，再依据 registry/manifest/hash 读写；R2 凭据属于运行时信任根，不是逐用户 principal | `docs/data_governance.md`、`tests/test_data_lake_publish.py`、`tests/test_smoke.py` | 本地契约覆盖；无真实 R2 成员联测 |
| HTTP-01 | 平台成员管理页和 HTTP 生命周期 | 提供显式成员列表、grant/change/revoke、最后 admin、审计和幂等响应契约；请求 actor/caller 由服务端注入 | 目标基线没有 `/api/members` 或等价平台成员路由，只有 `DatabaseService` façade | 阻塞，需后端/控制面实现后补测 |

## 成员与标注人员的边界

“当前标注人员”在本代码库中指 Argilla 外部身份映射记录。它可以包含 Scaffold principal 引用、Argilla user UUID、Argilla username、个人 workspace UUID、外部 membership 和 verification 状态，但这些字段描述的是外部标注运行时资源，不是平台 workspace membership。

平台成员必须由新的成员管理页和其后端成员管理 API 负责。成员管理页至少要操作 Scaffold 的 `(issuer, subject)` principal、workspace role 和显式 task ACL，并在变更后使后续请求立即重新判权。`/api/annotators`、`/api/cohorts` 只能管理 Argilla 映射、外部验证和人员组，不能被复用为平台成员管理页。

## 当前阻塞点与后端验收建议

1. 后端必须提供可稳定调用的成员管理 HTTP API，或明确将某个现有 API 作为版本化公共契约。契约至少包括列表、grant、change、revoke、workspace scope、资源不可见语义、`Idempotency-Key`、actor/caller 服务端注入和审计结果。
2. 后端必须把 active workspace membership 作为 `authorize_task` 的硬前置条件。撤销 workspace membership 后，即使 task ACL 行仍残留，也必须立即拒绝 task 访问；不能让前端自行删除 task ACL 来弥补授权缺口。该项是高风险验收门槛。
3. 每个 R2 读写路径必须展示对应的 `require_task` 或等价授权检查，以及 registry/manifest/hash 校验；不能把 Argilla user UUID、email、workspace query 参数或 R2 URI 作为授权凭据。
4. 只有在成员 HTTP API 和 R2 授权入口稳定后，才能新增真正的浏览器到 R2 的端到端测试。当前在不改生产代码的约束下，只能验收 `DatabaseService`、Cloudflare auth、Argilla adapter/repository 和既有数据湖契约。

## 验收命令

在具备项目开发依赖的环境运行：

```bash
python3 -m pytest -q tests/test_db_service.py
python3 -m pytest -q tests/test_cloudflare_access_auth.py tests/test_panel_annotators.py tests/test_panel_annotators_routes.py tests/test_argilla_admin.py
python3 -m pytest -q tests/test_data_lake_publish.py tests/test_smoke.py
```

本地缺少 pytest 或可选 Argilla 依赖时，命令只能报告环境阻塞，不能将未运行的测试标记为通过。
