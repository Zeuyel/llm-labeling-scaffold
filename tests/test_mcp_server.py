from __future__ import annotations

import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from typing import Any

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from llm_labeling_scaffold.mcp_server import BearerTokenMiddleware, McpConfigurationError, McpServerConfig, PanelApiClient, create_mcp_server, create_streamable_http_app


class FakePanelClient:
    def __init__(self, delay: float = 0):
        self.calls: list[tuple[str, str, dict[str, Any] | None, dict[str, str] | None]] = []
        self.delay = delay

    async def _wait(self) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)

    async def get(self, path: str, query: dict[str, Any] | None = None) -> dict[str, Any]:
        await self._wait()
        self.calls.append(("GET", path, query, None))
        return {"method": "GET", "path": path, "query": query or {}}

    async def post(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        await self._wait()
        self.calls.append(("POST", path, payload, headers))
        return {"method": "POST", "path": path, "payload": payload or {}}

    async def put(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        await self._wait()
        self.calls.append(("PUT", path, payload, headers))
        return {"method": "PUT", "path": path, "payload": payload}


@contextmanager
def _panel_api_server():
    class Handler(BaseHTTPRequestHandler):
        received: dict[str, Any] = {}

        def do_GET(self):
            Handler.received = {
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
            }
            body = json.dumps({"ok": True}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            return

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", Handler
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


MCP_TOKEN = "mcp-secret-0123456789-abcdef-012345"
INTERNAL_TOKEN = "mcp-internal-0123456789-abcdef-012"
DRAFT_ETAG = '"task-draft-v3-' + "a" * 64 + '"'


def _config(*, bearer_token: str | None = MCP_TOKEN, enable_writes: bool = False) -> McpServerConfig:
    return McpServerConfig(
        panel_url="http://panel:8765",
        bearer_token=bearer_token,
        internal_token=INTERNAL_TOKEN,
        enable_writes=enable_writes,
    )


def test_panel_api_client_uses_scoped_bearer_auth_and_json_queries():
    async def run(base_url: str):
        client = PanelApiClient(
            McpServerConfig(
                panel_url=base_url,
                bearer_token=MCP_TOKEN,
                internal_token=INTERNAL_TOKEN,
            )
        )
        try:
            return await client.get("/api/tasks", {"task_id": "task_a", "empty": ""})
        finally:
            await client.aclose()

    with _panel_api_server() as (base_url, handler):
        result = asyncio.run(run(base_url))

    assert result == {"ok": True}
    assert handler.received == {
        "path": "/api/tasks?task_id=task_a",
        "authorization": f"Bearer {INTERNAL_TOKEN}",
    }


def test_mcp_server_is_read_only_by_default_and_declares_annotations():
    server = create_mcp_server(_config(), panel_client=FakePanelClient())
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}

    assert "scaffold_task_list" in tools
    assert tools["scaffold_task_list"].annotations.readOnlyHint is True
    assert "scaffold_task_publish" not in tools
    assert "scaffold_data_lake_import_submit" not in tools
    assert "argilla_push" not in tools
    assert "delete_task" not in tools


def test_all_task_scoped_mcp_tools_forward_workspace():
    panel = FakePanelClient()
    server = create_mcp_server(_config(), panel_client=panel)

    asyncio.run(server.call_tool("scaffold_task_detail", {"task_id": "task-a", "workspace": "workspace-a"}))
    assert panel.calls[-1] == (
        "GET",
        "/api/tasks/task-a",
        {"workspace": "workspace-a"},
        None,
    )
    asyncio.run(
        server.call_tool(
            "scaffold_task_draft_detail",
            {"task_id": "task-a", "workspace": "workspace-a"},
        ),
    )
    assert panel.calls[-1] == (
        "GET",
        "/api/task/control",
        {"task_id": "task-a", "workspace": "workspace-a"},
        None,
    )
    asyncio.run(server.call_tool("scaffold_task_check", {"task_id": "task-a", "workspace": "workspace-a"}))
    assert panel.calls[-1] == (
        "POST",
        "/api/tasks/task-a/check",
        {"workspace": "workspace-a"},
        None,
    )
    asyncio.run(server.call_tool("scaffold_import_list", {"task_id": "task-a", "workspace": "workspace-a"}))
    assert panel.calls[-1] == (
        "GET",
        "/api/task/imports",
        {"task_id": "task-a", "workspace": "workspace-a"},
        None,
    )
    asyncio.run(
        server.call_tool(
            "scaffold_import_detail",
            {"task_id": "task-a", "import_id": "import-a", "workspace": "workspace-a"},
        ),
    )
    assert panel.calls[-1] == (
        "GET",
        "/api/import/detail",
        {"task_id": "task-a", "import_id": "import-a", "workspace": "workspace-a"},
        None,
    )
    asyncio.run(
        server.call_tool(
            "scaffold_data_lake_preview",
            {"task_id": "task-a", "workspace": "workspace-a"},
        ),
    )
    assert panel.calls[-1] == (
        "GET",
        "/api/task/data_lake",
        {"task_id": "task-a", "workspace": "workspace-a"},
        None,
    )
    asyncio.run(
        server.call_tool(
            "scaffold_data_lake_import_dry_run",
            {"task_id": "task-a", "workspace": "workspace-a"},
        ),
    )
    assert panel.calls[-1] == (
        "POST",
        "/api/import/data_lake",
        {"task_id": "task-a", "import_id": "", "dry_run": True, "workspace": "workspace-a"},
        None,
    )
    asyncio.run(server.call_tool("scaffold_job_status", {"task_id": "task-a", "workspace": "workspace-a"}))
    assert panel.calls[-1] == (
        "GET",
        "/api/jobs",
        {"task_id": "task-a", "workspace": "workspace-a"},
        None,
    )


def test_mcp_write_tools_proxy_etag_confirmation_and_idempotency():
    panel = FakePanelClient()
    server = create_mcp_server(_config(enable_writes=True), panel_client=panel)

    asyncio.run(server.call_tool("scaffold_task_list", {"workspace": "workspace-a"}))
    assert panel.calls[-1] == ("GET", "/api/tasks", {"workspace": "workspace-a"}, None)
    asyncio.run(server.call_tool("scaffold_task_list", {}))
    assert panel.calls[-1] == ("GET", "/api/tasks", None, None)

    spec = {
        "task_id": "task-a",
        "text_fields": ["title"],
        "primary_label_values": ["yes", "no"],
    }
    asyncio.run(
        server.call_tool(
            "scaffold_task_draft_create",
            {"spec": spec, "workspace": "workspace-a"},
        ),
    )
    assert panel.calls[-1] == (
        "POST",
        "/api/tasks",
        {**spec, "workspace": "workspace-a"},
        None,
    )
    asyncio.run(
        server.call_tool(
            "scaffold_task_draft_update",
            {
                "task_id": "task-a",
                "spec": spec,
                "expected_draft_etag": DRAFT_ETAG,
                "workspace": "workspace-a",
            },
        ),
    )
    assert panel.calls[-1] == (
        "PUT",
        "/api/tasks/task-a",
        {**spec, "workspace": "workspace-a"},
        {"If-Match": DRAFT_ETAG},
    )

    asyncio.run(
        server.call_tool(
            "scaffold_task_publish",
            {
                "task_id": "任务 a",
                "expected_draft_etag": DRAFT_ETAG,
                "workspace": "workspace-a",
                "reason": "MCP controlled release",
                "confirm": True,
                "idempotency_key": "publish-001",
            },
        )
    )
    assert panel.calls[-1] == (
        "POST",
        "/api/tasks/%E4%BB%BB%E5%8A%A1%20a/publish",
        {
            "confirm": True,
            "idempotency_key": "publish-001",
            "reason": "MCP controlled release",
            "workspace": "workspace-a",
        },
        {"If-Match": DRAFT_ETAG},
    )

    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    publish_properties = tools["scaffold_task_publish"].inputSchema["properties"]
    assert "expected_draft_etag" in publish_properties
    assert "expected_draft_fingerprint" not in publish_properties

    with pytest.raises(ToolError, match="expected_draft_etag"):
        asyncio.run(
            server.call_tool(
                "scaffold_task_publish",
                {
                    "task_id": "task-a",
                    "expected_draft_etag": "a" * 64,
                    "reason": "invalid etag",
                    "confirm": True,
                    "idempotency_key": "publish-invalid-etag",
                },
            ),
        )

    with pytest.raises(ToolError, match="confirm=true"):
        asyncio.run(
            server.call_tool(
                "scaffold_data_lake_import_submit",
                {"task_id": "task_a", "idempotency_key": "import-001"},
            )
        )

    asyncio.run(
        server.call_tool(
            "scaffold_data_lake_import_submit",
            {
                "task_id": "task-a",
                "workspace": "workspace-a",
                "confirm": True,
                "idempotency_key": "import-001",
            },
        ),
    )
    assert panel.calls[-1] == (
        "POST",
        "/api/import/data_lake",
        {
            "task_id": "task-a",
            "import_id": "",
            "confirm": True,
            "idempotency_key": "import-001",
            "workspace": "workspace-a",
        },
        None,
    )


def test_mcp_tools_do_not_serialize_concurrent_panel_requests():
    server = create_mcp_server(_config(), panel_client=FakePanelClient(delay=0.1))

    async def run():
        started = time.perf_counter()
        await asyncio.gather(
            server.call_tool("scaffold_task_list", {}),
            server.call_tool("scaffold_task_list", {}),
        )
        return time.perf_counter() - started

    assert asyncio.run(run()) < 0.17


def test_streamable_http_requires_a_bearer_token():
    app = create_streamable_http_app(_config(), panel_client=FakePanelClient())

    async def request(path: str, headers: dict[str, str] | None = None):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://mcp") as client:
            return await client.post(path, headers=headers) if path == "/mcp" else await client.get(path, headers=headers)

    assert asyncio.run(request("/healthz")).status_code == 200
    assert asyncio.run(request("/mcp")).status_code == 401
    assert asyncio.run(request("/mcp", {"Authorization": "Bearer wrong"})).status_code == 401


def test_bearer_middleware_accepts_valid_tokens_with_normalized_spacing():
    async def downstream(scope, receive, send):
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = BearerTokenMiddleware(downstream, MCP_TOKEN)

    async def request(header: str):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://mcp") as client:
            return await client.post("/mcp", headers={"Authorization": header})

    assert asyncio.run(request(f"bearer  {MCP_TOKEN}")).status_code == 204


def test_streamable_http_rejects_missing_bearer_token_configuration():
    with pytest.raises(McpConfigurationError, match="LLS_MCP_BEARER_TOKEN"):
        create_streamable_http_app(_config(bearer_token=None), panel_client=FakePanelClient())


def test_streamable_http_rejects_reused_internal_token():
    config = McpServerConfig(
        panel_url="http://panel:8765",
        bearer_token=MCP_TOKEN,
        internal_token=MCP_TOKEN,
    )

    with pytest.raises(McpConfigurationError, match="不能与"):
        create_streamable_http_app(config, panel_client=FakePanelClient())


def test_mcp_config_repr_does_not_expose_tokens():
    text = repr(_config())
    assert MCP_TOKEN not in text
    assert INTERNAL_TOKEN not in text
