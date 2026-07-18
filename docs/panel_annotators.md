# 标注人员与人员组 API 适配层

`llm_labeling_scaffold.panel_annotators` 是供后续总 Panel 路由调用的纯 DTO/服务适配层。本模块不注册路由、不调用 Argilla，也不依赖具体数据库 repository；数据库和外部服务通过 `AnnotatorRepository`、`AnnotatorAdapter` 与 `WorkspaceAuthorizer` Protocol 注入。

## 专用 HTTP 路由

人员控制 API 只在 Scaffold control-plane 且数据库授权运行时就绪时开放。所有请求必须显式绑定 `workspace`，查询使用 query 参数，写入使用 JSON body。不可见 workspace/resource 返回 `404`，权限不足返回 `403`，授权或运行时不可用返回 `503`。

| method | path | purpose |
| --- | --- | --- |
| `GET` | `/api/annotators?workspace=...` | 标注人员列表 |
| `GET` | `/api/annotators/{annotator_id}?workspace=...` | 标注人员详情 |
| `POST` | `/api/annotators/provision` | 使用一次性密码创建并绑定身份 |
| `POST` | `/api/annotators/bind` | 绑定已有 Argilla 身份 |
| `POST` | `/api/annotators/{annotator_id}/verify` | 重新验证身份 |
| `GET` | `/api/cohorts?workspace=...` | 人员组列表 |
| `GET` | `/api/cohorts/{cohort_id}?workspace=...` | 人员组详情 |
| `POST` | `/api/cohorts` | 创建人员组及初始 revision |
| `PUT` | `/api/cohorts/{cohort_id}` | 更新名称/默认容量并创建 revision |
| `PUT` | `/api/cohorts/{cohort_id}/members` | 替换成员并创建 revision |
| `POST` | `/api/cohorts/{cohort_id}/revisions` | 显式创建不可变 revision |

所有写操作都必须提供非空、合法的 `Idempotency-Key` header。相同 key 及相同请求会重放已保存响应；相同 key 对应不同请求返回 `409 idempotency_conflict`。缺失或非法 header 返回 `422`。`initial_password` 只允许出现在 provisioning 请求体中，不会出现在 response、repository、idempotency fingerprint/response、audit details 或日志中。

## Workspace 与权限

所有管理请求都必须带 `workspace`。服务默认使用 `Permission.WORKSPACE_MANAGE.value`，即 `workspace:manage`。授权器在服务端接收真实 actor；请求体中的 `actor`、`caller`、`channel`、`permission` 等字段会被拒绝。

资源不可见统一映射为 `404 resource_not_found`，权限不足映射为 `403 permission_denied`，授权/资料库不可用映射为 `503`。请求 DTO 错误返回 `422`；repository 返回的损坏记录或响应序列化失败返回 `500 service_unavailable`，不把内部记录问题伪装成用户请求错误。布尔字段只接受 JSON `true/false` 或整数 `0/1`，其他值视为损坏数据。异常响应只使用模块定义的安全错误文本，不代理 repository 或外部 adapter 的原始异常内容。

## 请求白名单

创建请求只允许：

```json
{
  "workspace": "workspace-a",
  "scaffold_user_id": "principal-1",
  "personal_workspace_name": "optional-name",
  "initial_password": "one-time-input"
}
```

创建请求不接受 `argilla_username`。外部用户名必须由不可变 Scaffold principal 或 provisioning adapter 派生。绑定请求可以提交已存在外部身份的 `argilla_user_id`、`argilla_username` 和 `personal_workspace_id`，服务仍会以 adapter 返回的身份快照做一致性校验。

人员组创建、更新和成员替换同样使用显式字段白名单；未知字段、服务端上下文字段及嵌套敏感字段均会被拒绝。

## 响应 DTO

`AnnotatorResponse` 只序列化固定字段：Scaffold 用户、Argilla 用户 UUID/用户名/角色、个人 workspace、membership、创建/更新时间和 `verification`。外部 raw DTO、`active/status` 等未定义字段不会被转发。

verification 使用稳定代码和中文标签：

| code | label |
| --- | --- |
| `unverified` | 未验证 |
| `verified` | 已验证 |
| `rejected` | 已拒绝 |
| `identity_drift` | 身份漂移 |
| `role_error` | 角色错误 |
| `membership_missing` | 工作区成员关系缺失 |
| `external_unavailable` | 外部服务不可用 |

缺失 verification 状态按 `unverified` 处理。服务不会伪造 Argilla 2.8 SDK 未提供的账号 active 状态。

人员组成员必须同时满足：

1. 记录属于当前 workspace；
2. Argilla 角色为 `annotator`；
3. 账号不是 owner；
4. verification 状态为 `verified`。

repository 仍负责 revision/并发约束，服务通过 `expected_revision` 将冲突留给统一错误映射。若 repository 实现可选的 `BatchAnnotatorRepository.get_annotators(workspace, annotator_ids)`，成员校验使用一次批量读取；当前未改动 repository，未提供该能力时服务保留逐项 `get_annotator` fallback，后续 repository 接入批量方法后即可消除该降级路径。

## 一次性初始密码

`initial_password` 只在 `create_annotator` 调用期间传给 provisioning adapter：

1. 不进入 repository 的参数或返回记录；
2. 不出现在响应 DTO、`safe_dict()`、日志或服务错误文本中；
3. 不提供读取当前密码或验证密码的接口；
4. adapter 失败时只返回通用 `external_service_unavailable` 错误；不会保留可能含密码/token 的异常链供日志或响应输出。

调用方应在敏感表单提交后立即丢弃原始请求对象，浏览器不得持久化该字段。
