from __future__ import annotations

import base64
import hmac
import math
import os
from http import HTTPStatus
from typing import Mapping

from .cloudflare_access import (
    CloudflareAccessVerifier,
    JwksUnavailableError,
    TokenForbiddenError,
    TokenVerificationError,
)
from .models import ActorContext, Identity, Principal

_BASIC_DEV_ISSUER = "urn:lls:basic-dev"
_MCP_SERVICE_ISSUER = "urn:lls:mcp-service"


class PanelAuthenticationError(Exception):
    def __init__(self, code: str, message: str, status: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _valid_service_token(value: str) -> bool:
    return len(value) >= 32 and not any(char.isspace() for char in value)


def _bounded_float_env(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = str(os.environ.get(name, "")).strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是数字") from exc
    if not math.isfinite(value) or value < minimum or value > maximum:
        raise ValueError(f"{name} 必须在 {minimum} 到 {maximum} 之间")
    return value


def resolve_panel_auth_mode(explicit: str | None = None) -> str:
    mode = str(explicit or os.environ.get("LLS_PANEL_AUTH_MODE") or "cloudflare_access").strip().lower()
    if mode not in {"cloudflare_access", "basic_dev"}:
        raise ValueError("LLS_PANEL_AUTH_MODE 只能是 cloudflare_access 或 basic_dev")
    return mode


class PanelAuthenticator:
    def __init__(
        self,
        *,
        mode: str,
        cloudflare_verifier: CloudflareAccessVerifier | None = None,
        basic_user: str = "",
        basic_password: str = "",
        internal_token: str = "",
    ) -> None:
        self.mode = resolve_panel_auth_mode(mode)
        self._cloudflare_verifier = cloudflare_verifier
        self._basic_user = basic_user
        self._basic_password = basic_password
        self._internal_token = internal_token if _valid_service_token(internal_token) else ""
        if self.mode == "cloudflare_access" and self._cloudflare_verifier is None:
            raise ValueError("cloudflare_access 模式缺少 verifier")
        if self.mode == "basic_dev" and (not self._basic_user or not self._basic_password):
            raise ValueError("basic_dev 模式必须设置用户名和密码")

    def authenticate(self, headers: Mapping[str, str]) -> ActorContext:
        authorization = str(headers.get("Authorization") or "").strip()
        parts = authorization.split()
        if len(parts) == 2 and parts[0].lower() == "bearer" and self._internal_token:
            if hmac.compare_digest(parts[1], self._internal_token):
                caller = Principal(
                    kind="service",
                    authentication_method="mcp_service_bearer",
                    identity=Identity(
                        issuer=_MCP_SERVICE_ISSUER,
                        subject="mcp",
                        display_name="MCP service",
                    ),
                )
                assertion = str(headers.get("Cf-Access-Jwt-Assertion") or "").strip()
                if assertion and self.mode == "cloudflare_access":
                    return ActorContext(actor=self._cloudflare_principal(assertion), caller=caller)
                return ActorContext.direct(caller)

        if self.mode == "basic_dev":
            return self._authenticate_basic(parts)
        return self._authenticate_cloudflare(headers)

    def _authenticate_basic(self, parts: list[str]) -> ActorContext:
        if len(parts) != 2 or parts[0].lower() != "basic":
            raise PanelAuthenticationError(
                "missing_basic_credentials",
                "开发模式 Basic Auth 凭据缺失",
                HTTPStatus.UNAUTHORIZED,
            )
        try:
            raw = base64.b64decode(parts[1], validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise PanelAuthenticationError(
                "invalid_basic_credentials",
                "开发模式 Basic Auth 凭据无效",
                HTTPStatus.UNAUTHORIZED,
            ) from exc
        user, separator, password = raw.partition(":")
        if (
            not separator
            or not hmac.compare_digest(user, self._basic_user)
            or not hmac.compare_digest(password, self._basic_password)
        ):
            raise PanelAuthenticationError(
                "invalid_basic_credentials",
                "开发模式 Basic Auth 凭据无效",
                HTTPStatus.UNAUTHORIZED,
            )
        principal = Principal(
            kind="user",
            authentication_method="basic_dev",
            identity=Identity(
                issuer=_BASIC_DEV_ISSUER,
                subject=user,
                display_name=user,
            ),
        )
        return ActorContext.direct(principal)

    def _authenticate_cloudflare(self, headers: Mapping[str, str]) -> ActorContext:
        assertion = str(headers.get("Cf-Access-Jwt-Assertion") or "").strip()
        if not assertion:
            raise PanelAuthenticationError(
                "missing_access_assertion",
                "Cloudflare Access assertion 缺失",
                HTTPStatus.UNAUTHORIZED,
            )
        return ActorContext.direct(self._cloudflare_principal(assertion))

    def _cloudflare_principal(self, assertion: str) -> Principal:
        verifier = self._cloudflare_verifier
        if verifier is None:
            raise PanelAuthenticationError(
                "authentication_not_configured",
                "Cloudflare Access 验证器未配置",
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
        try:
            identity = verifier.verify(assertion)
        except TokenForbiddenError as exc:
            raise PanelAuthenticationError(
                exc.code,
                "Cloudflare Access assertion 不适用于此应用",
                HTTPStatus.FORBIDDEN,
            ) from exc
        except JwksUnavailableError as exc:
            raise PanelAuthenticationError(
                exc.code,
                "Cloudflare Access 验证暂时不可用",
                HTTPStatus.SERVICE_UNAVAILABLE,
            ) from exc
        except TokenVerificationError as exc:
            raise PanelAuthenticationError(
                exc.code,
                "Cloudflare Access assertion 无效",
                HTTPStatus.UNAUTHORIZED,
            ) from exc
        return Principal(
            kind="user",
            authentication_method="cloudflare_access",
            identity=identity,
        )

    @property
    def challenge(self) -> str | None:
        if self.mode == "basic_dev":
            return 'Basic realm="lls-panel-dev"'
        return None


def build_panel_authenticator(
    *,
    mode: str | None = None,
    basic_user: str = "admin",
    basic_password: str | None = None,
) -> PanelAuthenticator:
    resolved_mode = resolve_panel_auth_mode(mode)
    internal_token = str(os.environ.get("LLS_MCP_INTERNAL_TOKEN") or "").strip()
    if resolved_mode == "basic_dev":
        password = str(basic_password or os.environ.get("LLS_PANEL_PASSWORD") or "")
        if not password:
            raise ValueError("basic_dev 模式必须通过 --password 或 LLS_PANEL_PASSWORD 设置密码")
        return PanelAuthenticator(
            mode=resolved_mode,
            basic_user=basic_user,
            basic_password=password,
            internal_token=internal_token,
        )

    issuer = str(os.environ.get("LLS_CF_ACCESS_ISSUER") or "").strip()
    expected_audience = str(os.environ.get("LLS_CF_ACCESS_AUD") or "").strip()
    if not issuer or not expected_audience:
        raise ValueError("cloudflare_access 模式必须设置 LLS_CF_ACCESS_ISSUER 和 LLS_CF_ACCESS_AUD")
    verifier = CloudflareAccessVerifier(
        issuer,
        expected_audience,
        jwks_ttl_seconds=_bounded_float_env("LLS_CF_ACCESS_JWKS_TTL_SECONDS", 300, 30, 3600),
        http_timeout_seconds=_bounded_float_env("LLS_CF_ACCESS_HTTP_TIMEOUT_SECONDS", 5, 0.5, 15),
        clock_skew_seconds=_bounded_float_env("LLS_CF_ACCESS_CLOCK_SKEW_SECONDS", 0, 0, 60),
    )
    return PanelAuthenticator(
        mode=resolved_mode,
        cloudflare_verifier=verifier,
        internal_token=internal_token,
    )
