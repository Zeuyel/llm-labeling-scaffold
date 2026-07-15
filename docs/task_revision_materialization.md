# Task revision 物化状态机

本文定义数据库 task revision 到本地 immutable snapshot 的持久化 outbox 协议。数据库是 revision 与 current revision 的权威来源；`tasks/<task_key>/task.yaml` 仅为兼容缓存，不参与 current revision 判定。

## Schema 决策

沿用 `20260714_0002`，不修改该迁移，也不新增 `0003`。现有 `task_revision_materializations` 已提供状态、可领取时间、lease、worker、attempt 和错误字段；snapshot 路径可由 revision UUID 与 `content_hash` 确定性推导，不需要持久化路径列。

## 数据库状态机

| 当前状态 | 条件/动作 | 下一状态 | 持久化保证 |
| --- | --- | --- | --- |
| `pending` | `available_at <= now`，worker 在事务中领取 | `processing` | PostgreSQL 使用 `FOR UPDATE SKIP LOCKED`；`now` 取自数据库时钟，记录 worker、lease，并递增 `attempt_count` fencing token |
| `processing` | lease 超时后被新 worker 回收 | `processing` | 新 attempt 覆盖 lease；旧 attempt 不能完成数据库状态转换 |
| `processing` | snapshot 原子提交并复核成功 | `succeeded` | ready 状态单独提交；不依赖 `after_commit` callback |
| `processing` | 可重试错误且未达上限 | `pending` | 保存 `last_error`，按 `available_at` 延迟重试 |
| `processing` | 内容冲突、校验失败或达到上限 | `failed` | 保留旧 `tasks.current_revision_id`，不激活失败 revision |
| `succeeded` | 独立事务锁定 task 行，candidate revision 更大 | `succeeded` | 单调更新 `current_revision_id`，同事务追加 activation audit |
| `succeeded` | current revision 相同或更大 | `succeeded` | 幂等 no-op；较旧 revision 标记为 status 层的 `superseded`，不能回退 current |

`attempt_count` 是 lease fencing token。worker 完成 ready 或失败转换时必须同时匹配 materialization ID、`processing`、`claimed_by` 和领取时的 attempt；过期 worker 即使继续运行，也不能提交数据库状态。领取、过期判断、ready 时间和 retry 可用时间均以数据库事务读取的时钟为基准。旧 attempt 丢失 lease 时返回独立的 `lease_lost` 结果，不得误报为 `retry_scheduled`。

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

snapshot root 与 compatibility cache 使用同一 pinned directory capability 模型：从文件系统 anchor 开始逐组件 `openat(O_DIRECTORY | O_NOFOLLOW)`，缺失组件通过父 fd 创建并 fsync。`.locks`、`.staging`、revision UUID、content hash 和唯一 staging 目录都必须是 real directory；revision lock 和三个 snapshot 文件都必须是 regular file。root、锁链和目录 inode 在文件写入、读取、rename 与 fsync 前后持续复核，symlink 或运行中交换均 fail closed。snapshot regular file 在读取前必须以 `fstat` 等于数据库 expected bytes 的精确长度，读取最多 expected length 加一个 EOF probe，并在读取后复核长度和 inode。

worker 在同一 snapshot root 内写唯一 staging 目录，三个文件以 `O_CREAT | O_EXCL | O_NOFOLLOW` 创建并 fsync。提交使用 Linux `renameat2(RENAME_NOREPLACE)` 在 pinned `.staging` 与 revision directory fd 之间执行原子不可覆盖 rename；并发出现的空目录、symlink 或不同内容都只会触发一致性校验失败，不会被替换。提交后再次通过目录 fd 枚举精确文件集、打开 regular files 并校验全部 bytes/JSON/YAML，再允许 ready 状态提交。

## 激活与兼容缓存

snapshot ready 后，worker 固定 root/revision/content hash 目录和三个 snapshot file fd，重新校验精确文件集、长度与内容，并在同一 authority 生命周期内开启数据库事务锁定 task 行：

1. current 为空时允许激活。
2. candidate `revision_number` 大于 current 时允许激活。
3. candidate 等于 current 时幂等返回。
4. candidate 小于 current 时返回 `superseded`，禁止回退。

更新 `tasks.current_revision_id` 与 `task.revision_activated` audit 在同一事务提交。提交前、提交后以及 pinned authority 退出前都再次复核目录、三个文件 inode、精确长度和 bytes；`before_activation` 或提交窗口内的替换在 current 更新前 fail closed，提交后才检测到时仅在 current 仍指向该 revision 的条件下恢复 previous current，并把损坏 materialization 标记为 `failed`。随后 worker 再从数据库 current revision 对应 snapshot 刷新 `tasks/<task_key>/task.yaml` 与 `.task_source.json`；刷新时再次锁定 task 行，因此乱序 worker 只能写当时数据库 current 对应内容。

