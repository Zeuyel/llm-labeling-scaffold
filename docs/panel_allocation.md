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
