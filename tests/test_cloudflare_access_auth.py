from __future__ import annotations

import base64
import json
import threading
import time
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


class _BlockingFetcher:
    def __init__(
        self,
        initial: dict,
        *,
        blocked_result: dict | None = None,
        blocked_error: Exception | None = None,
        later_result: dict | None = None,
        later_error: Exception | None = None,
    ) -> None:
        self.initial = initial
        self.blocked_result = blocked_result
        self.blocked_error = blocked_error
        self.later_result = later_result
        self.later_error = later_error
        self.calls: list[tuple[str, float]] = []
        self.started = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()

    def __call__(self, url: str, timeout_seconds: float) -> dict:
        with self._lock:
            self.calls.append((url, timeout_seconds))
            call_number = len(self.calls)
        if call_number == 1:
            return self.initial
        if call_number == 2:
            self.started.set()
            if not self.release.wait(timeout=5):
                raise TimeoutError("test fetch was not released")
            if self.blocked_error is not None:
                raise self.blocked_error
            if self.blocked_result is None:
                raise AssertionError("blocked_result is required")
            return self.blocked_result
        if self.later_error is not None:
            raise self.later_error
        if self.later_result is None:
            raise AssertionError("unexpected JWKS fetch")
        return self.later_result


def _verifier(
    jwk: dict,
    *,
    fetcher=None,
    max_jwks_keys: int = 8,
    **kwargs,
) -> CloudflareAccessVerifier:
    return CloudflareAccessVerifier(
        ISSUER,
        EXPECTED_AUDIENCE,
        jwks_fetcher=fetcher or _SequenceFetcher({"keys": [jwk]}),
        max_jwks_keys=max_jwks_keys,
        **kwargs,
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


def test_known_kid_is_not_blocked_by_an_unknown_kid_refresh():
    valid_private_key, valid_jwk = _signing_key("key-1")
    unknown_private_key, _ = _signing_key("unknown-key")
    fetcher = _BlockingFetcher(
        {"keys": [valid_jwk]},
        blocked_result={"keys": [valid_jwk]},
    )
    verifier = _verifier(valid_jwk, fetcher=fetcher)
    valid_token = _assertion(valid_private_key, "key-1")
    unknown_token = _assertion(unknown_private_key, "unknown-key")
    verifier.verify(valid_token)

    unknown_errors: list[Exception] = []
    known_errors: list[Exception] = []
    known_done = threading.Event()

    def verify_unknown() -> None:
        try:
            verifier.verify(unknown_token)
        except Exception as exc:
            unknown_errors.append(exc)

    def verify_known() -> None:
        try:
            verifier.verify(valid_token)
        except Exception as exc:
            known_errors.append(exc)
        finally:
            known_done.set()

    unknown_thread = threading.Thread(target=verify_unknown)
    unknown_thread.start()
    assert fetcher.started.wait(timeout=1)
    known_thread = threading.Thread(target=verify_known)
    known_thread.start()
    try:
        assert known_done.wait(timeout=1)
        assert unknown_thread.is_alive()
    finally:
        fetcher.release.set()
        unknown_thread.join(timeout=2)
        known_thread.join(timeout=2)

    assert known_errors == []
    assert len(unknown_errors) == 1
    assert isinstance(unknown_errors[0], TokenVerificationError)
    assert len(fetcher.calls) == 2


def test_random_unknown_kids_are_single_flight_and_rate_limited_until_rotation_cooldown():
    valid_private_key, valid_jwk = _signing_key("key-1")
    rotated_private_key, rotated_jwk = _signing_key("key-2")
    clock_value = [100.0]
    fetcher = _BlockingFetcher(
        {"keys": [valid_jwk]},
        blocked_result={"keys": [valid_jwk]},
        later_result={"keys": [valid_jwk, rotated_jwk]},
    )
    verifier = _verifier(
        valid_jwk,
        fetcher=fetcher,
        clock=lambda: clock_value[0],
        max_negative_kids=8,
        unknown_kid_cooldown_seconds=10,
    )
    verifier.verify(_assertion(valid_private_key, "key-1"))

    start = threading.Event()
    errors: list[Exception] = []
    tokens = [
        _assertion(rotated_private_key, f"random-{index}")
        for index in range(24)
    ]

    def verify_random(token: str) -> None:
        start.wait(timeout=1)
        try:
            verifier.verify(token)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=verify_random, args=(token,)) for token in tokens]
    for thread in threads:
        thread.start()
    start.set()
    assert fetcher.started.wait(timeout=1)
    try:
        time.sleep(0.05)
        assert len(fetcher.calls) == 2
    finally:
        fetcher.release.set()
        for thread in threads:
            thread.join(timeout=2)

    assert len(fetcher.calls) == 2
    assert len(errors) == len(tokens)
    for index in range(24, 48):
        with pytest.raises(TokenVerificationError):
            verifier.verify(_assertion(rotated_private_key, f"random-{index}"))
    assert len(fetcher.calls) == 2
    assert len(verifier._jwks._negative_kids) <= 8

    clock_value[0] += 11
    identity = verifier.verify(_assertion(rotated_private_key, "key-2"))
    assert identity.subject == SUBJECT
    assert len(fetcher.calls) == 3


