from __future__ import annotations

import base64
from dataclasses import dataclass
import hmac
import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse


class McpConfigurationError(ValueError):
    pass


class PanelApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class McpServerConfig:
    panel_url: str
    panel_user: str
    panel_password: str
    bearer_token: str | None
    internal_token: str
    timeout_seconds: float = 15.0

    @classmethod
    def from_env(
        cls,
        *,
        panel_url: str | None = None,
        panel_user: str | None = None,
        panel_password: str | None = None,
        bearer_token: str | None = None,
        internal_token: str | None = None,
        timeout_seconds: float | None = None,
    ) -> "McpServerConfig":
        resolved_url = str(panel_url or os.environ.get("LLS_MCP_PANEL_URL") or "http://127.0.0.1:8765").strip().rstrip("/")
        parsed = urlparse(resolved_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise McpConfigurationError("LLS_MCP_PANEL_URL 必须是完整的 HTTP(S) Panel 地址")
        resolved_user = str(panel_user or os.environ.get("LLS_MCP_PANEL_USER") or "admin").strip()
        resolved_password = str(panel_password or os.environ.get("LLS_MCP_PANEL_PASSWORD") or os.environ.get("LLS_PANEL_PASSWORD") or "")
        if not resolved_user or not resolved_password:
            raise McpConfigurationError("MCP 连接 Panel 必须配置 LLS_MCP_PANEL_USER 和 LLS_MCP_PANEL_PASSWORD")
        resolved_token = str(bearer_token or os.environ.get("LLS_MCP_BEARER_TOKEN") or "").strip() or None
        resolved_internal_token = str(internal_token or os.environ.get("LLS_MCP_INTERNAL_TOKEN") or "").strip()
        if len(resolved_internal_token) < 32 or any(char.isspace() for char in resolved_internal_token):
            raise McpConfigurationError("MCP 必须配置至少 32 个非空白字符的 LLS_MCP_INTERNAL_TOKEN")
        resolved_timeout = timeout_seconds if timeout_seconds is not None else float(os.environ.get("LLS_MCP_TIMEOUT_SECONDS", "15"))
        if resolved_timeout <= 0:
            raise McpConfigurationError("LLS_MCP_TIMEOUT_SECONDS 必须大于 0")
        return cls(
            panel_url=resolved_url,
            panel_user=resolved_user,
            panel_password=resolved_password,
            bearer_token=resolved_token,
            internal_token=resolved_internal_token,
            timeout_seconds=resolved_timeout,
        )


class PanelApiClient:
    def __init__(self, config: McpServerConfig):
        self.config = config

    def get(self, path: str, query: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.request("GET", path, query=query)

    def post(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.request("POST", path, payload=payload)

    def put(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("PUT", path, payload=payload)

    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not path.startswith("/"):
            raise ValueError("Panel API 路径必须以 / 开头")
        url = f"{self.config.panel_url}{path}"
        if query:
            encoded = urlencode({key: value for key, value in query.items() if value not in (None, "")})
            if encoded:
                url = f"{url}?{encoded}"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        credentials = base64.b64encode(f"{self.config.panel_user}:{self.config.panel_password}".encode("utf-8")).decode("ascii")
        headers = {
            "Authorization": f"Basic {credentials}",
            "Accept": "application/json",
            "X-LLS-MCP-Internal-Token": self.config.internal_token,
            "X-LLS-Actor": "mcp",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
                return self._parse_response(raw, response.status)
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            details = self._error_details(raw)
            raise PanelApiError(f"Panel API {exc.code}: {details}") from exc
        except URLError as exc:
            raise PanelApiError(f"无法连接 Panel API: {exc.reason}") from exc

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
            text = header.decode("latin-1")
            scheme, _, token = text.partition(" ")
            if scheme.lower() != "bearer":
                token = ""
            if not token or not hmac.compare_digest(token, self.bearer_token):
                response = JSONResponse({"error": "MCP bearer token 无效"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _safe_segment(value: str, label: str) -> str:
    text = str(value or "").strip()
    if not text or ".." in text or "/" in text or "\\" in text:
        raise ValueError(f"{label} 必须是单段安全标识符")
    return text


def _require_confirmed_idempotency(confirm: bool, idempotency_key: str, action: str) -> str:
    key = str(idempotency_key or "").strip()
    if not confirm:
        raise ValueError(f"{action} 必须显式设置 confirm=true")
    if not key or len(key) > 200 or any(ord(char) < 32 for char in key):
        raise ValueError(f"{action} 必须提供有效 idempotency_key")
    return key


def create_mcp_server(config: McpServerConfig, *, panel_client: PanelApiClient | None = None) -> FastMCP:
    client = panel_client or PanelApiClient(config)
    server = FastMCP(
        "LLM Labeling Scaffold",
        instructions="通过受控 Panel API 管理任务草稿、数据湖导入和运行状态。不要调用未暴露的删除、归档、Argilla、训练或 R2 管理动作。",
        stateless_http=True,
        streamable_http_path="/mcp",
    )

    @server.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(_: StarletteRequest):
        return JSONResponse({"ok": True, "service": "llm-labeling-scaffold-mcp"})

    @server.tool(description="读取平台健康状态、版本和非敏感运行配置。")
    def scaffold_platform_status() -> dict[str, Any]:
        return {
            "health": client.get("/api/health"),
            "version": client.get("/api/version"),
            "settings": client.get("/api/settings/public"),
        }

    @server.tool(description="列出当前可见任务及其发布状态。草稿任务不可执行。")
    def scaffold_task_list() -> dict[str, Any]:
        return client.get("/api/tasks")

    @server.tool(description="读取已发布任务的字段、标签、流程预设和数据湖摘要。")
    def scaffold_task_detail(task_id: str) -> dict[str, Any]:
        return client.get(f"/api/tasks/{_safe_segment(task_id, '任务编号')}")

    @server.tool(description="读取 Scaffold 控制面中的任务草稿、revision 与发布记录。仅 control 模式可用。")
    def scaffold_task_draft_detail(task_id: str) -> dict[str, Any]:
        return client.get("/api/task/control", {"task_id": _safe_segment(task_id, "任务编号")})

    @server.tool(description="创建任务草稿。此操作不会发布任务，也不会执行数据导入。")
    def scaffold_task_draft_create(spec: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(spec, dict):
            raise ValueError("spec 必须是任务单对象")
        _safe_segment(str(spec.get("task_id") or ""), "任务编号")
        return client.post("/api/tasks", spec)

    @server.tool(description="更新既有任务草稿。任务编号不可变，当前已发布 revision 不会被草稿覆盖。")
    def scaffold_task_draft_update(task_id: str, spec: dict[str, Any]) -> dict[str, Any]:
        safe_task_id = _safe_segment(task_id, "任务编号")
        if not isinstance(spec, dict) or str(spec.get("task_id") or "").strip() != safe_task_id:
            raise ValueError("spec.task_id 必须与 task_id 一致")
        return client.put(f"/api/tasks/{safe_task_id}", spec)

    @server.tool(description="发布任务草稿为新 revision。必须显式确认并使用稳定 idempotency_key。")
    def scaffold_task_publish(task_id: str, confirm: bool = False, idempotency_key: str = "") -> dict[str, Any]:
        safe_task_id = _safe_segment(task_id, "任务编号")
        key = _require_confirmed_idempotency(confirm, idempotency_key, "发布任务")
        return client.post(f"/api/tasks/{safe_task_id}/publish", {"confirm": True, "idempotency_key": key})

    @server.tool(description="检查任务配置、流程预设和数据湖来源是否可用。该工具不写入数据。")
    def scaffold_task_check(task_id: str) -> dict[str, Any]:
        return client.post(f"/api/tasks/{_safe_segment(task_id, '任务编号')}/check", {})

    @server.tool(description="列出任务的本地导入资产和审计摘要。")
    def scaffold_import_list(task_id: str) -> dict[str, Any]:
        return client.get("/api/task/imports", {"task_id": _safe_segment(task_id, "任务编号")})

    @server.tool(description="读取一个导入资产的 manifest、字段、依赖和可用性摘要。")
    def scaffold_import_detail(task_id: str, import_id: str) -> dict[str, Any]:
        return client.get(
            "/api/import/detail",
            {"task_id": _safe_segment(task_id, "任务编号"), "import_id": _safe_segment(import_id, "导入编号")},
        )

    @server.tool(description="预览任务在数据湖登记表中的来源对象与校验信息，不下载或写入数据。")
    def scaffold_data_lake_preview(task_id: str) -> dict[str, Any]:
        return client.get("/api/task/data_lake", {"task_id": _safe_segment(task_id, "任务编号")})

    @server.tool(description="对数据湖导入进行 dry-run，返回将要读取的受登记数据对象与导入编号。")
    def scaffold_data_lake_import_dry_run(task_id: str, import_id: str = "") -> dict[str, Any]:
        return client.post(
            "/api/import/data_lake",
            {"task_id": _safe_segment(task_id, "任务编号"), "import_id": import_id.strip(), "dry_run": True},
        )

    @server.tool(description="提交数据湖导入异步任务。必须显式确认并使用稳定 idempotency_key。")
    def scaffold_data_lake_import_submit(task_id: str, idempotency_key: str, import_id: str = "", confirm: bool = False) -> dict[str, Any]:
        key = _require_confirmed_idempotency(confirm, idempotency_key, "数据湖导入")
        return client.post(
            "/api/import/data_lake",
            {
                "task_id": _safe_segment(task_id, "任务编号"),
                "import_id": import_id.strip(),
                "confirm": True,
                "idempotency_key": key,
            },
        )

    @server.tool(description="读取任务异步 job 的状态和结果摘要。")
    def scaffold_job_status(task_id: str) -> dict[str, Any]:
        return client.get("/api/jobs", {"task_id": _safe_segment(task_id, "任务编号")})

    return server


def create_streamable_http_app(config: McpServerConfig, *, panel_client: PanelApiClient | None = None):
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
