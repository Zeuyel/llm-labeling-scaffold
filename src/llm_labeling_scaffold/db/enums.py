from __future__ import annotations

from enum import Enum


class PrincipalType(str, Enum):
    USER = "user"
    SERVICE = "service"


class Role(str, Enum):
    VIEWER = "viewer"
    ANNOTATOR = "annotator"
    EXPERIMENTER = "experimenter"
    ADMIN = "admin"


class TaskStatus(str, Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class IdempotencyState(str, Enum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class AuditActorType(str, Enum):
    PRINCIPAL = "principal"
    SYSTEM = "system"


class AuditChannel(str, Enum):
    PANEL = "panel"
    MCP = "mcp"
    API = "api"
    CLI = "cli"
    WORKER = "worker"
    SYSTEM = "system"


class MigrationStatus(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
