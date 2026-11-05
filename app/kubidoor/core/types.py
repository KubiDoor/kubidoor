"""Shared enumerations used across layers (kept dependency-free to avoid import cycles)."""

from enum import StrEnum


class Severity(StrEnum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


class IncidentStatus(StrEnum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    INVESTIGATING = "INVESTIGATING"
    RESOLVED = "RESOLVED"


class ResourceType(StrEnum):
    POD = "Pod"
    NODE = "Node"
    DEPLOYMENT = "Deployment"
    PVC = "PersistentVolumeClaim"


class ActionType(StrEnum):
    DEPLOYMENT_RESTART = "deployment_restart"
    POD_DELETE = "pod_delete"


class ActionStatus(StrEnum):
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    DRY_RUN = "DRY_RUN"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


ACTIVE_ACTION_STATUSES = frozenset(
    {ActionStatus.PENDING_APPROVAL, ActionStatus.APPROVED, ActionStatus.RUNNING}
)
