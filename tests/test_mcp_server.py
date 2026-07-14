from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp.server.fastmcp.exceptions import ToolError

from llm_labeling_scaffold import mcp_server
from llm_labeling_scaffold.auth import CloudflareAccessVerifier, Identity
from llm_labeling_scaffold.mcp_server import (
    McpAuthenticationMiddleware,
    McpConfigurationError,
    McpRequestContext,
    McpServerConfig,
    PanelApiClient,
    PanelApiError,
    create_mcp_server,
    create_streamable_http_app,
    current_mcp_request_context,
    serve_mcp,
)


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


MCP_TOKEN = "mcp-secret-0123456789-abcdef-012345"
INTERNAL_TOKEN = "mcp-internal-0123456789-abcdef-012"
OPAQUE_TOKEN = "oauth:opaque-client-token-value"
ISSUER = "https://team.cloudflareaccess.com"
MCP_AUDIENCE = "mcp-application-audience"
PANEL_AUDIENCE = "panel-application-audience"
SUBJECT = "mcp-user-7335d417"
DRAFT_FINGERPRINT = "a" * 64


def _config(
    *,
    auth_mode: str = "static_dev",
    bearer_token: str | None = MCP_TOKEN,
    internal_token: str = INTERNAL_TOKEN,
    cloudflare_issuer: str | None = None,
    cloudflare_audience: str | None = None,
    enable_writes: bool = False,
) -> McpServerConfig:
    return McpServerConfig(
        panel_url="http://panel:8765",
        bearer_token=bearer_token,
        internal_token=internal_token,
        auth_mode=auth_mode,
        cloudflare_issuer=cloudflare_issuer,
        cloudflare_audience=cloudflare_audience,
        enable_writes=enable_writes,
    )


def _managed_config(**kwargs) -> McpServerConfig:
    return _config(
        auth_mode="cloudflare_access",
        cloudflare_issuer=ISSUER,
        cloudflare_audience=MCP_AUDIENCE,
        **kwargs,
    )


def _base64url_uint(value: int) -> str:
    width = (value.bit_length() + 7) // 8
    return jwt.utils.base64url_encode(value.to_bytes(width, "big")).decode("ascii")


def _signing_key(kid: str = "key-1") -> tuple[rsa.RSAPrivateKey, dict[str, str]]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = private_key.public_key().public_numbers()
    return private_key, {
        "kid": kid,
        "kty": "RSA",
        "alg": "RS256",
        "use": "sig",
        "e": _base64url_uint(numbers.e),
        "n": _base64url_uint(numbers.n),
    }


def _assertion(
    private_key: rsa.RSAPrivateKey,
    *,
    kid: str = "key-1",
    audience: str = MCP_AUDIENCE,
    claims: dict[str, Any] | None = None,
    omit: tuple[str, ...] = (),
) -> str:
    now = int(time.time())
    payload = {
        "aud": [audience],
        "exp": now + 300,
        "iat": now - 1,
        "iss": ISSUER,
        "nbf": now - 1,
        "sub": SUBJECT,
        "type": "app",
    }
    payload.update(claims or {})
    for name in omit:
        payload.pop(name, None)
    return jwt.encode(payload, private_key, algorithm="RS256", headers={"kid": kid, "typ": "JWT"})


def _verifier(jwk: dict[str, str]) -> CloudflareAccessVerifier:
    return CloudflareAccessVerifier(
        ISSUER,
        MCP_AUDIENCE,
        jwks_fetcher=lambda *_: {"keys": [jwk]},
    )