cache root 从文件系统 anchor 开始逐 path component 以 parent directory fd、`O_DIRECTORY | O_NOFOLLOW` 打开，缺失组件只通过 `mkdirat` 语义创建并 fsync 父目录。完整组件链的 fd 与 inode 在锁获取、task 写入、`os.replace` 与文件/目录 fsync 前后持续复核；root、任一祖先组件的 symlink 或运行中交换都 fail closed。

`_locks`、`task_materialization` 和 task cache 目录都相对 pinned root/parent fd 创建并固定 inode。lock file 只允许 regular file，以 `O_NOFOLLOW` 打开；其目录链与文件 inode 在 flock 持有期间纳入每次 task/metadata 读写、replace 和 fsync 的路径复核。预置 symlink 或运行中目录交换都不能把 lock、task 或 metadata 写到替代目标。两个 cache 目标文件也必须是普通文件或不存在；匹配检查在读取前要求 task 和 metadata 等于本次 expected bytes 长度，oversized 或 sparse 文件直接视为 mismatch 并原子重建。

兼容缓存是可恢复缓存，不是原子目录 snapshot。worker 先原子替换 `task.yaml`，再原子替换包含 revision ID、content hash 和 task file hash 的 `.task_source.json`。任一步崩溃都会留下可检测的 file/metadata mismatch；worker 重启、空闲扫描、显式 cache recovery 或对 terminal materialization 的 one-shot/drain 重跑都会从数据库 current snapshot 重建。普通刷新错误进入 worker 的待修复队列，并在下一次可恢复运行中重试。缓存失败不会回退已提交的 activation，也不会阻塞其他 outbox。该过程不替换任务目录，也不删除 `raw/`、prompt 或其他相对路径资源。

control loader 直接查询数据库 current revision，并在 pinned snapshot fd 生命周期内校验 immutable snapshot，不读取兼容缓存内容，也不会在 `validate()` 后按物理路径重新打开文件。它以已验证的内存 YAML 作为 `TaskConfig.raw`，同时把 `TaskConfig.path` 设为调用方提供的稳定逻辑路径 `tasks_root/<task_key>/task.yaml`。因此 `input.path` 及未来相对 task 路径字段继续相对同一任务目录解析；snapshot 物理路径只用于 provenance，不改变运行时基址。

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
| snapshot root、`.locks`、`.staging` 或 revision/hash 路径是 symlink | DB 进入 `failed` | 人工移除攻击路径后重新发布或受控恢复 | 不向 symlink 目标写 lock、staging 或 snapshot 文件 |
| snapshot root/lock/staging/revision 在运行中被交换 | inode 复核失败，DB 不进入 ready | 保留旧 active revision并调查文件系统 | pinned fd 不跟随替代路径；外部目标零写入 |
| rename 前并发创建空 target | `RENAME_NOREPLACE` 返回冲突 | 校验现有 target；不完整则 fail closed | 空目录 inode 与内容保持不变，不被 staging 覆盖 |
| validate/read 期间 revision 或文件被交换为 symlink | loader/status 校验失败 | 人工恢复 immutable snapshot | 不按路径重开，不读取 symlink 内容 |
| `before_activation` 或 activation 提交窗口替换 snapshot file | candidate 进入 `failed`；若已提交则条件恢复 previous current | 调查 immutable snapshot 后重新发布 | 损坏 revision 不保持 `succeeded/current`，旧 active revision 继续执行 |
| snapshot/cache regular file 被替换为 oversized sparse file | snapshot 校验失败或 cache mismatch 后重建 | snapshot 需人工恢复；cache 自动恢复 | `fstat` 在任何内容读取前拒绝，不按攻击文件长度分配内存 |
| 激活后兼容缓存刷新崩溃 | DB current 已更新，task/metadata 可能不匹配 | terminal operation 重跑、worker 待修复队列或空闲扫描从 DB current 重建 | loader 始终读取 immutable snapshot |
| 缓存写入期间 task 目录被交换 | 已固定 fd 仍指向原 inode，路径入口已变化 | inode 复核失败并停止后续替换；恢复时重新打开当前安全目录 | 不跟随 symlink，不向交换后的目录写入 |
| cache root 或 `_locks` 在锁/写入期间被交换 | pinned fd 指向原 inode，公开路径已变化 | root/lock inode 复核失败并返回 cache refresh failure；移除攻击后 terminal 重跑 | activation 不回退，不向 symlink 目标写 lock/task/metadata，不误报 refreshed |
