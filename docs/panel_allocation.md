# Panel allocation preview

`POST /api/allocation/preview` 是只读的 allocation planner 入口，只在 Scaffold control-plane task source 下开放。请求必须携带明确的 workspace、task 和 revision scope：

```json
{
  "scope": {
    "workspace": "workspace-a",
    "task_id": "task-a",
    "revision_id": "revision-1",
    "revision_hash": "<64 位小写 SHA-256>"
  },
  "request": {
    "strategy": "fixed_partition",
    "source_manifest": {
      "manifest_id": "manifest-1",
      "manifest_hash": "<64 位小写 SHA-256>",
      "kind": "sample"
    },
    "records": [
      {"record_id": "record-1", "content_hash": "<64 位小写 SHA-256>", "batch_id": "batch-1"}
    ],
    "annotators": [
      {"annotator_id": "annotator-1", "cohort_id": "cohort-1", "capacity": 10}
    ],
    "seed": 17,
    "algorithm_version": "allocation-v1",
    "overlap_rules": [],
    "calibration": null
  }
}
```

服务端以已认证 actor/caller 建立权限上下文，并固定要求 `annotation:review` task 权限。请求中的 `actor`、`caller`、`channel`、权限字段、密码、API key、凭证和任何文件路径都会被拒绝；这些值不会进入响应。

`request.task_revision` 可以省略，服务端从 scope 构造 `AllocationRequest.task_revision`；若提供，必须与 scope 完全一致。响应是 `panel_allocation_preview_v1` 白名单 DTO，只包含规划摘要、fingerprint、workspace/dataset requirements、负载、重叠、校准和结构化错误，不包含 seed、原始 records、凭证或 planner 内部 DTO。

该入口只调用纯 `preview_allocation`。它不创建或更新数据库记录，不读 R2，不调用 Argilla，也不进入通用 `/api/action`。权限上下文不可用返回 `503`，权限不足返回 `403`，请求或 planner blocking errors 返回 `422`。

## Plan 控制 API

正式计划只通过以下专用路由操作，不能通过 `/api/action`：

- `POST /api/allocation/plans` 创建 plan。
- `POST /api/allocation/plans/{plan_id}/confirm` 确认 plan。
- `GET /api/allocation/plans?workspace=...` 查询当前用户可见的 plan 列表。
- `GET /api/allocation/plans/{plan_id}?workspace=...` 查询 plan detail。
- `GET /api/allocation/plans/{plan_id}/assignments?workspace=...` 查询 assignment 列表。
- `GET /api/allocation/assignments/{assignment_id}?workspace=...` 查询 assignment detail。
- `GET /api/allocation/plans/{plan_id}/progress?workspace=...` 查询服务端派生的 progress。

create 的 scope 还必须提供 `cohort_revision_id`。create/confirm 必须提供非空的 `Idempotency-Key` header；若 JSON 中同时提供 `idempotency_key`，两者必须完全一致。actor、caller、channel、plan、assignment 和 progress 均由服务端确定或由物化记录读取，调用方提交这些字段会被拒绝。

create/confirm 由 `TrustedAllocationRepository` 负责 ACL、服务端 planner、幂等记录和 audit。plan detail 与 assignment detail 只返回数据库中已持久化的 plan/assignment/binding/receipt，并包含可追溯的 `audit_events`；不返回原始标签、密码或 API key。progress 由 accepted receipt 聚合计算，调用方不能传入或覆盖。

所有 plan/assignment 路由都要求 `annotation:review` task 权限。资源 ID 属于其他 workspace、不可见 task 或不匹配的 workspace selector 统一按资源不存在处理，返回 `404`，不泄漏资源存在性。请求结构错误返回中文 `422`，计划尚未满足确认条件返回中文 `409`，幂等操作冲突返回中文 `409`，授权或数据库服务不可用返回中文 `503`。
