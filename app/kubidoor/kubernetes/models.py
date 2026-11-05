"""Plain domain objects parsed from Kubernetes API responses.

These deliberately carry only what monitoring/diagnostics need: no environment variables,
no volume contents, nothing from Secrets. API schemas are built from these, so sensitive
cluster configuration cannot leak through the REST API.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Condition:
    type: str
    status: str
    reason: str | None = None
    message: str | None = None
    last_transition: datetime | None = None
    last_update: datetime | None = None


@dataclass(frozen=True, slots=True)
class ContainerInfo:
    name: str
    image: str
    ready: bool
    restart_count: int
    state: str  # running | waiting | terminated | unknown
    state_reason: str | None = None
    state_message: str | None = None
    exit_code: int | None = None
    last_reason: str | None = None  # reason of the previous termination
    last_exit_code: int | None = None
    last_finished_at: datetime | None = None
    requests: Mapping[str, str] = field(default_factory=dict)
    limits: Mapping[str, str] = field(default_factory=dict)
    is_init: bool = False


@dataclass(frozen=True, slots=True)
class PodInfo:
    namespace: str
    name: str
    uid: str
    phase: str
    node_name: str | None
    created_at: datetime | None
    reason: str | None = None
    message: str | None = None
    deletion_timestamp: datetime | None = None
    labels: Mapping[str, str] = field(default_factory=dict)
    owner_kind: str | None = None
    owner_name: str | None = None
    owner_uid: str | None = None
    containers: tuple[ContainerInfo, ...] = ()
    conditions: tuple[Condition, ...] = ()
    node_selector: Mapping[str, str] = field(default_factory=dict)
    tolerations: tuple[Mapping[str, str], ...] = ()
    image_pull_secrets: tuple[str, ...] = ()  # secret *names* only, never values

    @property
    def ready(self) -> bool:
        return any(c.type == "Ready" and c.status == "True" for c in self.conditions)

    @property
    def total_restarts(self) -> int:
        return sum(c.restart_count for c in self.containers if not c.is_init)

    def condition(self, type_: str) -> Condition | None:
        return next((c for c in self.conditions if c.type == type_), None)


@dataclass(frozen=True, slots=True)
class NodeInfo:
    name: str
    uid: str
    created_at: datetime | None
    conditions: tuple[Condition, ...] = ()
    capacity: Mapping[str, str] = field(default_factory=dict)
    allocatable: Mapping[str, str] = field(default_factory=dict)
    unschedulable: bool = False

    def condition(self, type_: str) -> Condition | None:
        return next((c for c in self.conditions if c.type == type_), None)

    @property
    def ready_status(self) -> str:
        cond = self.condition("Ready")
        return cond.status if cond else "Unknown"


@dataclass(frozen=True, slots=True)
class DeploymentInfo:
    namespace: str
    name: str
    uid: str
    created_at: datetime | None
    generation: int
    observed_generation: int
    replicas: int  # desired
    updated_replicas: int
    ready_replicas: int
    available_replicas: int
    unavailable_replicas: int
    paused: bool = False
    conditions: tuple[Condition, ...] = ()
    selector: Mapping[str, str] = field(default_factory=dict)

    def condition(self, type_: str) -> Condition | None:
        return next((c for c in self.conditions if c.type == type_), None)


@dataclass(frozen=True, slots=True)
class PVCInfo:
    namespace: str
    name: str
    uid: str
    phase: str
    created_at: datetime | None
    storage_class: str | None = None
    volume_name: str | None = None
    requested_storage: str | None = None


@dataclass(frozen=True, slots=True)
class EventInfo:
    namespace: str | None
    kind: str
    name: str
    uid: str | None
    type: str  # Normal | Warning
    reason: str
    message: str
    count: int
    first_at: datetime | None
    last_at: datetime | None


@dataclass(frozen=True, slots=True)
class ResourceUsage:
    """Measured usage from metrics-server. Only ever built from real measurements."""

    cpu_millicores: float
    memory_bytes: int
