# 标注人员与人员组 API 适配层

`llm_labeling_scaffold.panel_annotators` 是供后续总 Panel 路由调用的纯 DTO/服务适配层。本模块不注册路由、不调用 Argilla，也不依赖具体数据库 repository；数据库和外部服务通过 `AnnotatorRepository`、`AnnotatorAdapter` 与 `WorkspaceAuthorizer` Protocol 注入。

## Workspace 与权限

所有管理请求都必须带 `workspace`。服务默认使用 `Permission.WORKSPACE_MANAGE.value`，即 `workspace:manage`。授权器在服务端接收真实 actor；请求体中的 `actor`、`caller`、`channel`、`permission` 等字段会被拒绝。

资源不可见统一映射为 `404 resource_not_found`，权限不足映射为 `403 permission_denied`，授权/资料库不可用映射为 `503`。异常响应只使用模块定义的安全错误文本，不代理 repository 或外部 adapter 的原始异常内容。

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

repository 仍负责 revision/并发约束，服务通过 `expected_revision` 将冲突留给统一错误映射。

## 一次性初始密码

`initial_password` 只在 `create_annotator` 调用期间传给 provisioning adapter：

1. 不进入 repository 的参数或返回记录；
2. 不出现在响应 DTO、`safe_dict()`、日志或服务错误文本中；
3. 不提供读取当前密码或验证密码的接口；
4. adapter 失败时只返回通用 `external_service_unavailable` 错误。

调用方应在敏感表单提交后立即丢弃原始请求对象，浏览器不得持久化该字段。