async def _request(
    app,
    *,
    path: str = "/mcp",
    method: str = "POST",
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://mcp") as client:
        return await client.request(method, path, headers=headers)


@pytest.fixture
def inline_auth_verification(monkeypatch):
    calls: list[tuple[Any, tuple[Any, ...]]] = []

    async def run(func, *args):
        calls.append((func, args))
        return func(*args)

    monkeypatch.setattr(mcp_server.asyncio, "to_thread", run)
    return calls


def test_panel_api_client_uses_scoped_bearer_auth_and_json_queries():
    received: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        received.update(
            {
                "path": request.url.raw_path.decode("ascii"),
                "authorization": request.headers.get("Authorization"),
            }
        )
        return httpx.Response(200, json={"ok": True})

    async def run():
        client = PanelApiClient(
            McpServerConfig(
                panel_url="http://panel:8765",
                bearer_token=MCP_TOKEN,
                internal_token=INTERNAL_TOKEN,
            ),
            transport=httpx.MockTransport(handler),
        )
        try:
            return await client.request(
                "GET",
                "/api/tasks",
                query={"task_id": "task_a", "empty": ""},
                headers={"Authorization": f"Bearer {OPAQUE_TOKEN}"},
            )
        finally:
            await client.aclose()

    result = asyncio.run(run())

    assert result == {"ok": True}
    assert received == {
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


def test_mcp_write_tools_proxy_fingerprint_confirmation_and_idempotency():
    panel = FakePanelClient()
    server = create_mcp_server(_config(enable_writes=True), panel_client=panel)

    asyncio.run(server.call_tool("scaffold_task_list", {}))
    assert panel.calls[-1] == ("GET", "/api/tasks", None, None)

    asyncio.run(
        server.call_tool(
            "scaffold_task_publish",
            {
                "task_id": "任务 a",
                "expected_draft_fingerprint": DRAFT_FINGERPRINT,
                "confirm": True,
                "idempotency_key": "publish-001",
            },
        )
    )
    assert panel.calls[-1] == (
        "POST",
        "/api/tasks/%E4%BB%BB%E5%8A%A1%20a/publish",
        {"confirm": True, "idempotency_key": "publish-001"},
        {"If-Match": DRAFT_FINGERPRINT},
    )

    with pytest.raises(ToolError, match="confirm=true"):
        asyncio.run(
            server.call_tool(
                "scaffold_data_lake_import_submit",
                {"task_id": "task_a", "idempotency_key": "import-001"},
            )
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


def test_static_dev_streamable_http_requires_a_bearer_token():
    app = create_streamable_http_app(_config(), panel_client=FakePanelClient())

    assert asyncio.run(_request(app, path="/healthz", method="GET")).status_code == 200
    assert asyncio.run(_request(app)).status_code == 401
    assert asyncio.run(_request(app, headers={"Authorization": "Bearer wrong"})).status_code == 401


def test_static_dev_middleware_accepts_valid_tokens_with_normalized_spacing():
    async def downstream(scope, receive, send):
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = McpAuthenticationMiddleware(downstream, _config())

    assert asyncio.run(_request(app, headers={"Authorization": f"bearer  {MCP_TOKEN}"})).status_code == 204


def test_streamable_http_rejects_missing_bearer_token_configuration():
    with pytest.raises(McpConfigurationError, match="LLS_MCP_BEARER_TOKEN"):
        create_streamable_http_app(_config(bearer_token=None), panel_client=FakePanelClient())


def test_streamable_http_rejects_reused_internal_token():
    config = McpServerConfig(
        panel_url="http://panel:8765",
        bearer_token=MCP_TOKEN,
        internal_token=MCP_TOKEN,
        auth_mode="static_dev",
    )

    with pytest.raises(McpConfigurationError, match="不能与"):
        create_streamable_http_app(config, panel_client=FakePanelClient())


def test_mcp_config_repr_does_not_expose_tokens():
    text = repr(_config())
    assert MCP_TOKEN not in text
    assert INTERNAL_TOKEN not in text


def test_cloudflare_access_context_is_request_scoped_and_auth_headers_are_stripped(
    inline_auth_verification,
):
    private_key, jwk = _signing_key()
    assertion = _assertion(private_key)
    observed: dict[str, Any] = {}

    async def downstream(scope, receive, send):
        observed["headers"] = dict(scope["headers"])
        observed["context"] = current_mcp_request_context()
        response = httpx.Response(204)
        await send({"type": "http.response.start", "status": response.status_code, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = McpAuthenticationMiddleware(
        downstream,
        _managed_config(),
        cloudflare_verifier=_verifier(jwk),
    )
    response = asyncio.run(
        _request(
            app,
            headers={
                "Authorization": f"Bearer {OPAQUE_TOKEN}",
                "Cf-Access-Jwt-Assertion": assertion,
            },
        )
    )

    assert response.status_code == 204
    assert b"authorization" not in observed["headers"]
    assert b"cf-access-jwt-assertion" not in observed["headers"]
    context = observed["context"]
    assert isinstance(context, McpRequestContext)
    assert context.authentication_method == "cloudflare_access"
    assert context.identity_key == (ISSUER, SUBJECT)
    assert context.access_assertion == assertion
    assert len(inline_auth_verification) == 1
    assert current_mcp_request_context() is None


def test_cloudflare_access_request_context_does_not_cross_concurrent_requests(
    inline_auth_verification,
):
    class MappingVerifier:
        def verify(self, assertion: str) -> Identity:
            return Identity(issuer=ISSUER, subject=f"subject-{assertion[-1]}")

    async def downstream(scope, receive, send):
        first = current_mcp_request_context()
        await asyncio.sleep(0.05 if first and first.subject.endswith("a") else 0.01)
        second = current_mcp_request_context()
        body = json.dumps(
            {
                "first": list(first.identity_key) if first else None,
                "second": list(second.identity_key) if second else None,
            }
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})

    app = McpAuthenticationMiddleware(
        downstream,
        _managed_config(),
        cloudflare_verifier=MappingVerifier(),
    )

    async def run():
        return await asyncio.gather(
            _request(app, headers={"Cf-Access-Jwt-Assertion": "assertion-a"}),
            _request(app, headers={"Cf-Access-Jwt-Assertion": "assertion-b"}),
        )

    first, second = asyncio.run(run())

    assert first.json() == {
        "first": [ISSUER, "subject-a"],
        "second": [ISSUER, "subject-a"],
    }
    assert second.json() == {
        "first": [ISSUER, "subject-b"],
        "second": [ISSUER, "subject-b"],
    }
    assert len(inline_auth_verification) == 2
    assert current_mcp_request_context() is None


def test_cloudflare_access_rejects_panel_audience_and_never_falls_back_to_static_bearer(
    inline_auth_verification,
):
    private_key, jwk = _signing_key()
    app = McpAuthenticationMiddleware(
        lambda scope, receive, send: None,
        _managed_config(bearer_token=MCP_TOKEN),
        cloudflare_verifier=_verifier(jwk),
    )

    panel_assertion = _assertion(private_key, audience=PANEL_AUDIENCE)
    wrong_audience = asyncio.run(
        _request(app, headers={"Cf-Access-Jwt-Assertion": panel_assertion})
    )
    bearer_only = asyncio.run(
        _request(app, headers={"Authorization": f"Bearer {MCP_TOKEN}"})
    )

    assert wrong_audience.status_code == 403
    assert wrong_audience.json()["code"] == "access_assertion_not_for_application"
    assert bearer_only.status_code == 401
    assert bearer_only.json()["code"] == "missing_access_assertion"


@pytest.mark.parametrize(
    ("claims", "omit", "expected_status", "expected_code"),
    [
        ({"iss": "https://other.cloudflareaccess.com"}, (), 403, "access_assertion_not_for_application"),
        ({"exp": int(time.time()) - 1}, (), 401, "expired_access_assertion"),
        ({"iat": int(time.time()) + 60}, (), 401, "access_assertion_not_yet_valid"),
        ({"nbf": int(time.time()) + 60}, (), 401, "access_assertion_not_yet_valid"),
        ({"type": "org"}, (), 403, "invalid_access_token_type"),
        ({"sub": ""}, (), 401, "invalid_access_subject"),
        ({}, ("iat",), 401, "invalid_access_assertion"),
    ],
)
def test_cloudflare_access_rejects_invalid_security_claims(
    claims: dict[str, Any],
    omit: tuple[str, ...],
    expected_status: int,
    expected_code: str,
    inline_auth_verification,
):
    private_key, jwk = _signing_key()
    assertion = _assertion(private_key, claims=claims, omit=omit)
    app = McpAuthenticationMiddleware(
        lambda scope, receive, send: None,
        _managed_config(),
        cloudflare_verifier=_verifier(jwk),
    )

    response = asyncio.run(
        _request(app, headers={"Cf-Access-Jwt-Assertion": assertion})
    )

    assert response.status_code == expected_status
    assert response.json()["code"] == expected_code


def test_cloudflare_access_rejects_tampered_signature(inline_auth_verification):
    private_key, jwk = _signing_key()
    assertion = _assertion(private_key)
    header, payload, signature = assertion.split(".")
    raw_signature = bytearray(jwt.utils.base64url_decode(signature.encode("ascii")))
    raw_signature[0] ^= 1
    tampered = ".".join(
        [header, payload, jwt.utils.base64url_encode(bytes(raw_signature)).decode("ascii")]
    )
    app = McpAuthenticationMiddleware(
        lambda scope, receive, send: None,
        _managed_config(),
        cloudflare_verifier=_verifier(jwk),
    )

    response = asyncio.run(
        _request(app, headers={"Cf-Access-Jwt-Assertion": tampered})
    )

    assert response.status_code == 401
    assert response.json()["code"] == "invalid_access_assertion"


@pytest.mark.parametrize(
    "config",
    [
        _config(auth_mode="cloudflare_access", cloudflare_issuer=None, cloudflare_audience=MCP_AUDIENCE),
        _config(auth_mode="cloudflare_access", cloudflare_issuer=ISSUER, cloudflare_audience=None),
        _managed_config(internal_token=""),
    ],
)
def test_streamable_http_cloudflare_access_configuration_fails_closed(config: McpServerConfig):
    with pytest.raises(McpConfigurationError):
        create_streamable_http_app(config, panel_client=FakePanelClient())


def test_mcp_auth_mode_defaults_to_cloudflare_access_without_static_fallback(monkeypatch):
    monkeypatch.delenv("LLS_MCP_AUTH_MODE", raising=False)
    monkeypatch.delenv("LLS_MCP_CF_ACCESS_ISSUER", raising=False)
    monkeypatch.delenv("LLS_MCP_CF_ACCESS_AUD", raising=False)
    monkeypatch.setenv("LLS_MCP_BEARER_TOKEN", MCP_TOKEN)
    config = McpServerConfig.from_env(internal_token=INTERNAL_TOKEN)

    assert config.auth_mode == "cloudflare_access"
    with pytest.raises(McpConfigurationError, match="LLS_MCP_CF_ACCESS_ISSUER"):
        create_streamable_http_app(config, panel_client=FakePanelClient())


def test_mcp_cloudflare_access_rejects_reused_panel_audience(monkeypatch):
    monkeypatch.setenv("LLS_MCP_AUTH_MODE", "cloudflare_access")
    monkeypatch.setenv("LLS_MCP_CF_ACCESS_ISSUER", ISSUER)
    monkeypatch.setenv("LLS_MCP_CF_ACCESS_AUD", MCP_AUDIENCE)
    monkeypatch.setenv("LLS_CF_ACCESS_AUD", MCP_AUDIENCE)
    config = McpServerConfig.from_env(internal_token=INTERNAL_TOKEN)

    with pytest.raises(McpConfigurationError, match="不同") as exc_info:
        create_streamable_http_app(config, panel_client=FakePanelClient())

    assert MCP_AUDIENCE not in str(exc_info.value)


def test_mcp_config_rejects_missing_internal_token(monkeypatch):
    monkeypatch.delenv("LLS_MCP_INTERNAL_TOKEN", raising=False)

    with pytest.raises(McpConfigurationError, match="LLS_MCP_INTERNAL_TOKEN"):
        McpServerConfig.from_env(internal_token="")


def test_static_dev_rejects_non_loopback_listener(monkeypatch):
    calls: list[tuple[Any, str, int]] = []
    monkeypatch.setattr(mcp_server, "create_streamable_http_app", lambda config: "app")
    monkeypatch.setattr(
        mcp_server.uvicorn,
        "run",
        lambda app, *, host, port, log_level: calls.append((app, host, port)),
    )

    with pytest.raises(McpConfigurationError, match="回环"):
        serve_mcp(_config(), transport="streamable-http", host="0.0.0.0", port=8766)

    serve_mcp(
        _config(),
        transport="streamable-http",
        host="0.0.0.0",
        published_host="127.0.0.1",
        port=8766,
    )
    assert calls == [("app", "0.0.0.0", 8766)]


def test_cloudflare_access_rejects_non_loopback_origin_publish(monkeypatch):
    calls: list[tuple[Any, str, int]] = []
    monkeypatch.setattr(mcp_server, "create_streamable_http_app", lambda config: "app")
    monkeypatch.setattr(
        mcp_server.uvicorn,
        "run",
        lambda app, *, host, port, log_level: calls.append((app, host, port)),
    )

    with pytest.raises(McpConfigurationError, match="Tunnel-only"):
        serve_mcp(
            _managed_config(),
            transport="streamable-http",
            host="0.0.0.0",
            published_host="0.0.0.0",
            port=8766,
        )

    serve_mcp(
        _managed_config(),
        transport="streamable-http",
        host="0.0.0.0",
        published_host="127.0.0.1",
        port=8766,
    )
    assert calls == [("app", "0.0.0.0", 8766)]


def test_sensitive_auth_values_do_not_enter_repr_response_errors_or_logs(
    caplog,
    inline_auth_verification,
):
    context = McpRequestContext(
        authentication_method="cloudflare_access",
        issuer=ISSUER,
        subject=SUBJECT,
        access_assertion="sensitive-access-assertion",
    )
    assert "sensitive-access-assertion" not in repr(context)

    app = McpAuthenticationMiddleware(
        lambda scope, receive, send: None,
        _managed_config(),
        cloudflare_verifier=_verifier(_signing_key()[1]),
    )
    with caplog.at_level(logging.INFO):
        response = asyncio.run(
            _request(
                app,
                headers={
                    "Authorization": f"Bearer {OPAQUE_TOKEN}",
                    "Cf-Access-Jwt-Assertion": "sensitive-access-assertion",
                },
            )
        )

    combined = response.text + caplog.text
    assert "sensitive-access-assertion" not in combined
    assert OPAQUE_TOKEN not in combined
    assert MCP_TOKEN not in combined
    assert INTERNAL_TOKEN not in combined


def test_panel_api_errors_redact_configured_tokens():
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={"error": f"echo {INTERNAL_TOKEN} and {MCP_TOKEN}"},
        )

    async def run():
        client = PanelApiClient(_config(), transport=httpx.MockTransport(handler))
        try:
            await client.get("/api/tasks")
        finally:
            await client.aclose()

    with pytest.raises(PanelApiError) as exc_info:
        asyncio.run(run())

    assert INTERNAL_TOKEN not in str(exc_info.value)
    assert MCP_TOKEN not in str(exc_info.value)
    assert str(exc_info.value).count("[REDACTED]") == 2