def test_failed_refresh_has_single_flight_and_does_not_form_a_retry_storm():
    valid_private_key, valid_jwk = _signing_key("key-1")
    unknown_private_key, _ = _signing_key("unknown-key")
    clock_value = [200.0]
    fetcher = _BlockingFetcher(
        {"keys": [valid_jwk]},
        blocked_error=OSError("network unavailable"),
        later_error=OSError("network still unavailable"),
    )
    verifier = _verifier(
        valid_jwk,
        fetcher=fetcher,
        clock=lambda: clock_value[0],
        refresh_failure_cooldown_seconds=10,
    )
    valid_token = _assertion(valid_private_key, "key-1")
    verifier.verify(valid_token)

    refresh_errors: list[Exception] = []

    def trigger_failed_refresh() -> None:
        try:
            verifier.verify(_assertion(unknown_private_key, "unknown-key"))
        except Exception as exc:
            refresh_errors.append(exc)

    refresh_thread = threading.Thread(target=trigger_failed_refresh)
    refresh_thread.start()
    assert fetcher.started.wait(timeout=1)
    verifier.verify(valid_token)
    for index in range(20):
        with pytest.raises(TokenVerificationError):
            verifier.verify(_assertion(unknown_private_key, f"during-failure-{index}"))
    assert len(fetcher.calls) == 2
    fetcher.release.set()
    refresh_thread.join(timeout=2)

    assert len(refresh_errors) == 1
    assert isinstance(refresh_errors[0], JwksUnavailableError)
    for index in range(40):
        with pytest.raises(JwksUnavailableError):
            verifier.verify(_assertion(unknown_private_key, f"cooldown-{index}"))
    assert len(fetcher.calls) == 2
    verifier.verify(valid_token)

    clock_value[0] += 11
    with pytest.raises(JwksUnavailableError):
        verifier.verify(_assertion(unknown_private_key, "retry-after-cooldown"))
    assert len(fetcher.calls) == 3
    for index in range(20):
        with pytest.raises(JwksUnavailableError):
            verifier.verify(_assertion(unknown_private_key, f"second-cooldown-{index}"))
    assert len(fetcher.calls) == 3


def test_ttl_expiry_refreshes_known_keys_normally():
    valid_private_key, valid_jwk = _signing_key("key-1")
    clock_value = [300.0]
    fetcher = _SequenceFetcher(
        {"keys": [valid_jwk]},
        {"keys": [valid_jwk]},
    )
    verifier = _verifier(
        valid_jwk,
        fetcher=fetcher,
        clock=lambda: clock_value[0],
        jwks_ttl_seconds=5,
    )
    valid_token = _assertion(valid_private_key, "key-1")

    verifier.verify(valid_token)
    verifier.verify(valid_token)
    assert len(fetcher.calls) == 1
    clock_value[0] += 6
    verifier.verify(valid_token)
    assert len(fetcher.calls) == 2


@pytest.mark.parametrize(
    "kwargs",
    [
        {"jwks_ttl_seconds": float("nan")},
        {"jwks_ttl_seconds": float("inf")},
        {"http_timeout_seconds": float("nan")},
        {"http_timeout_seconds": float("-inf")},
        {"clock_skew_seconds": float("nan")},
        {"clock_skew_seconds": float("inf")},
        {"unknown_kid_cooldown_seconds": float("nan")},
        {"unknown_kid_cooldown_seconds": float("inf")},
        {"refresh_failure_cooldown_seconds": float("nan")},
        {"refresh_failure_cooldown_seconds": float("-inf")},
    ],
)
def test_verifier_rejects_non_finite_security_timing_values(kwargs: dict):
    _, jwk = _signing_key("key-1")

    with pytest.raises(ValueError):
        _verifier(jwk, **kwargs)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("LLS_CF_ACCESS_JWKS_TTL_SECONDS", "nan"),
        ("LLS_CF_ACCESS_JWKS_TTL_SECONDS", "inf"),
        ("LLS_CF_ACCESS_HTTP_TIMEOUT_SECONDS", "-inf"),
        ("LLS_CF_ACCESS_CLOCK_SKEW_SECONDS", "nan"),
        ("LLS_CF_ACCESS_CLOCK_SKEW_SECONDS", "inf"),
    ],
)
def test_environment_rejects_non_finite_access_timing_values(monkeypatch, name: str, value: str):
    monkeypatch.setenv("LLS_PANEL_AUTH_MODE", "cloudflare_access")
    monkeypatch.setenv("LLS_CF_ACCESS_ISSUER", ISSUER)
    monkeypatch.setenv("LLS_CF_ACCESS_AUD", EXPECTED_AUDIENCE)
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=name):
        build_panel_authenticator()


