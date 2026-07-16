from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from .enums import AuditActorType, AuditChannel
from .models import AuditEvent, Principal
from .sensitive_json import normalize_sensitive_json_object


SYSTEM_ISSUER = "llm-labeling-scaffold"
SYSTEM_SUBJECT = "system"


def append_audit_event(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    event_type: str,
    actor: Principal | None,
    caller: Principal | None,
    channel: AuditChannel,
    resource_type: str | None = None,
    resource_id: str | uuid.UUID | None = None,
    request_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> AuditEvent:
    if actor is None:
        actor_type = AuditActorType.SYSTEM
        actor_principal_id = None
        actor_issuer = SYSTEM_ISSUER
        actor_subject = SYSTEM_SUBJECT
        actor_display_name = None
    else:
        actor_type = AuditActorType.PRINCIPAL
        actor_principal_id = actor.id
        actor_issuer = actor.issuer
        actor_subject = actor.subject
        actor_display_name = actor.display_name

    normalized_details = {} if details is None else normalize_sensitive_json_object(details)

    event = AuditEvent(
        workspace_id=workspace_id,
        actor_type=actor_type,
        actor_principal_id=actor_principal_id,
        caller_principal_id=caller.id if caller is not None else None,
        channel=channel,
        actor_issuer=actor_issuer,
        actor_subject=actor_subject,
        actor_display_name=actor_display_name,
        event_type=event_type,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id is not None else None,
        request_id=request_id,
        details=normalized_details,
    )
    session.add(event)
    return event
