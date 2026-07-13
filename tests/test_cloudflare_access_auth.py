from __future__ import annotations

import base64
import json
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from llm_labeling_scaffold import panel
from llm_labeling_scaffold.auth import (
    ActorContext,
    CloudflareAccessVerifier,
    Identity,
    JwksUnavailableError,
    PanelAuthenticator,
    Principal,
    TokenForbiddenError,
    TokenVerificationError,
    build_panel_authenticator,
)

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


def _assertion(
    private_key: rsa.RSAPrivateKey,
    kid: str,
    *,
    claims: dict | None = None,
    omit: tuple[str, ...] = (),
) -> str:
    now = int(time.time())
    payload = {
        "aud": [EXPECTED_AUDIENCE],
        "email": "alice@example.com",
        "name": "Alice",
        "exp": now + 300,
        "iat": now - 1,
        "nbf": now - 1,
        "iss": ISSUER,
        "sub": SUBJECT,
        "type": "app",
    }
    payload.update(claims or {})
    for name in omit:
        payload.pop(name, None)
    return jwt.encode(payload, private_key, algorithm="RS256", headers={"kid": kid, "typ": "JWT"})


class _SequenceFetcher:
    def __init__(self, *responses: dict | Exception) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, float]] = []

    def __call__(self, url: str, timeout_seconds: float) -> dict:
        self.calls.append((url, timeout_seconds))
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        response = self.responses[index]
        if isinstance(response, Exception):
            raise response
        return response


def _verifier(jwk: dict, *, fetcher=None, max_jwks_keys: int = 8) -> CloudflareAccessVerifier:
    return CloudflareAccessVerifier(
        ISSUER,
        EXPECTED_AUDIENCE,
        jwks_fetcher=fetcher or _SequenceFetcher({"keys": [jwk]}),
        max_jwks_keys=max_jwks_keys,
    )


def test_verifies_access_identity_and_uses_issuer_subject_as_key():
    private_key, jwk = _signing_key("key-1")
    fetcher = _SequenceFetcher({"keys": [jwk]})
    verifier = _verifier(jwk, fetcher=fetcher)

    identity = verifier.verify(_assertion(private_key, "key-1"))

    assert verifier.expected_audience == EXPECTED_AUDIENCE
    assert identity.identity_key == (ISSUER, SUBJECT)
    assert identity.email == "alice@example.com"
    assert identity.display_name == "Alice"
    assert fetcher.calls == [(f"{ISSUER}/cdn-cgi/access/certs", 5)]


def test_rejects_tampered_signature():
    private_key, jwk = _signing_key("key-1")
    token = _assertion(private_key, "key-1")
    header, payload, signature = token.split(".")
    raw_signature = bytearray(jwt.utils.base64url_decode(signature.encode("ascii")))
    raw_signature[0] ^= 1
    tampered = ".".join(
        [header, payload, jwt.utils.base64url_encode(bytes(raw_signature)).decode("ascii")]
    )

    with pytest.raises(TokenVerificationError) as exc_info:
        _verifier(jwk).verify(tampered)

    assert exc_info.value.code == "invalid_access_assertion"


@pytest.mark.parametrize(
    ("claims", "expected_code"),
    [
        ({"aud": ["wrong-audience"]}, "access_assertion_not_for_application"),
        ({"iss": "https://other.cloudflareaccess.com"}, "access_assertion_not_for_application"),
        ({"type": "org"}, "invalid_access_token_type"),
    ],
)
def test_rejects_wrong_audience_issuer_and_type(claims: dict, expected_code: str):
    private_key, jwk = _signing_key("key-1")

    with pytest.raises(TokenForbiddenError) as exc_info:
        _verifier(jwk).verify(_assertion(private_key, "key-1", claims=claims))

    assert exc_info.value.code == expected_code


@pytest.mark.parametrize(
    ("claims", "omit", "expected_code"),
    [
        ({"exp": int(time.time()) - 1}, (), "expired_access_assertion"),
        ({"nbf": int(time.time()) + 60}, (), "access_assertion_not_yet_valid"),
        ({}, ("nbf",), "invalid_access_assertion"),
    ],
)
def test_rejects_expired_not_yet_valid_and_missing_required_claims(
    claims: dict,
    omit: tuple[str, ...],
    expected_code: str,
):
    private_key, jwk = _signing_key("key-1")

    with pytest.raises(TokenVerificationError) as exc_info:
        _verifier(jwk).verify(_assertion(private_key, "key-1", claims=claims, omit=omit))

    assert exc_info.value.code == expected_code


