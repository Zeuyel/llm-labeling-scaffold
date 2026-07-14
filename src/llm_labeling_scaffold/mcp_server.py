from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import hmac
import json
import math
import os
from typing import Any
from urllib.parse import quote, urlparse

import httpx
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse


class McpConfigurationError(ValueError):
    pass


class PanelApiError(RuntimeError):
    pass


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class McpServerConfig:
    panel_url: str
    bearer_token: str | None = field(repr=False)
    internal_token: str = field(repr=False)
    enable_writes: bool = False
    timeout_seconds: float = 15.0

    @classmethod
    def from_env(
        cls,
        *,
        panel_url: str | None = None,
        bearer_token: str | None = None,
        internal_token: str | None = None,
        enable_writes: bool | None = None,
        timeout_seconds: float | None = None,
    ) -> "McpServerConfig":
        resolved_url = str(panel_url or os.environ.get("LLS_MCP_PANEL_URL") or "http://127.0.0.1:8765").strip().rstrip("/")
        parsed = urlparse(resolved_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise McpConfigurationError("LLS_MCP_PANEL_URL 必须是完整的 HTTP(S) Panel 地址")
        resolved_token = str(bearer_token or os.environ.get("LLS_MCP_BEARER_TOKEN") or "").strip() or None
        resolved_internal_token = str(internal_token or os.environ.get("LLS_MCP_INTERNAL_TOKEN") or "").strip()
        if len(resolved_internal_token) < 32 or any(char.isspace() for char in resolved_internal_token):
            raise McpConfigurationError("MCP 必须配置至少 32 个非空白字符的 LLS_MCP_INTERNAL_TOKEN")
        try:
            resolved_timeout = timeout_seconds if timeout_seconds is not None else float(os.environ.get("LLS_MCP_TIMEOUT_SECONDS", "15"))
        except ValueError as exc:
            raise McpConfigurationError("LLS_MCP_TIMEOUT_SECONDS 必须是有效数字") from exc
        if not math.isfinite(resolved_timeout) or resolved_timeout <= 0:
            raise McpConfigurationError("LLS_MCP_TIMEOUT_SECONDS 必须是大于 0 的有限数字")
        resolved_writes = _truthy(os.environ.get("LLS_MCP_ENABLE_WRITES")) if enable_writes is None else bool(enable_writes)
        return cls(
            panel_url=resolved_url,
            bearer_token=resolved_token,
            internal_token=resolved_internal_token,
            enable_writes=resolved_writes,
            timeout_seconds=resolved_timeout,
        )


class PanelApiClient:
    def __init__(self, config: McpServerConfig, *, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self._client = httpx.AsyncClient(
            base_url=config.panel_url,
            headers={
                "Authorization": f"Bearer {config.internal_token}",
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(config.timeout_seconds),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    async def get(self, path: str, query: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self.request("GET", path, query=query)

    async def post(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return await self.request("POST", path, payload=payload, headers=headers)

    async def put(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return await self.request("PUT", path, payload=payload, headers=headers)

    async def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if not path.startswith("/"):
            raise ValueError("Panel API 路径必须以 / 开头")
        params = {key: value for key, value in (query or {}).items() if value not in (None, "")}
        try:
            response = await self._client.request(method, path, params=params or None, json=payload, headers=headers)
        except httpx.RequestError as exc:
            raise PanelApiError(f"无法连接 Panel API: {exc}") from exc
        raw = response.text
        if response.is_error:
            raise PanelApiError(f"Panel API {response.status_code}: {self._error_details(raw)}")
        return self._parse_response(raw, response.status_code)

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _parse_response(raw: str, status: int) -> dict[str, Any]:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PanelApiError(f"Panel API {status} 返回了非 JSON 内容") from exc
        if not isinstance(data, dict):
            raise PanelApiError(f"Panel API {status} 返回了非对象 JSON")
        return data

    @staticmethod
    def _error_details(raw: str) -> str:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return raw[:500] or "请求失败"
        if isinstance(payload, dict) and payload.get("error"):
            return str(payload["error"])
        return raw[:500] or "请求失败"


class BearerTokenMiddleware:
    def __init__(self, app, bearer_token: str):
        self.app = app
        self.bearer_token = bearer_token

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope.get("path") != "/healthz":
            header = next((value for key, value in scope.get("headers", []) if key.lower() == b"authorization"), b"")
            parts = header.decode("latin-1").split()
            token = parts[1] if len(parts) == 2 and parts[0].lower() == "bearer" else ""
            if not token or not hmac.compare_digest(token, self.bearer_token):
                response = JSONResponse({"error": "MCP bearer token 无效"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _safe_segment(value: str, label: str) -> str:
    text = str(value or "").strip()
    if not text or ".." in text or "/" in text or "\\" in text or any(ord(char) < 32 for char in text):
        raise ValueError(f"{label} 必须是单段安全标识符")
    return text


def _path_segment(value: str, label: str) -> str:
    return quote(_safe_segment(value, label), safe="")


def _optional_workspace(value: str) -> str:
    text = str(value or "").strip()
    return _safe_segment(text, "workspace") if text else ""


def _with_workspace(payload: dict[str, Any], workspace: str) -> dict[str, Any]:
    value = _optional_workspace(workspace)
    return {**payload, "workspace": value} if value else payload


def _strong_etag(value: str) -> str:
    text = str(value or "").strip()
    if text.startswith("W/") or len(text) < 2 or text[0] != '"' or text[-1] != '"' or "," in text:
        raise ValueError("expected_draft_etag 必须是 GET draft 返回的完整强 ETag")
    return text


def _require_confirmed_idempotency(confirm: bool, idempotency_key: str, action: str) -> str:
    key = str(idempotency_key or "").strip()
    if not confirm:
        raise ValueError(f"{action} 必须显式设置 confirm=true")
    if not key or len(key) > 200 or any(ord(char) < 32 for char in key):
        raise ValueError(f"{action} 必须提供有效 idempotency_key")
    return key


READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
READ_EXTERNAL = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
WRITE_CREATE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
WRITE_UPDATE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)
WRITE_IDEMPOTENT = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
WRITE_EXTERNAL_IDEMPOTENT = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True)


def create_mcp_server(config: McpServerConfig, *, panel_client: Any | None = None) -> FastMCP:
    owns_client = panel_client is None
    client = panel_client or PanelApiClient(config)

    @asynccontextmanager
    async def lifespan(_: FastMCP):
        try:
            yield None
        finally:
            if owns_client:
                await client.aclose()

    server = FastMCP(
        "LLM Labeling Scaffold",
        instructions="通过受控 Panel API 管理任务草稿、数据湖导入和运行状态。不要调用未暴露的删除、归档、Argilla、训练或 R2 管理动作。",
        stateless_http=True,
        streamable_http_path="/mcp",
        lifespan=lifespan,
    )

    @server.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(_: StarletteRequest):
        return JSONResponse({"ok": True, "service": "llm-labeling-scaffold-mcp"})

    @server.tool(description="读取平台健康状态、版本和非敏感运行配置。", annotations=READ_ONLY)
    async def scaffold_platform_status() -> dict[str, Any]:
        health, version, settings = await asyncio.gather(
            client.get("/api/health"),
            client.get("/api/version"),
            client.get("/api/settings/public"),
        )
        return {"health": health, "version": version, "settings": settings}

    @server.tool(description="列出当前可见任务及其发布状态。草稿任务不可执行。", annotations=READ_ONLY)
    async def scaffold_task_list(workspace: str = "") -> dict[str, Any]:
        return await client.get("/api/tasks", _with_workspace({}, workspace) or None)

    @server.tool(description="读取已发布任务的字段、标签、流程预设和数据湖摘要。", annotations=READ_ONLY)
    async def scaffold_task_detail(task_id: str, workspace: str = "") -> dict[str, Any]:
        return await client.get(
            f"/api/tasks/{_path_segment(task_id, '任务编号')}",
            _with_workspace({}, workspace) or None,
        )

    @server.tool(description="读取 Scaffold 控制面中的任务草稿、强 ETag、revision 与发布记录。仅 control 模式可用。", annotations=READ_ONLY)
    async def scaffold_task_draft_detail(task_id: str, workspace: str = "") -> dict[str, Any]:
        return await client.get(
            "/api/task/control",
            _with_workspace({"task_id": _safe_segment(task_id, "任务编号")}, workspace),
        )

    @server.tool(description="检查任务配置、流程预设和数据湖来源是否可用。该工具不写入数据。", annotations=READ_EXTERNAL)
    async def scaffold_task_check(task_id: str, workspace: str = "") -> dict[str, Any]:
        return await client.post(
            f"/api/tasks/{_path_segment(task_id, '任务编号')}/check",
            _with_workspace({}, workspace),
        )

    @server.tool(description="列出任务的本地导入资产和审计摘要。", annotations=READ_ONLY)
    async def scaffold_import_list(task_id: str, workspace: str = "") -> dict[str, Any]:
        return await client.get(
            "/api/task/imports",
            _with_workspace({"task_id": _safe_segment(task_id, "任务编号")}, workspace),
        )

    @server.tool(description="读取一个导入资产的 manifest、字段、依赖和可用性摘要。", annotations=READ_ONLY)
    async def scaffold_import_detail(task_id: str, import_id: str, workspace: str = "") -> dict[str, Any]:
        return await client.get(
            "/api/import/detail",
            _with_workspace(
                {
                    "task_id": _safe_segment(task_id, "任务编号"),
                    "import_id": _safe_segment(import_id, "导入编号"),
                },
                workspace,
            ),
        )

    @server.tool(description="预览任务在数据湖登记表中的来源对象与校验信息，不下载或写入数据。", annotations=READ_EXTERNAL)
    async def scaffold_data_lake_preview(task_id: str, workspace: str = "") -> dict[str, Any]:
        return await client.get(
            "/api/task/data_lake",
            _with_workspace({"task_id": _safe_segment(task_id, "任务编号")}, workspace),
        )

    @server.tool(description="对数据湖导入进行 dry-run，返回将要读取的受登记数据对象与导入编号。", annotations=READ_EXTERNAL)
    async def scaffold_data_lake_import_dry_run(
        task_id: str,
        import_id: str = "",
        workspace: str = "",
    ) -> dict[str, Any]:
        return await client.post(
            "/api/import/data_lake",
            _with_workspace(
                {
                    "task_id": _safe_segment(task_id, "任务编号"),
                    "import_id": import_id.strip(),
                    "dry_run": True,
                },
                workspace,
            ),
        )

    @server.tool(description="读取任务异步 job 的状态和结果摘要。", annotations=READ_ONLY)
    async def scaffold_job_status(task_id: str, workspace: str = "") -> dict[str, Any]:
        return await client.get(
            "/api/jobs",
            _with_workspace({"task_id": _safe_segment(task_id, "任务编号")}, workspace),
        )

    if config.enable_writes:
        @server.tool(description="创建任务草稿。此操作不会发布任务，也不会执行数据导入。", annotations=WRITE_CREATE)
        async def scaffold_task_draft_create(spec: dict[str, Any], workspace: str = "") -> dict[str, Any]:
            if not isinstance(spec, dict):
                raise ValueError("spec 必须是任务单对象")
            task_id = _safe_segment(str(spec.get("task_id") or ""), "任务编号")
            return await client.post(
                "/api/tasks",
                _with_workspace({**spec, "task_id": task_id}, workspace),
            )

        @server.tool(description="更新既有任务草稿。必须提交读取草稿时获得的完整强 ETag；冲突时拒绝覆盖。", annotations=WRITE_UPDATE)
        async def scaffold_task_draft_update(
            task_id: str,
            spec: dict[str, Any],
            expected_draft_etag: str,
            workspace: str = "",
        ) -> dict[str, Any]:
            safe_task_id = _safe_segment(task_id, "任务编号")
            if not isinstance(spec, dict) or str(spec.get("task_id") or "").strip() != safe_task_id:
                raise ValueError("spec.task_id 必须与 task_id 一致")
            return await client.put(
                f"/api/tasks/{_path_segment(safe_task_id, '任务编号')}",
                _with_workspace({**spec, "task_id": safe_task_id}, workspace),
                headers={"If-Match": _strong_etag(expected_draft_etag)},
            )

        @server.tool(description="发布任务草稿为新 revision。必须提交强 ETag、reason、显式确认和稳定 idempotency_key。", annotations=WRITE_IDEMPOTENT)
        async def scaffold_task_publish(
            task_id: str,
            expected_draft_etag: str,
            reason: str,
            confirm: bool = False,
            idempotency_key: str = "",
            workspace: str = "",
        ) -> dict[str, Any]:
            key = _require_confirmed_idempotency(confirm, idempotency_key, "发布任务")
            normalized_reason = str(reason or "").strip()
            if not normalized_reason:
                raise ValueError("发布任务必须提供 reason")
            return await client.post(
                f"/api/tasks/{_path_segment(task_id, '任务编号')}/publish",
                _with_workspace(
                    {
                        "confirm": True,
                        "idempotency_key": key,
                        "reason": normalized_reason,
                    },
                    workspace,
                ),
                headers={"If-Match": _strong_etag(expected_draft_etag)},
            )

        @server.tool(description="提交数据湖导入异步任务。必须显式确认并使用稳定 idempotency_key。", annotations=WRITE_EXTERNAL_IDEMPOTENT)
        async def scaffold_data_lake_import_submit(
            task_id: str,
            idempotency_key: str,
            import_id: str = "",
            confirm: bool = False,
            workspace: str = "",
        ) -> dict[str, Any]:
            key = _require_confirmed_idempotency(confirm, idempotency_key, "数据湖导入")
            return await client.post(
                "/api/import/data_lake",
                _with_workspace(
                    {
                        "task_id": _safe_segment(task_id, "任务编号"),
                        "import_id": import_id.strip(),
                        "confirm": True,
                        "idempotency_key": key,
                    },
                    workspace,
                ),
            )

    return server


def create_streamable_http_app(config: McpServerConfig, *, panel_client: Any | None = None):
    if not config.bearer_token or len(config.bearer_token) < 32 or any(char.isspace() for char in config.bearer_token):
        raise McpConfigurationError("Streamable HTTP MCP 必须配置至少 32 个非空白字符的 LLS_MCP_BEARER_TOKEN")
    if hmac.compare_digest(config.bearer_token, config.internal_token):
        raise McpConfigurationError("LLS_MCP_BEARER_TOKEN 不能与 LLS_MCP_INTERNAL_TOKEN 相同")
    app = create_mcp_server(config, panel_client=panel_client).streamable_http_app()
    return BearerTokenMiddleware(app, config.bearer_token)


def serve_mcp(config: McpServerConfig, *, transport: str, host: str, port: int) -> None:
    if transport == "stdio":
        create_mcp_server(config).run(transport="stdio")
        return
    if transport != "streamable-http":
        raise McpConfigurationError(f"不支持的 MCP transport: {transport}")
    uvicorn.run(create_streamable_http_app(config), host=host, port=port, log_level="info")
