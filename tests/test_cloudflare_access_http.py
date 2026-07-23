from __future__ import annotations

import base64
import errno
import http.client
import json
import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Mapping

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from llm_labeling_scaffold import panel
from llm_labeling_scaffold.auth import CloudflareAccessVerifier, PanelAuthenticator
from llm_labeling_scaffold.db import AuthorizationSession


ISSUER = "https://team.cloudflareaccess.com"
EXPECTED_AUDIENCE = "configured-application-audience"
SUBJECT = "7335d417-61da-459d-899c-0a01c76a2f94"


def _base64url_uint(value: int) -> str:
    width = (value.bit_length() + 7) // 8
    return jwt.utils.base64url_encode(value.to_bytes(width, "big")).decode("ascii")


def _signing_key(kid: str) -> tuple[rsa.RSAPrivateKey, dict]:
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


def _assertion(private_key: rsa.RSAPrivateKey, kid: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "aud": [EXPECTED_AUDIENCE],
            "email": "alice@example.com",
            "name": "Alice",
            "exp": now + 300,
            "iat": now - 1,
            "nbf": now - 1,
            "iss": ISSUER,
            "sub": SUBJECT,
            "type": "app",
        },
        private_key,
        algorithm="RS256",
        headers={"kid": kid, "typ": "JWT"},
    )


def _access_authenticator(private_key: rsa.RSAPrivateKey, jwk: dict) -> tuple[PanelAuthenticator, str]:
    token = _assertion(private_key, str(jwk["kid"]))

    def fetcher(url: str, timeout_seconds: float) -> dict:
        assert url == f"{ISSUER}/cdn-cgi/access/certs"
        assert timeout_seconds == 5
        return {"keys": [jwk]}

    return (
        _RecordingPanelAuthenticator(
            mode="cloudflare_access",
            cloudflare_verifier=CloudflareAccessVerifier(
                ISSUER,
                EXPECTED_AUDIENCE,
                jwks_fetcher=fetcher,
            ),
        ),
        token,
    )


class _RecordingPanelAuthenticator(PanelAuthenticator):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.observed_headers: list[dict[str, list[str]]] = []

    def authenticate(self, headers: Mapping[str, str]):
        get_all = getattr(headers, "get_all", None)
        self.observed_headers.append(
            {
                "assertions": list(get_all("Cf-Access-Jwt-Assertion") or []) if get_all else [],
                "emails": list(get_all("Cf-Access-Authenticated-User-Email") or []) if get_all else [],
            }
        )
        return super().authenticate(headers)


class _EmptyAuthorizationService:
    def get_session(self, identity) -> AuthorizationSession:
        return AuthorizationSession(principal=None, workspaces=())


@contextmanager
def _panel_server(host: str, authenticator: PanelAuthenticator):
    server_type = panel._panel_server_type(host)
    try:
        httpd = server_type((host, 0), panel._Handler)
    except OSError as exc:
        if _loopback_socket_unavailable(host, exc):
            pytest.skip(f"loopback bind unavailable: {exc}")
        raise

    old = {
        "runs_root": panel._Handler.runs_root,
        "tasks_root": panel._Handler.tasks_root,
        "static_dir": panel._Handler.static_dir,
        "authenticator": panel._Handler.authenticator,
        "authorization_service": panel._Handler.authorization_service,
        "authorization_ready": panel._Handler.authorization_ready,
        "active_task_loader": panel._Handler.active_task_loader,
    }
    panel._Handler.runs_root = Path("runs")
    panel._Handler.tasks_root = Path("tasks")
    panel._Handler.static_dir = None
    panel._Handler.authenticator = authenticator
    panel._Handler.authorization_service = _EmptyAuthorizationService()
    panel._Handler.authorization_ready = True
    panel._Handler.active_task_loader = None
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(httpd.server_address[1])
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        for key, value in old.items():
            setattr(panel._Handler, key, value)


def _request(
    host: str,
    port: int,
    headers: list[tuple[str, str]],
) -> tuple[int, dict, dict[str, str]]:
    connection = http.client.HTTPConnection(host, port, timeout=5)
    try:
        connection.putrequest("GET", "/api/session")
        for name, value in headers:
            connection.putheader(name, value)
        connection.endheaders()
        response = connection.getresponse()
        return (
            response.status,
            json.loads(response.read().decode("utf-8")),
            dict(response.getheaders()),
        )
    finally:
        connection.close()


def _loopback_socket_unavailable(host: str, exc: OSError) -> bool:
    if exc.errno in {errno.EACCES, errno.EPERM}:
        return True
    if host == "::1" and exc.errno in {
        errno.EADDRNOTAVAIL,
        errno.EAFNOSUPPORT,
        errno.EPROTONOSUPPORT,
    }:
        return True
    return False


def test_cloudflare_access_real_ipv4_http_path_handles_auth_and_duplicate_headers():
    private_key, jwk = _signing_key("key-1")
    authenticator, token = _access_authenticator(private_key, jwk)
    assert isinstance(authenticator, _RecordingPanelAuthenticator)
    basic = "Basic " + base64.b64encode(b"admin:secret").decode("ascii")

    with _panel_server("127.0.0.1", authenticator) as port:
        missing_status, missing, _ = _request("127.0.0.1", port, [])
        basic_status, basic_body, _ = _request(
            "127.0.0.1",
            port,
            [("Authorization", basic)],
        )
        valid_status, session, response_headers = _request(
            "127.0.0.1",
            port,
            [
                ("Cf-Access-Jwt-Assertion", token),
                ("Cf-Access-Jwt-Assertion", token),
                ("Cf-Access-Authenticated-User-Email", "attacker-one@example.com"),
                ("Cf-Access-Authenticated-User-Email", "attacker-two@example.com"),
            ],
        )

    assert (missing_status, missing["code"]) == (401, "missing_access_assertion")
    assert (basic_status, basic_body["code"]) == (401, "missing_access_assertion")
    assert valid_status == 200
    assert session == {
        "authenticated": True,
        "user": {"display_name": "Alice", "email": "alice@example.com"},
        "authentication": {"method": "cloudflare_access"},
        "authorization": {
            "state": "ready",
            "workspaces": [],
            "invitation_claims": {"status": "none", "items": []},
        },
    }
    assert response_headers["Cache-Control"] == "no-store, private"
    assert authenticator.observed_headers[-1] == {
        "assertions": [token, token],
        "emails": ["attacker-one@example.com", "attacker-two@example.com"],
    }


def test_panel_ipv6_loopback_bind_and_request():
    if not socket.has_ipv6:
        pytest.skip("Python runtime has no IPv6 support")
    authenticator = PanelAuthenticator(
        mode="basic_dev",
        basic_user="admin",
        basic_password="secret",
    )
    basic = "Basic " + base64.b64encode(b"admin:secret").decode("ascii")

    with _panel_server("::1", authenticator) as port:
        try:
            status, session, _ = _request("::1", port, [("Authorization", basic)])
        except OSError as exc:
            if _loopback_socket_unavailable("::1", exc):
                pytest.skip(f"IPv6 loopback request unavailable: {exc}")
            raise

    assert status == 200
    assert session == {
        "authenticated": True,
        "user": {"display_name": "admin"},
        "authentication": {"method": "basic_dev"},
        "authorization": {
            "state": "ready",
            "workspaces": [],
            "invitation_claims": {"status": "not_claimable_no_email", "items": []},
        },
    }
