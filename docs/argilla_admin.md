# Argilla owner-side provisioning

`llm_labeling_scaffold.integrations.argilla_admin` 只负责通过 Argilla 2.8 public Python SDK 校验或创建 annotator、稳定个人 workspace 和 membership。它不接入数据库、Panel API、UI、allocation、通用 Job 或 `/api/action`。

## 运行边界

- SDK 和 server 都必须通过现有 Argilla contract 的 2.8.x 校验；项目依赖由 #59 固定到受支持的 2.8 版本。
- owner API key 只从 `ARGILLA_API_KEY` secret/env 读取。adapter 构造函数不接受 `api_key` 参数。
- `client.me.role` 必须是 `admin` 或 `owner`。目标用户必须是独立的 `annotator`，owner 账号不会作为 annotator 返回。
- Argilla 2.8 public SDK 不暴露 user active/status，也不支持回读 API key、校验现有密码或安全地完成密码轮换。成功结果只返回 `verification_state=verified`，表示 UUID/name/role/resource visibility/membership 已重读验证，不表示 active。

## 稳定身份

用户名和 personal workspace name 只由 Scaffold 的不可变 `(issuer, subject)` 身份键派生：

```python
from llm_labeling_scaffold.integrations.argilla_admin import (
    derive_argilla_username,
    derive_personal_workspace_name,
)

username = derive_argilla_username(identity.issuer, identity.subject)
workspace_name = derive_personal_workspace_name(identity.issuer, identity.subject)
```

邮箱、display name、first name 和 last name 都不是身份权威。workspace name 只包含 Argilla 2.8 接受的字母、数字、下划线和连字符。

当调用方已有 Argilla UUID 时，应传入 `expected_user_uuid` 或 `expected_workspace_uuid`。expected UUID 查询失败会直接 fail closed，不会按同名资源回退；UUID 对应的 username/name 漂移、同名异 UUID 或非 annotator role 也会拒绝继续。

## 一次性密码

`password` 只允许作为 `ensure_annotator()` 的内存内参数，用于首次创建用户：

```python
adapter = ArgillaAdminAdapter()
result = adapter.ensure_annotator(
    principal_issuer=identity.issuer,
    principal_subject=identity.subject,
    password=one_time_password,
    first_name="Ada",
    last_name="Lovelace",
)

payload = result.to_dict()
```

首次成功后，调用方可在后续内存内同步中把 DTO 的 UUID 作为 `expected_user_uuid` 和 `expected_workspace_uuid` 传回，以启用 ID 权威校验。

不得把密码放入通用 Job/action params、job JSON、数据库、manifest、日志或异常。adapter 在构造 `User` resource 后清除自己的明文引用，并在 `User.create()` 成功或失败返回后立即通过 public `password` 属性清空 SDK resource。已有用户不会消费或校验传入密码。未来的敏感同步入口由 #64 单独提供。

## 幂等与返回值

- 已存在的 user、workspace 和 membership 会复用；已存在 membership 不发送 POST。
- user/workspace 创建异常后会按稳定名称重读；确认资源已出现时 action 为 `recovered`。
- membership 添加超时或冲突后会重读 `workspace.users`；确认目标 UUID 已存在时 action 为 `recovered`。
- membership 是增量 ensure，不删除 workspace 中的额外成员。
- 返回 DTO 只包含 user `uuid/username/role/first_name/last_name`、workspace `uuid/name`、`verification_state`、`verified_at` 和 user/workspace/membership action。不会返回 SDK resource、raw params、password、API key、status 或 active。
