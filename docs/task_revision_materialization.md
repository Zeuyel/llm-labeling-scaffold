# Task revision 物化状态机

本文定义数据库 task revision 到本地 immutable snapshot 的持久化 outbox 协议。数据库是 revision 与 current revision 的权威来源；`tasks/<task_key>/task.yaml` 仅为兼容缓存，不参与 current revision 判定。

## Schema 决策

沿用 `20260714_0002`，不修改该迁移，也不新增 `0003`。现有 `task_revision_materializations` 已提供状态、可领取时间、lease、worker、attempt 和错误字段；snapshot 路径可由 revision UUID 与 `content_hash` 确定性推导，不需要持久化路径列。

## 数据库状态机

| 当前状态 | 条件/动作 | 下一状态 | 持久化保证 |
| --- | --- | --- | --- |
| `pending` | `available_at <= now`，worker 在事务中领取 | `processing` | PostgreSQL 使用 `FOR UPDATE SKIP LOCKED`；记录 worker、lease，并递增 `attempt_count` fencing token |
| `processing` | lease 超时后被新 worker 回收 | `processing` | 新 attempt 覆盖 lease；旧 attempt 不能完成数据库状态转换 |
| `processing` | snapshot 原子提交并复核成功 | `succeeded` | ready 状态单独提交；不依赖 `after_commit` callback |
| `processing` | 可重试错误且未达上限 | `pending` | 保存 `last_error`，按 `available_at` 延迟重试 |
| `processing` | 内容冲突、校验失败或达到上限 | `failed` | 保留旧 `tasks.current_revision_id`，不激活失败 revision |
| `succeeded` | 独立事务锁定 task 行，candidate revision 更大 | `succeeded` | 单调更新 `current_revision_id`，同事务追加 activation audit |
| `succeeded` | current revision 相同或更大 | `succeeded` | 幂等 no-op；较旧 revision 标记为 status 层的 `superseded`，不能回退 current |

`attempt_count` 是 lease fencing token。worker 完成 ready 或失败转换时必须同时匹配 materialization ID、`processing`、`claimed_by` 和领取时的 attempt；过期 worker 即使继续运行，也不能提交数据库状态。

## 文件状态机

目标目录固定为：

```text
runs/_system/task_control/task_snapshots/<revision_id>/<content_hash>/
```

每个 snapshot 包含 `task.yaml`、`definition.json` 和 `manifest.json`。提交前必须完成以下校验：

1. 数据库 `definition` 是对象，`rendered_task` 是可解析、可执行的 task YAML。
2. definition 与 rendered task 的 `task_id` 都等于数据库 task key。
3. 重新计算的 definition/rendered 联合 fingerprint 等于 revision `content_hash`。
4. manifest 中的 revision、workspace、task、revision number 和各文件 hash 全部一致。

worker 在目标目录同一文件系统内写唯一 staging 目录，fsync 文件和目录后，在 revision 文件锁下执行不可覆盖 rename。目标不存在时提交；目标已存在时只允许逐项校验后幂等复用。任一文件缺失、hash 不同、manifest 不同或内容不同都 fail closed，绝不删除或覆盖目标。

## 激活与兼容缓存

snapshot ready 后，worker 开启新的数据库事务并锁定 task 行：

1. current 为空时允许激活。
2. candidate `revision_number` 大于 current 时允许激活。
3. candidate 等于 current 时幂等返回。
4. candidate 小于 current 时返回 `superseded`，禁止回退。

更新 `tasks.current_revision_id` 与 `task.revision_activated` audit 在同一事务提交。事务提交后，worker 再从数据库 current revision 对应 snapshot 刷新 `tasks/<task_key>/task.yaml` 与 `.task_source.json`；刷新时再次锁定 task 行，因此乱序 worker 只能写当时数据库 current 对应内容。缓存目录及两个目标文件都拒绝 symlink。

兼容缓存是可恢复缓存，不是原子目录 snapshot。worker 先原子替换 `task.yaml`，再原子替换包含 revision ID、content hash 和 task file hash 的 `.task_source.json`。任一步崩溃都会留下可检测的 file/metadata mismatch；worker 重启或显式 cache recovery 会从数据库 current snapshot 重建。缓存失败不会回退已提交的 activation，也不会阻塞其他 outbox；坏缓存会记录错误并在后续恢复中重试。该过程不替换任务目录，也不删除 `raw/`、prompt 或其他相对路径资源。

control loader 直接查询数据库 current revision 并校验 immutable snapshot，不读取兼容缓存内容。它以 snapshot 中的 YAML 作为 `TaskConfig.raw`，同时把 `TaskConfig.path` 设为调用方提供的稳定逻辑路径 `tasks_root/<task_key>/task.yaml`。因此 `input.path` 及未来相对 task 路径字段继续相对同一任务目录解析；snapshot 物理路径只用于 provenance 和完整性校验，不改变运行时基址。

## Operation/status 契约

`task.publish` 保持 #51 的成功回放语义：首次 claim 和成功 replay 都返回原始 `202 task_publish_v1` body，其中的 `materialization_state` 不被后台 worker 改写。

实时结果通过 materialization ID 单独读取 `task_materialization_status_v1`：

- outbox state：`pending`、`processing`、`succeeded`、`failed`
- activation state：`awaiting_activation`、`active`、`superseded`、`not_ready`
- lease/attempt/error、确定性 snapshot 路径和 snapshot 校验结果

短同步 drain 只在给定超时内协助领取/恢复该 materialization 并轮询独立 status，不改变 publish operation 的 `202` body。

## 故障矩阵

| 故障点 | 崩溃后状态 | 恢复动作 | 不变量 |
| --- | --- | --- | --- |
| 领取后、snapshot 提交前崩溃 | DB `processing`，目标不存在 | lease 超时后新 attempt 重写 staging | current 不变；旧 attempt 被 fencing |
| `task.yaml` 只写一半时崩溃 | 半文件只存在于唯一 staging | 新 attempt 忽略旧 staging并重新生成 | 半文件不可被 loader 看见 |
| 原子 rename 后、ready 提交前崩溃 | 完整 orphan snapshot，DB `processing` | lease 回收后逐项校验并复用目标 | 不覆盖目标；相异即失败 |
| ready 提交后、激活前崩溃 | DB `succeeded`，current 仍旧 | worker 扫描 ready-but-not-active 并激活 | ready 与 activation 可独立恢复 |
| 两个 worker 同时领取 | 每行只有一个有效 lease/attempt | `SKIP LOCKED` 领取不同工作；目标锁处理重复写 | 同一 attempt 只有一个数据库完成者 |
| revision 2 先于 revision 1 ready | revision 2 先激活 | revision 1 后续变为 `superseded` | current revision number 单调递增 |
| 目标已有不同或不完整内容 | DB 进入 `failed` | 人工调查，不自动覆盖 | 旧 active revision 继续执行 |
| 激活后兼容缓存刷新崩溃 | DB current 已更新，task/metadata 可能不匹配 | metadata file hash 检出 mismatch，worker 从 DB current 重建 | loader 始终读取 immutable snapshot |
