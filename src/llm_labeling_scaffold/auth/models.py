from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote


@dataclass(frozen=True, slots=True)
class Identity:
    issuer: str
    subject: str
    email: str | None = None
    display_name: str | None = None
    email_verified: bool = False

    @property
    def identity_key(self) -> tuple[str, str]:
        return self.issuer, self.subject

    def display_snapshot(self) -> dict[str, str]:
        snapshot = {}
        if self.display_name:
            snapshot["display_name"] = self.display_name
        if self.email:
            snapshot["email"] = self.email
        return snapshot


@dataclass(frozen=True, slots=True)
class Principal:
    kind: str
    authentication_method: str
    identity: Identity

    @property
    def audit_id(self) -> str:
        if self.authentication_method == "cloudflare_access":
            issuer = quote(self.identity.issuer, safe="")
            subject = quote(self.identity.subject, safe="")
            return f"cloudflare_access:{issuer}:{subject}"
        return self.identity.subject

    @property
    def is_static_mcp_service(self) -> bool:
        return self.kind == "service" and self.authentication_method == "mcp_service_bearer"


@dataclass(frozen=True, slots=True)
class ActorContext:
    actor: Principal
    caller: Principal

    @classmethod
    def direct(cls, principal: Principal) -> "ActorContext":
        return cls(actor=principal, caller=principal)