def test_unknown_kid_refreshes_jwks_once_and_accepts_rotated_key():
    first_private_key, first_jwk = _signing_key("key-1")
    second_private_key, second_jwk = _signing_key("key-2")
    fetcher = _SequenceFetcher(
        {"keys": [first_jwk]},
        {"keys": [first_jwk, second_jwk]},
    )
    verifier = _verifier(first_jwk, fetcher=fetcher)

    verifier.verify(_assertion(first_private_key, "key-1"))
    identity = verifier.verify(_assertion(second_private_key, "key-2"))

    assert identity.subject == SUBJECT
    assert len(fetcher.calls) == 2


def test_jwks_network_failure_fails_closed():
    private_key, jwk = _signing_key("key-1")
    fetcher = _SequenceFetcher(OSError("network unavailable"))

    with pytest.raises(JwksUnavailableError) as exc_info:
        _verifier(jwk, fetcher=fetcher).verify(_assertion(private_key, "key-1"))

    assert exc_info.value.code == "jwks_unavailable"


def test_jwks_cache_rejects_more_than_the_configured_key_bound():
    private_key, jwk = _signing_key("key-1")
    keys = [{**jwk, "kid": f"key-{index}"} for index in range(3)]
    verifier = _verifier(jwk, fetcher=_SequenceFetcher({"keys": keys}), max_jwks_keys=2)

    with pytest.raises(JwksUnavailableError) as exc_info:
        verifier.verify(_assertion(private_key, "key-1"))

    assert exc_info.value.code == "invalid_jwks_response"


@pytest.mark.parametrize(
    ("claims", "expected_status", "expected_code"),
    [
        ({"aud": ["wrong-audience"]}, 403, "access_assertion_not_for_application"),
        ({"iss": "https://other.cloudflareaccess.com"}, 403, "access_assertion_not_for_application"),
        ({"type": "org"}, 403, "invalid_access_token_type"),
        ({"exp": int(time.time()) - 1}, 401, "expired_access_assertion"),
        ({"nbf": int(time.time()) + 60}, 401, "access_assertion_not_yet_valid"),
    ],
)
def test_panel_returns_stable_status_for_invalid_access_assertions(
    tmp_path: Path,
    claims: dict,
    expected_status: int,
    expected_code: str,
):
    private_key, jwk = _signing_key("key-1")
    authenticator = PanelAuthenticator(
        mode="cloudflare_access",
        cloudflare_verifier=_verifier(jwk),
    )

    with _panel_server(tmp_path / "runs", tmp_path / "tasks", authenticator) as base_url:
        status, body = _request(
            base_url,
            "/api/session",
            headers={"Cf-Access-Jwt-Assertion": _assertion(private_key, "key-1", claims=claims)},
        )

    assert (status, body["code"]) == (expected_status, expected_code)


@contextmanager
def _panel_server(runs_root: Path, tasks_root: Path, authenticator: PanelAuthenticator):
    old = {
        "runs_root": panel._Handler.runs_root,
        "tasks_root": panel._Handler.tasks_root,
        "static_dir": panel._Handler.static_dir,
        "authenticator": panel._Handler.authenticator,
    }
    panel._Handler.runs_root = runs_root
    panel._Handler.tasks_root = tasks_root
    panel._Handler.static_dir = None
    panel._Handler.authenticator = authenticator
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), panel._Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        for key, value in old.items():
            setattr(panel._Handler, key, value)


