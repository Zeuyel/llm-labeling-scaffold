from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import hmac
import ipaddress
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

from .auth.cloudflare_access import (
    CloudflareAccessVerifier,
    JwksUnavailableError,
    TokenForbiddenError,
    TokenVerificationError,
)


class McpConfigurationError(ValueError):
    pass


class PanelApiError(RuntimeError):
    pass


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _bounded_float_env(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = str(os.environ.get(name, "")).strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise McpConfigurationError(f"{name} 必须是数字") from exc
    if not math.isfinite(value) or value < minimum or value > maximum:
        raise McpConfigurationError(f"{name} 必须在 {minimum} 到 {maximum} 之间")
    return value


def resolve_mcp_auth_mode(explicit: str | None = None) -> str:
    mode = str(explicit or os.environ.get("LLS_MCP_AUTH_MODE") or "cloudflare_access").strip().lower()
    if mode not in {"cloudflare_access", "static_dev"}:
        raise McpConfigurationError("LLS_MCP_AUTH_MODE 只能是 cloudflare_access 或 static_dev")
    return mode


@dataclass(frozen=True, slots=True)
class McpRequestContext:
    authentication_method: str
    issuer: str
    subject: str
    access_assertion: str | None = field(default=None, repr=False, compare=False)

    @property
    def identity_key(self) -> tuple[str, str]:
        return self.issuer, self.subject


_MCP_REQUEST_CONTEXT: ContextVar[McpRequestContext | None] = ContextVar(
    "lls_mcp_request_context",
    default=None,
)


def current_mcp_request_context() -> McpRequestContext | None:
    return _MCP_REQUEST_CONTEXT.get()


@dataclass(frozen=True)
class McpServerConfig:
    panel_url: str
    bearer_token: str | None = field(default=None, repr=False)
    internal_token: str = field(default="", repr=False)
    auth_mode: str = "cloudflare_access"
    cloudflare_issuer: str | None = None
    cloudflare_audience: str | None = field(default=None, repr=False)
    panel_cloudflare_audience: str | None = field(default=None, repr=False)
    cloudflare_jwks_ttl_seconds: float = 300.0
    cloudflare_http_timeout_seconds: float = 5.0
    cloudflare_clock_skew_seconds: float = 0.0
    enable_writes: bool = False
    timeout_seconds: float = 15.0

    @classmethod
    def from_env(
        cls,
        *,
        panel_url: str | None = None,
        auth_mode: str | None = None,
        bearer_token: str | None = None,
        internal_token: str | None = None,
        cloudflare_issuer: str | None = None,
        cloudflare_audience: str | None = None,
        enable_writes: bool | None = None,
        timeout_seconds: float | None = None,
    ) -> "McpServerConfig":
        resolved_url = str(panel_url or os.environ.get("LLS_MCP_PANEL_URL") or "http://127.0.0.1:8765").strip().rstrip("/")
        parsed = urlparse(resolved_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise McpConfigurationError("LLS_MCP_PANEL_URL 必须是完整的 HTTP(S) Panel 地址")
        resolved_auth_mode = resolve_mcp_auth_mode(auth_mode)
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
            auth_mode=resolved_auth_mode,
            cloudflare_issuer=(
                str(cloudflare_issuer or os.environ.get("LLS_MCP_CF_ACCESS_ISSUER") or "").strip() or None
            ),
            cloudflare_audience=(
                str(cloudflare_audience or os.environ.get("LLS_MCP_CF_ACCESS_AUD") or "").strip() or None
            ),
            panel_cloudflare_audience=(
                str(os.environ.get("LLS_CF_ACCESS_AUD") or "").strip() or None
            ),
            cloudflare_jwks_ttl_seconds=_bounded_float_env(
                "LLS_MCP_CF_ACCESS_JWKS_TTL_SECONDS",
                300,
                30,
                3600,
            ),
            cloudflare_http_timeout_seconds=_bounded_float_env(
                "LLS_MCP_CF_ACCESS_HTTP_TIMEOUT_SECONDS",
                5,
                0.5,
                15,
            ),
            cloudflare_clock_skew_seconds=_bounded_float_env(
                "LLS_MCP_CF_ACCESS_CLOCK_SKEW_SECONDS",
                0,
                0,
                60,
            ),
            enable_writes=resolved_writes,
            timeout_seconds=resolved_timeout,
        )


class PanelApiClient:
    def __init__(self, config: McpServerConfig, *, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self._authorization = f"Bearer {config.internal_token}"
        self._client = httpx.AsyncClient(
            base_url=config.panel_url,
            headers={
                "Authorization": self._authorization,
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
        request_headers = httpx.Headers(headers or {})
        request_headers["Authorization"] = self._authorization
        request_headers.pop("Cf-Access-Jwt-Assertion", None)
        context = current_mcp_request_context()
        access_assertion = context.access_assertion if context is not None else None
        if resolve_mcp_auth_mode(self.config.auth_mode) == "cloudflare_access" and (
            context is None
            or context.authentication_method != "cloudflare_access"
            or not access_assertion
        ):
            raise PanelApiError("Managed MCP 请求缺少 request-scoped Access assertion")
        if access_assertion:
            request_headers["Cf-Access-Jwt-Assertion"] = access_assertion
        try:
            response = await self._client.request(
                method,
                path,
                params=params or None,
                json=payload,
                headers=request_headers,
            )
        except httpx.RequestError as exc:
            details = self._redact(str(exc), access_assertion)
            raise PanelApiError(f"无法连接 Panel API: {details}") from exc
        raw = response.text
        if response.is_error:
            details = self._redact(self._error_details(raw), access_assertion)
            raise PanelApiError(f"Panel API {response.status_code}: {details}")
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

    def _redact(self, value: str, access_assertion: str | None = None) -> str:
        redacted = value
        for secret in (self.config.internal_token, self.config.bearer_token, access_assertion):
            if secret:
                redacted = redacted.replace(secret, "[REDACTED]")
        return redacted


class McpAuthenticationMiddleware:
    def __init__(
        self,
        app,
        config: McpServerConfig,
        *,
        cloudflare_verifier: CloudflareAccessVerifier | None = None,
    ):
        self.app = app
        self.config = config
        self.auth_mode = resolve_mcp_auth_mode(config.auth_mode)
        self._cloudflare_verifier = cloudflare_verifier

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        sanitized_scope = _without_sensitive_auth_headers(scope)
        if scope.get("path") == "/healthz":
            await self.app(sanitized_scope, receive, send)
            return

        if self.auth_mode == "static_dev":
            if not self._valid_static_token(scope):
                await _send_auth_error(
                    scope,
                    receive,
                    send,
                    status_code=401,
                    code="invalid_static_dev_token",
                    challenge="Bearer",
                )
                return
            context = McpRequestContext(
                authentication_method="static_dev",
                issuer="urn:lls:mcp-static-dev",
                subject="static-dev",
            )
        else:
            assertions = _header_values(scope, b"cf-access-jwt-assertion")
            if len(assertions) != 1:
                await _send_auth_error(
                    scope,
                    receive,
                    send,
                    status_code=401,
                    code="missing_access_assertion",
                )
                return
            assertion = assertions[0].decode("latin-1").strip()
            verifier = self._cloudflare_verifier
            if verifier is None:
                await _send_auth_error(
                    scope,
                    receive,
                    send,
                    status_code=503,
                    code="authentication_not_configured",
                )
                return
            try:
                identity = await asyncio.to_thread(verifier.verify, assertion)
            except TokenForbiddenError as exc:
                await _send_auth_error(scope, receive, send, status_code=403, code=exc.code)
                return
            except JwksUnavailableError as exc:
                await _send_auth_error(scope, receive, send, status_code=503, code=exc.code)
                return
            except TokenVerificationError as exc:
                await _send_auth_error(scope, receive, send, status_code=401, code=exc.code)
                return
            context = McpRequestContext(
                authentication_method="cloudflare_access",
                issuer=identity.issuer,
                subject=identity.subject,
                access_assertion=assertion,
            )

        context_token = _MCP_REQUEST_CONTEXT.set(context)
        try:
            await self.app(sanitized_scope, receive, send)
        finally:
            _MCP_REQUEST_CONTEXT.reset(context_token)

    def _valid_static_token(self, scope) -> bool:
        headers = _header_values(scope, b"authorization")
        if len(headers) != 1 or self.config.bearer_token is None:
            return False
        parts = headers[0].decode("latin-1").split()
        token = parts[1] if len(parts) == 2 and parts[0].lower() == "bearer" else ""
        return bool(token) and hmac.compare_digest(token, self.config.bearer_token)

def _header_values(scope, name: bytes) -> list[bytes]:
    return [value for key, value in scope.get("headers", []) if key.lower() == name]


def _without_sensitive_auth_headers(scope):
    sanitized = dict(scope)
    sanitized["headers"] = [
        (key, value)
        for key, value in scope.get("headers", [])
        if key.lower() not in {b"authorization", b"cf-access-jwt-assertion"}
    ]
    return sanitized


async def _send_auth_error(
    scope,
    receive,
    send,
    *,
    status_code: int,
    code: str,
    challenge: str | None = None,
) -> None:
    headers = {"WWW-Authenticate": challenge} if challenge else None
    response = JSONResponse(
        {"error": "MCP authentication failed", "code": code},
        status_code=status_code,
        headers=headers,
    )
    await response(scope, receive, send)


def _safe_segment(value: str, label: str) -> str:
    text = str(value or "").strip()
    if (
        not text
        or text == "."
        or ".." in text
        or "/" in text
        or "\\" in text
        or any(ord(char) < 32 for char in text)
    ):
        raise ValueError(f"{label} 必须是单段安全标识符")
    return text


def _path_segment(value: str, label: str) -> str:
    return quote(_safe_segment(value, label), safe="")


def _with_workspace(payload: dict[str, Any], workspace: str) -> dict[str, Any]:
    return {**payload, "workspace": _safe_segment(workspace, "workspace")}


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
    async def scaffold_task_list(workspace: str) -> dict[str, Any]:
        return await client.get("/api/tasks", _with_workspace({}, workspace))

    @server.tool(description="读取已发布任务的字段、标签、流程预设和数据湖摘要。", annotations=READ_ONLY)
    async def scaffold_task_detail(task_id: str, workspace: str) -> dict[str, Any]:
        return await client.get(
            f"/api/tasks/{_path_segment(task_id, '任务编号')}",
            _with_workspace({}, workspace),
        )

    @server.tool(description="读取 Scaffold 控制面中的任务草稿、强 ETag、revision 与发布记录。仅 control 模式可用。", annotations=READ_ONLY)
    async def scaffold_task_draft_detail(task_id: str, workspace: str) -> dict[str, Any]:
        return await client.get(
            "/api/task/control",
            _with_workspace({"task_id": _safe_segment(task_id, "任务编号")}, workspace),
        )

    @server.tool(description="检查任务配置、流程预设和数据湖来源是否可用。该工具不写入数据。", annotations=READ_EXTERNAL)
    async def scaffold_task_check(task_id: str, workspace: str) -> dict[str, Any]:
        return await client.post(
            f"/api/tasks/{_path_segment(task_id, '任务编号')}/check",
            _with_workspace({}, workspace),
        )

    @server.tool(description="列出任务的本地导入资产和审计摘要。", annotations=READ_ONLY)
    async def scaffold_import_list(task_id: str, workspace: str) -> dict[str, Any]:
        return await client.get(
            "/api/task/imports",
            _with_workspace({"task_id": _safe_segment(task_id, "任务编号")}, workspace),
        )

    @server.tool(description="读取一个导入资产的 manifest、字段、依赖和可用性摘要。", annotations=READ_ONLY)
    async def scaffold_import_detail(task_id: str, import_id: str, workspace: str) -> dict[str, Any]:
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
    async def scaffold_data_lake_preview(task_id: str, workspace: str) -> dict[str, Any]:
        return await client.get(
            "/api/task/data_lake",
            _with_workspace({"task_id": _safe_segment(task_id, "任务编号")}, workspace),
        )

    @server.tool(description="对数据湖导入进行 dry-run，返回将要读取的受登记数据对象与导入编号。", annotations=READ_EXTERNAL)
    async def scaffold_data_lake_import_dry_run(
        task_id: str,
        workspace: str,
        import_id: str = "",
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
    async def scaffold_job_status(task_id: str, workspace: str) -> dict[str, Any]:
        return await client.get(
            "/api/jobs",
            _with_workspace({"task_id": _safe_segment(task_id, "任务编号")}, workspace),
        )

    if config.enable_writes:
        @server.tool(description="创建任务草稿。此操作不会发布任务，也不会执行数据导入。", annotations=WRITE_CREATE)
        async def scaffold_task_draft_create(spec: dict[str, Any], workspace: str) -> dict[str, Any]:
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
            workspace: str,
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
            workspace: str,
            confirm: bool = False,
            idempotency_key: str = "",
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
            workspace: str,
            import_id: str = "",
            confirm: bool = False,
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


def create_streamable_http_app(
    config: McpServerConfig,
    *,
    panel_client: Any | None = None,
    cloudflare_verifier: CloudflareAccessVerifier | None = None,
):
    mode = _validate_streamable_http_config(config)
    verifier = cloudflare_verifier
    if mode == "cloudflare_access" and verifier is None:
        try:
            verifier = CloudflareAccessVerifier(
                config.cloudflare_issuer or "",
                config.cloudflare_audience or "",
                jwks_ttl_seconds=config.cloudflare_jwks_ttl_seconds,
                http_timeout_seconds=config.cloudflare_http_timeout_seconds,
                clock_skew_seconds=config.cloudflare_clock_skew_seconds,
            )
        except ValueError as exc:
            raise McpConfigurationError(str(exc)) from exc
    app = create_mcp_server(config, panel_client=panel_client).streamable_http_app()
    return McpAuthenticationMiddleware(app, config, cloudflare_verifier=verifier)


def _validate_streamable_http_config(config: McpServerConfig) -> str:
    mode = resolve_mcp_auth_mode(config.auth_mode)
    internal_token = config.internal_token
    if len(internal_token) < 32 or any(char.isspace() for char in internal_token):
        raise McpConfigurationError("MCP 必须配置至少 32 个非空白字符的 LLS_MCP_INTERNAL_TOKEN")
    if mode == "cloudflare_access":
        if not config.cloudflare_issuer or not config.cloudflare_audience:
            raise McpConfigurationError(
                "cloudflare_access 模式必须设置 LLS_MCP_CF_ACCESS_ISSUER 和 LLS_MCP_CF_ACCESS_AUD"
            )
        if (
            config.panel_cloudflare_audience
            and config.cloudflare_audience == config.panel_cloudflare_audience
        ):
            raise McpConfigurationError(
                "LLS_MCP_CF_ACCESS_AUD 必须与 LLS_CF_ACCESS_AUD 使用不同的 Access application AUD"
            )
        return mode
    token = config.bearer_token or ""
    if len(token) < 32 or any(char.isspace() for char in token):
        raise McpConfigurationError(
            "static_dev 模式必须配置至少 32 个非空白字符的 LLS_MCP_BEARER_TOKEN"
        )
    if hmac.compare_digest(token, config.internal_token):
        raise McpConfigurationError("LLS_MCP_BEARER_TOKEN 不能与 LLS_MCP_INTERNAL_TOKEN 相同")
    return mode


def _is_loopback_host(host: str) -> bool:
    normalized = str(host or "").strip().lower().removeprefix("[").removesuffix("]")
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def serve_mcp(
    config: McpServerConfig,
    *,
    transport: str,
    host: str,
    port: int,
    published_host: str | None = None,
) -> None:
    if transport == "stdio":
        create_mcp_server(config).run(transport="stdio")
        return
    if transport != "streamable-http":
        raise McpConfigurationError(f"不支持的 MCP transport: {transport}")
    exposure_host = published_host or host
    if not _is_loopback_host(exposure_host):
        mode = resolve_mcp_auth_mode(config.auth_mode)
        if mode == "static_dev":
            raise McpConfigurationError("static_dev 模式只能监听回环地址")
        raise McpConfigurationError("cloudflare_access 模式必须通过 Tunnel-only 回环源站发布 MCP")
    uvicorn.run(create_streamable_http_app(config), host=host, port=port, log_level="info")