def test_non_finite_clock_skew_cannot_bypass_expired_token_validation():
    private_key, jwk = _signing_key("key-1")
    with pytest.raises(ValueError):
        _verifier(jwk, clock_skew_seconds=float("nan"))

    verifier = _verifier(jwk, clock_skew_seconds=0)
    with pytest.raises(TokenVerificationError) as exc_info:
        verifier.verify(
            _assertion(
                private_key,
                "key-1",
                claims={"exp": int(time.time()) - 1},
            )
        )
    assert exc_info.value.code == "expired_access_assertion"


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
    claims: dict,
    expected_status: int,
    expected_code: str,
):
    private_key, jwk = _signing_key("key-1")
    authenticator = PanelAuthenticator(
        mode="cloudflare_access",
        cloudflare_verifier=_verifier(jwk),
    )

    status, body, _ = _panel_request(
        authenticator,
        "/api/session",
        headers={"Cf-Access-Jwt-Assertion": _assertion(private_key, "key-1", claims=claims)},
    )

    assert (status, body["code"]) == (expected_status, expected_code)


def _panel_request(
    authenticator: PanelAuthenticator,
    path: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    runs_root: Path | None = None,
    tasks_root: Path | None = None,
) -> tuple[int, dict, dict[str, str]]:
    handler = object.__new__(panel._Handler)
    handler.authenticator = authenticator
    handler.headers = headers or {}
    handler.path = path
    handler.command = method
    handler.runs_root = runs_root or Path("runs")
    handler.tasks_root = tasks_root or Path("tasks")
    handler.static_dir = None
    response: dict[str, object] = {}

    def capture(body: dict, status: int = 200, headers: dict[str, str] | None = None) -> None:
        response.update(body=body, status=status, headers=headers or {})

    handler._json = capture
    if method == "GET":
        panel._Handler.do_GET(handler)
    else:
        panel._Handler._require_auth(handler)
    return (
        int(response["status"]),
        response["body"],
        response["headers"],
    )


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

    roots = {"runs_root": tmp_path / "runs", "tasks_root": tmp_path / "tasks"}
    status, session, session_headers = _panel_request(authenticator, "/api/session", headers=headers, **roots)
    health_status, health, _ = _panel_request(authenticator, "/api/health", headers=headers, **roots)
    capabilities_status, capabilities, _ = _panel_request(
        authenticator,
        "/api/capabilities",
        headers=headers,
        **roots,
    )
    settings_status, settings_body, _ = _panel_request(
        authenticator,
        "/api/settings/public",
        headers=headers,
        **roots,
    )
    read_status, read_body, _ = _panel_request(authenticator, "/api/tasks", headers=headers, **roots)
    create_status, create_body, _ = _panel_request(
        authenticator,
        "/api/tasks",
        method="POST",
        headers=headers,
        **roots,
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
    assert session_headers["Cache-Control"] == "no-store, private"
    assert session_headers["Pragma"] == "no-cache"
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


def test_authorization_gate_uses_actor_for_delegated_mcp_context():
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

    class HandlerStub:
        command = "GET"
        path = "/api/tasks"
        authenticator = None

        def __init__(self, context: ActorContext) -> None:
            self.context = context
            self.response = None

        def _authenticate(self) -> ActorContext:
            return self.context

        def _json(self, body, status=200, headers=None) -> None:
            self.response = (body, status, headers)

    delegated = HandlerStub(ActorContext(actor=user, caller=service))
    assert panel._Handler._require_auth(delegated) is False
    assert delegated.response == (
        {"error": "授权层尚不可用", "code": "authorization_unavailable"},
        503,
        None,
    )

    static_service = HandlerStub(ActorContext.direct(service))
    assert panel._Handler._require_auth(static_service) is True
    assert static_service._request_context == ActorContext.direct(service)


def test_cloudflare_mode_rejects_missing_assertion_and_does_not_fall_back_to_basic():
    _, jwk = _signing_key("key-1")
    authenticator = PanelAuthenticator(
        mode="cloudflare_access",
        cloudflare_verifier=_verifier(jwk),
    )
    basic = "Basic " + base64.b64encode(b"admin:secret").decode("ascii")

    missing_status, missing, _ = _panel_request(authenticator, "/api/session")
    basic_status, basic_body, _ = _panel_request(
        authenticator,
        "/api/session",
        headers={"Authorization": basic},
    )

    assert (missing_status, missing["code"]) == (401, "missing_access_assertion")
    assert (basic_status, basic_body["code"]) == (401, "missing_access_assertion")


def test_basic_auth_remains_available_only_in_explicit_development_mode():
    authenticator = PanelAuthenticator(
        mode="basic_dev",
        basic_user="admin",
        basic_password="secret",
    )
    basic = "Basic " + base64.b64encode(b"admin:secret").decode("ascii")

    status, session, _ = _panel_request(
        authenticator,
        "/api/session",
        headers={"Authorization": basic},
    )

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