def _request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    body: dict | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict]:
    request_headers = {"Content-Type": "application/json", **(headers or {})}
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(base_url + path, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _draft_spec() -> dict:
    return {
        "task_id": "access_actor_task",
        "id_field": "record_id",
        "text_fields": ["title"],
        "primary_label_name": "decision",
        "primary_label_values": ["accept", "reject"],
    }


def test_panel_session_uses_verified_display_snapshot_not_spoofed_headers(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LLS_TASK_SOURCE", "control")
    private_key, jwk = _signing_key("key-1")
    token = _assertion(private_key, "key-1")
    authenticator = PanelAuthenticator(
        mode="cloudflare_access",
        cloudflare_verifier=_verifier(jwk),
    )
    headers = {
        "Cf-Access-Jwt-Assertion": token,
        "Cf-Access-Authenticated-User-Email": "attacker@example.com",
        "X-Actor": "attacker",
        "X-User-Email": "attacker@example.com",
    }

    with _panel_server(tmp_path / "runs", tmp_path / "tasks", authenticator) as base_url:
        status, session = _request(base_url, "/api/session", headers=headers)
        health_status, health = _request(base_url, "/api/health", headers=headers)
        capabilities_status, capabilities = _request(base_url, "/api/capabilities", headers=headers)
        settings_status, settings_body = _request(base_url, "/api/settings/public", headers=headers)
        read_status, read_body = _request(base_url, "/api/tasks", headers=headers)
        create_status, create_body = _request(
            base_url,
            "/api/tasks",
            method="POST",
            body=_draft_spec(),
            headers=headers,
        )

    assert status == 200
    assert session == {
        "authenticated": True,
        "user": {
            "display_name": "Alice",
            "email": "alice@example.com",
        },
        "authentication": {"method": "cloudflare_access"},
        "authorization": {"state": "unavailable"},
    }
    assert "issuer" not in session["user"]
    assert "subject" not in session["user"]
    assert "roles" not in session["user"]
    assert "workspace" not in session
    assert "capabilities" not in session
    assert token not in json.dumps(session)
    assert (health_status, health["ok"]) == (200, True)
    assert capabilities_status == 200
    assert capabilities["authorization"] == {"state": "unavailable"}
    assert (settings_status, settings_body["code"]) == (503, "authorization_unavailable")
    assert (read_status, read_body["code"]) == (503, "authorization_unavailable")
    assert (create_status, create_body["code"]) == (503, "authorization_unavailable")
    assert not (tmp_path / "runs" / "_system" / "task_control" / "registry.json").exists()


def test_actor_context_separates_actor_from_caller_and_mcp_checks_caller():
    user = Principal(
        kind="user",
        authentication_method="cloudflare_access",
        identity=Identity(ISSUER, SUBJECT, email="alice@example.com"),
    )
    service = Principal(
        kind="service",
        authentication_method="mcp_service_bearer",
        identity=Identity("urn:lls:mcp-service", "mcp"),
    )
    panel_context = ActorContext.direct(user)
    managed_mcp_context = ActorContext(actor=user, caller=service)

    assert panel_context.actor is panel_context.caller
    assert panel_context.actor.identity.identity_key == (ISSUER, SUBJECT)
    assert managed_mcp_context.actor.kind == "user"
    assert managed_mcp_context.caller.kind == "service"

    holder = type("Holder", (), {"_request_context": managed_mcp_context})()
    assert panel._Handler._is_mcp_request(holder) is True


def test_cloudflare_mode_rejects_missing_assertion_and_does_not_fall_back_to_basic(tmp_path: Path):
    _, jwk = _signing_key("key-1")
    authenticator = PanelAuthenticator(
        mode="cloudflare_access",
        cloudflare_verifier=_verifier(jwk),
    )
    basic = "Basic " + base64.b64encode(b"admin:secret").decode("ascii")

    with _panel_server(tmp_path / "runs", tmp_path / "tasks", authenticator) as base_url:
        missing_status, missing = _request(base_url, "/api/session")
        basic_status, basic_body = _request(base_url, "/api/session", headers={"Authorization": basic})

    assert (missing_status, missing["code"]) == (401, "missing_access_assertion")
    assert (basic_status, basic_body["code"]) == (401, "missing_access_assertion")


def test_basic_auth_remains_available_only_in_explicit_development_mode(tmp_path: Path):
    authenticator = PanelAuthenticator(
        mode="basic_dev",
        basic_user="admin",
        basic_password="secret",
    )
    basic = "Basic " + base64.b64encode(b"admin:secret").decode("ascii")

    with _panel_server(tmp_path / "runs", tmp_path / "tasks", authenticator) as base_url:
        status, session = _request(base_url, "/api/session", headers={"Authorization": basic})

    assert status == 200
    assert session == {
        "authenticated": True,
        "user": {"display_name": "admin"},
        "authentication": {"method": "basic_dev"},
        "authorization": {"state": "unavailable"},
    }


def test_default_mode_does_not_infer_basic_from_a_password(monkeypatch):
    monkeypatch.delenv("LLS_PANEL_AUTH_MODE", raising=False)
    monkeypatch.delenv("LLS_CF_ACCESS_ISSUER", raising=False)
    monkeypatch.delenv("LLS_CF_ACCESS_AUD", raising=False)

    with pytest.raises(ValueError, match="cloudflare_access"):
        build_panel_authenticator(basic_password="secret")
