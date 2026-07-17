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


class IdempotencyState(str, Enum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class TaskMaterializationState(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class TaskLifecycle(str, Enum):
    ACTIVE = "active"
    DISABLED = "disabled"
    ARCHIVED = "archived"


class ArgillaBindingState(str, Enum):
    ACTIVE = "active"
    DISABLED = "disabled"


class AnnotatorMappingState(str, Enum):
    ACTIVE = "active"
    DISABLED = "disabled"


class AnnotatorVerificationState(str, Enum):
    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    REJECTED = "rejected"


class AnnotatorCohortState(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class AllocationStrategy(str, Enum):
    SHARED_QUEUE = "shared_queue"
    FIXED_PARTITION = "fixed_partition"
    CALIBRATION_THEN_PARTITION = "calibration_then_partition"


class AllocationManifestKind(str, Enum):
    SAMPLE = "sample"
    BATCH = "batch"


class AllocationPhase(str, Enum):
    CALIBRATION = "calibration"
    PRODUCTION = "production"


class AllocationWorkspaceMode(str, Enum):
    CALIBRATION = "calibration"
    PERSONAL = "personal"
    SHARED = "shared"


class AllocationAssignmentRole(str, Enum):
    CALIBRATION = "calibration"
    PRIMARY = "primary"
    OVERLAP = "overlap"
    SHARED = "shared"


class AllocationPlanLifecycle(str, Enum):
    DRAFT = "draft"
    CONFIRMED = "confirmed"


class AllocationDatasetState(str, Enum):
    PENDING = "pending"
    MATERIALIZING = "materializing"
    READY = "ready"
    FAILED = "failed"


class CollectionDisposition(str, Enum):
    ACCEPTED = "accepted"
    QUARANTINED = "quarantined"


class AnnotationJobLifecycle(str, Enum):
    DRAFT = "draft"
    READY = "ready"
    DISPATCHING = "dispatching"
    DISPATCHED = "dispatched"
    COLLECTING = "collecting"
    COMPLETED = "completed"
    FAILED = "failed"
    ARCHIVED = "archived"


class AnnotationDispatchState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class AnnotationCollectionState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
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
