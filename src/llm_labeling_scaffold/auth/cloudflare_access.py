from __future__ import annotations

import json
import math
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from typing import Any, Callable
from urllib.parse import urlparse

import jwt

from .models import Identity

_MAX_ASSERTION_BYTES = 64 * 1024
_MAX_JWKS_BYTES = 1024 * 1024
_DEFAULT_MAX_JWKS_KEYS = 8
_DEFAULT_MAX_NEGATIVE_KIDS = 64
_DEFAULT_UNKNOWN_KID_COOLDOWN_SECONDS = 10.0
_DEFAULT_REFRESH_FAILURE_COOLDOWN_SECONDS = 10.0


class TokenVerificationError(Exception):
    def __init__(self, code: str = "invalid_access_assertion") -> None:
        super().__init__(code)
        self.code = code


class TokenForbiddenError(TokenVerificationError):
    pass


class JwksUnavailableError(TokenVerificationError):
    pass


JwksFetcher = Callable[[str, float], dict[str, Any]]


def _normalize_issuer(value: str) -> str:
    issuer = str(value or "").strip().rstrip("/")
    parsed = urlparse(issuer)
    hostname = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Cloudflare Access issuer 必须是 https://<team>.cloudflareaccess.com") from exc
    team_name = hostname.removesuffix(".cloudflareaccess.com")
    if (
        parsed.scheme != "https"
        or not team_name
        or team_name.startswith(".")
        or not hostname.endswith(".cloudflareaccess.com")
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Cloudflare Access issuer 必须是 https://<team>.cloudflareaccess.com")
    return f"https://{hostname}"


def _fetch_jwks(url: str, timeout_seconds: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "llm-labeling-scaffold/cloudflare-access",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read(_MAX_JWKS_BYTES + 1)
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        raise JwksUnavailableError("jwks_unavailable") from exc
    if len(body) > _MAX_JWKS_BYTES:
        raise JwksUnavailableError("jwks_response_too_large")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JwksUnavailableError("invalid_jwks_response") from exc
    if not isinstance(payload, dict):
        raise JwksUnavailableError("invalid_jwks_response")
    return payload


class _BoundedJwksCache:
    def __init__(
        self,
        url: str,
        *,
        ttl_seconds: float,
        timeout_seconds: float,
        max_keys: int,
        max_negative_kids: int,
        unknown_kid_cooldown_seconds: float,
        refresh_failure_cooldown_seconds: float,
        fetcher: JwksFetcher,
        clock: Callable[[], float],
    ) -> None:
        if not math.isfinite(ttl_seconds) or ttl_seconds <= 0 or ttl_seconds > 3600:
            raise ValueError("JWKS cache TTL 必须在 0 到 3600 秒之间")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0 or timeout_seconds > 30:
            raise ValueError("JWKS 请求超时必须在 0 到 30 秒之间")
        if max_keys <= 0 or max_keys > 32:
            raise ValueError("JWKS key 缓存上限必须在 1 到 32 之间")
        if max_negative_kids <= 0 or max_negative_kids > 1024:
            raise ValueError("未知 kid 负缓存上限必须在 1 到 1024 之间")
        if (
            not math.isfinite(unknown_kid_cooldown_seconds)
            or unknown_kid_cooldown_seconds <= 0
            or unknown_kid_cooldown_seconds > 300
        ):
            raise ValueError("未知 kid 刷新 cooldown 必须在 0 到 300 秒之间")
        if (
            not math.isfinite(refresh_failure_cooldown_seconds)
            or refresh_failure_cooldown_seconds <= 0
            or refresh_failure_cooldown_seconds > 300
        ):
            raise ValueError("JWKS 失败重试 cooldown 必须在 0 到 300 秒之间")
        self._url = url
        self._ttl_seconds = ttl_seconds
        self._timeout_seconds = timeout_seconds
        self._max_keys = max_keys
        self._max_negative_kids = max_negative_kids
        self._unknown_kid_cooldown_seconds = unknown_kid_cooldown_seconds
        self._refresh_failure_cooldown_seconds = refresh_failure_cooldown_seconds
        self._fetcher = fetcher
        self._clock = clock
        self._keys: dict[str, Any] = {}
        self._expires_at = 0.0
        self._negative_kids: OrderedDict[str, float] = OrderedDict()
        self._unknown_refresh_retry_at = 0.0
        self._failure_retry_at = 0.0
        self._last_refresh_error_code = "jwks_unavailable"
        self._refresh_in_flight = False
        self._condition = threading.Condition(threading.Lock())

    def signing_key(self, kid: str) -> Any:
        while True:
            refresh_reason = ""
            with self._condition:
                now = self._clock()
                self._prune_negative_kids(now)
                cache_is_fresh = bool(self._keys) and now < self._expires_at
                key = self._keys.get(kid)
                if cache_is_fresh and key is not None:
                    return key

                if now < self._failure_retry_at:
                    self._remember_negative_kid(kid, self._failure_retry_at)
                    raise JwksUnavailableError(self._last_refresh_error_code)

                if cache_is_fresh:
                    if self._negative_kid_is_active(kid, now):
                        raise TokenVerificationError("unknown_signing_key")
                    if now < self._unknown_refresh_retry_at:
                        self._remember_negative_kid(kid, self._unknown_refresh_retry_at)
                        raise TokenVerificationError("unknown_signing_key")
                    if self._refresh_in_flight:
                        self._remember_negative_kid(kid, now + self._unknown_kid_cooldown_seconds)
                        raise TokenVerificationError("unknown_signing_key")
                    self._refresh_in_flight = True
                    refresh_reason = "unknown"
                else:
                    if self._refresh_in_flight:
                        self._condition.wait()
                        continue
                    self._refresh_in_flight = True
                    refresh_reason = "initial" if not self._keys else "expired"

            try:
                parsed = self._fetch_keys()
            except JwksUnavailableError as exc:
                completed_at = self._clock()
                with self._condition:
                    self._refresh_in_flight = False
                    self._last_refresh_error_code = exc.code
                    self._failure_retry_at = completed_at + self._refresh_failure_cooldown_seconds
                    if refresh_reason == "unknown":
                        self._unknown_refresh_retry_at = max(
                            self._unknown_refresh_retry_at,
                            completed_at + self._unknown_kid_cooldown_seconds,
                        )
                    self._remember_negative_kid(
                        kid,
                        max(self._failure_retry_at, self._unknown_refresh_retry_at),
                    )
                    self._condition.notify_all()
                raise

            completed_at = self._clock()
            with self._condition:
                self._keys = parsed
                self._expires_at = completed_at + self._ttl_seconds
                self._failure_retry_at = 0.0
                self._last_refresh_error_code = "jwks_unavailable"
                key = self._keys.get(kid)
                if refresh_reason == "unknown" or key is None:
                    self._unknown_refresh_retry_at = max(
                        self._unknown_refresh_retry_at,
                        completed_at + self._unknown_kid_cooldown_seconds,
                    )
                if key is None:
                    self._remember_negative_kid(kid, self._unknown_refresh_retry_at)
                else:
                    self._negative_kids.pop(kid, None)
                self._refresh_in_flight = False
                self._condition.notify_all()
                if key is None:
                    raise TokenVerificationError("unknown_signing_key")
                return key

    def _fetch_keys(self) -> dict[str, Any]:
        try:
            payload = self._fetcher(self._url, self._timeout_seconds)
        except JwksUnavailableError:
            raise
        except Exception as exc:
            raise JwksUnavailableError("jwks_unavailable") from exc
        keys = payload.get("keys") if isinstance(payload, dict) else None
        if not isinstance(keys, list) or not keys or len(keys) > self._max_keys:
            raise JwksUnavailableError("invalid_jwks_response")

        parsed: dict[str, Any] = {}
        for item in keys:
            if not isinstance(item, dict):
                raise JwksUnavailableError("invalid_jwks_response")
            kid = item.get("kid")
            if not isinstance(kid, str) or not kid or len(kid) > 256 or kid in parsed:
                raise JwksUnavailableError("invalid_jwks_response")
            if item.get("kty") != "RSA" or item.get("alg") not in {None, "RS256"} or item.get("use") not in {None, "sig"}:
                raise JwksUnavailableError("invalid_jwks_response")
            try:
                parsed[kid] = jwt.PyJWK.from_dict(item, algorithm="RS256").key
            except Exception as exc:
                raise JwksUnavailableError("invalid_jwks_response") from exc
        return parsed

    def _negative_kid_is_active(self, kid: str, now: float) -> bool:
        expires_at = self._negative_kids.get(kid)
        if expires_at is None:
            return False
        if expires_at <= now:
            self._negative_kids.pop(kid, None)
            return False
        self._negative_kids.move_to_end(kid)
        return True

    def _remember_negative_kid(self, kid: str, expires_at: float) -> None:
        self._negative_kids.pop(kid, None)
        self._negative_kids[kid] = expires_at
        while len(self._negative_kids) > self._max_negative_kids:
            self._negative_kids.popitem(last=False)

    def _prune_negative_kids(self, now: float) -> None:
        expired = [kid for kid, expires_at in self._negative_kids.items() if expires_at <= now]
        for kid in expired:
            self._negative_kids.pop(kid, None)


class CloudflareAccessVerifier:
    def __init__(
        self,
        issuer: str,
        expected_audience: str,
        *,
        jwks_ttl_seconds: float = 300,
        http_timeout_seconds: float = 5,
        clock_skew_seconds: float = 0,
        max_jwks_keys: int = _DEFAULT_MAX_JWKS_KEYS,
        max_negative_kids: int = _DEFAULT_MAX_NEGATIVE_KIDS,
        unknown_kid_cooldown_seconds: float = _DEFAULT_UNKNOWN_KID_COOLDOWN_SECONDS,
        refresh_failure_cooldown_seconds: float = _DEFAULT_REFRESH_FAILURE_COOLDOWN_SECONDS,
        jwks_fetcher: JwksFetcher = _fetch_jwks,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.issuer = _normalize_issuer(issuer)
        self.expected_audience = str(expected_audience or "").strip()
        if not self.expected_audience or len(self.expected_audience) > 512:
            raise ValueError("Cloudflare Access AUD 必须是非空字符串")
        if not math.isfinite(clock_skew_seconds) or clock_skew_seconds < 0 or clock_skew_seconds > 60:
            raise ValueError("JWT clock skew 必须在 0 到 60 秒之间")
        self._clock_skew_seconds = clock_skew_seconds
        self.jwks_url = f"{self.issuer}/cdn-cgi/access/certs"
        self._jwks = _BoundedJwksCache(
            self.jwks_url,
            ttl_seconds=jwks_ttl_seconds,
            timeout_seconds=http_timeout_seconds,
            max_keys=max_jwks_keys,
            max_negative_kids=max_negative_kids,
            unknown_kid_cooldown_seconds=unknown_kid_cooldown_seconds,
            refresh_failure_cooldown_seconds=refresh_failure_cooldown_seconds,
            fetcher=jwks_fetcher,
            clock=clock,
        )

    def verify(self, assertion: str) -> Identity:
        token = str(assertion or "").strip()
        if not token or len(token.encode("utf-8")) > _MAX_ASSERTION_BYTES:
            raise TokenVerificationError("invalid_access_assertion")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise TokenVerificationError("invalid_access_assertion") from exc
        if header.get("alg") != "RS256":
            raise TokenVerificationError("invalid_signing_algorithm")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid or len(kid) > 256:
            raise TokenVerificationError("missing_signing_key_id")

        key = self._jwks.signing_key(kid)
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=["RS256"],
                audience=self.expected_audience,
                issuer=self.issuer,
                leeway=self._clock_skew_seconds,
                options={
                    "require": ["aud", "exp", "iat", "iss", "nbf", "sub", "type"],
                    "verify_aud": True,
                    "verify_exp": True,
                    "verify_iat": True,
                    "verify_iss": True,
                    "verify_nbf": True,
                    "verify_signature": True,
                },
            )
        except (jwt.InvalidAudienceError, jwt.InvalidIssuerError) as exc:
            raise TokenForbiddenError("access_assertion_not_for_application") from exc
        except jwt.ExpiredSignatureError as exc:
            raise TokenVerificationError("expired_access_assertion") from exc
        except jwt.ImmatureSignatureError as exc:
            raise TokenVerificationError("access_assertion_not_yet_valid") from exc
        except jwt.PyJWTError as exc:
            raise TokenVerificationError("invalid_access_assertion") from exc

        if claims.get("type") != "app":
            raise TokenForbiddenError("invalid_access_token_type")
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject.strip() or len(subject) > 512:
            raise TokenVerificationError("invalid_access_subject")
        email = claims.get("email")
        display_name = claims.get("name")
        return Identity(
            issuer=self.issuer,
            subject=subject.strip(),
            email=email.strip() if isinstance(email, str) and 0 < len(email.strip()) <= 1024 else None,
            display_name=(
                display_name.strip()
                if isinstance(display_name, str) and 0 < len(display_name.strip()) <= 1024
                else None
            ),
        )
