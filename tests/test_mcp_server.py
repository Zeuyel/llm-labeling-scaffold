from __future__ import annotations

import asyncio
import base64
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from typing import Any

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from llm_labeling_scaffold.mcp_server import BearerTokenMiddleware, McpConfigurationError, McpServerConfig, PanelApiClient, create_mcp_server, create_streamable_http_app


class FakePanelClient:
    def __init__(self):
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def get(self, path: str, query: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append(("GET", path, query))
        return {"method": "GET", "path": path, "query": query or {}}

    def post(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append(("POST", path, payload))
        return {"method": "POST", "path": path, "payload": payload or {}}

    def put(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("PUT", path, payload))
        return {"method": "PUT", "path": path, "payload": payload}


@contextmanager
def _panel_api_server():
    class Handler(BaseHTTPRequestHandler):
        received: dict[str, Any] = {}

        def do_GET(self):
            Handler.received = {
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "internal_token": self.headers.get("X-LLS-MCP-Internal-Token"),
                "actor": self.headers.get("X-LLS-Actor"),
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


def _config(*, bearer_token: str | None = MCP_TOKEN) -> McpServerConfig:
    return McpServerConfig(
        panel_url="http://panel:8765",
        panel_user="admin",
        panel_password="panel-secret",
        bearer_token=bearer_token,
        internal_token=INTERNAL_TOKEN,
    )


def test_panel_api_client_uses_panel_basic_auth_and_json_queries():
    with _panel_api_server() as (base_url, handler):
        config = McpServerConfig(
            panel_url=base_url,
            panel_user="admin",
            panel_password="panel-secret",
            bearer_token="mcp-secret",
            internal_token=INTERNAL_TOKEN,
        )
        result = PanelApiClient(config).get("/api/tasks", {"task_id": "task_a", "empty": ""})

    expected = "Basic " + base64.b64encode(b"admin:panel-secret").decode("ascii")
    assert result == {"ok": True}
    assert handler.received == {
        "path": "/api/tasks?task_id=task_a",
        "authorization": expected,
        "internal_token": INTERNAL_TOKEN,
        "actor": "mcp",
    }


def test_mcp_server_registers_only_controlled_tools():
    server = create_mcp_server(_config(), panel_client=FakePanelClient())

    names = {tool.name for tool in asyncio.run(server.list_tools())}

    assert "scaffold_task_list" in names
    assert "scaffold_task_publish" in names
    assert "scaffold_data_lake_import_submit" in names
    assert "argilla_push" not in names
    assert "delete_task" not in names


def test_mcp_tools_proxy_to_panel_and_require_confirmation():
    panel = FakePanelClient()
    server = create_mcp_server(_config(), panel_client=panel)

    asyncio.run(server.call_tool("scaffold_task_list", {}))
    assert panel.calls[-1] == ("GET", "/api/tasks", None)

    asyncio.run(
        server.call_tool(
            "scaffold_task_publish",
            {"task_id": "task_a", "confirm": True, "idempotency_key": "publish-001"},
        )
    )
    assert panel.calls[-1] == (
        "POST",
        "/api/tasks/task_a/publish",
        {"confirm": True, "idempotency_key": "publish-001"},
    )

    with pytest.raises(ToolError, match="confirm=true"):
        asyncio.run(server.call_tool("scaffold_data_lake_import_submit", {"task_id": "task_a", "idempotency_key": "import-001"}))


def test_streamable_http_requires_a_bearer_token():
    panel = FakePanelClient()
    app = create_streamable_http_app(_config(), panel_client=panel)

    async def request(path: str, headers: dict[str, str] | None = None, payload: dict[str, Any] | None = None):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://mcp") as client:
            return await client.post(path, headers=headers, json=payload) if path == "/mcp" else await client.get(path, headers=headers)

    assert asyncio.run(request("/healthz")).status_code == 200
    assert asyncio.run(request("/mcp")).status_code == 401
    assert asyncio.run(request("/mcp", {"Authorization": "Bearer wrong"})).status_code == 401


def test_bearer_middleware_allows_valid_tokens():
    async def downstream(scope, receive, send):
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = BearerTokenMiddleware(downstream, MCP_TOKEN)

    async def request(headers: dict[str, str]):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://mcp") as client:
            return await client.post("/mcp", headers=headers)

    assert asyncio.run(request({"Authorization": f"bearer {MCP_TOKEN}"})).status_code == 204


def test_streamable_http_rejects_missing_bearer_token_configuration():
    try:
        create_streamable_http_app(_config(bearer_token=None), panel_client=FakePanelClient())
    except McpConfigurationError as exc:
        assert "LLS_MCP_BEARER_TOKEN" in str(exc)
    else:
        raise AssertionError("streamable HTTP without a bearer token should fail")


def test_streamable_http_rejects_reused_internal_token():
    config = McpServerConfig(
        panel_url="http://panel:8765",
        panel_user="admin",
        panel_password="panel-secret",
        bearer_token=MCP_TOKEN,
        internal_token=MCP_TOKEN,
    )

    with pytest.raises(McpConfigurationError, match="不能与"):
        create_streamable_http_app(config, panel_client=FakePanelClient())
