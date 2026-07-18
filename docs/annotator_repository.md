# Annotator Control Repository

AnnotatorControlRepository 是 annotator/cohort 控制面的数据库 facade。调用方必须显式传入 workspace_slug，查询通过 WORKSPACE_MANAGE workspace ACL，写入通过 DatabaseTransaction claim 同时绑定 actor、caller、workspace、operation 和 request fingerprint。

repository 只保存 Argilla workspace、user、personal workspace 的 UUID 和非敏感快照。一次性初始密码可以经过 create_annotator_mapping 的敏感请求边界，但不会进入 request fingerprint、idempotency_records.response_body、audit_events.details、ORM 映射或任何返回 DTO。

## Mapping

create_annotator_mapping 只接受 Argilla annotator role；owner、其他 role、UUID 漂移、username 漂移和重复 Argilla user UUID 都 fail closed。verify_annotator 将远端身份、personal workspace 和 membership 作为一次验证输入：

- 成功写入 verification_state=verified 和 verified_at；
- role 错误、identity drift、membership 缺失、disabled binding 或 mapping 会写入 rejected，清除旧验证时间，并以稳定 reason 拒绝请求；
- Argilla 2.8 不提供的 active/status 字段不会被伪造。

## Cohort Revisions

create_cohort 和 create_cohort_revision 只接受当前 workspace 内、同一 Argilla binding 下 active、verified 且已有 Argilla user UUID 的 mapping。成员容量保存在 revision member snapshot 中。

revision 先以未封存状态写入成员，再在同一事务内绑定 connection 和封存。封存 revision 及其 member 是 immutable；replace_cohort_members 不更新旧行，而是创建新的 revision number 和 fingerprint。allocation plan 引用旧 revision 后仍能读取原始成员和容量。

Issue #66 只复用已有 20260716_0004 allocation schema 和数据库触发器，不新增或修改 migration。已有 SQLite/PostgreSQL migration 已覆盖 workspace 复合外键、远端 UUID freeze、sealed revision/member guard 和 allocation 引用保护。
