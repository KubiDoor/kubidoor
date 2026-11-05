"""Cluster API schemas (built from parsed domain objects; never contain secrets/env vars)."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ContainerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    image: str
    ready: bool
    restart_count: int
    state: str
    state_reason: str | None
    exit_code: int | None
    last_reason: str | None
    last_exit_code: int | None
    is_init: bool


class PodOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    namespace: str
    name: str
    uid: str
    phase: str
    ready: bool
    node_name: str | None
    created_at: datetime | None
    total_restarts: int
    owner_kind: str | None
    containers: list[ContainerOut]


class NodeConditionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    type: str
    status: str
    reason: str | None
    message: str | None
    last_transition: datetime | None


class NodeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    uid: str
    ready_status: str
    unschedulable: bool
    created_at: datetime | None
    conditions: list[NodeConditionOut]
    capacity: dict[str, str]
    allocatable: dict[str, str]


class DeploymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    namespace: str
    name: str
    uid: str
    replicas: int
    updated_replicas: int
    ready_replicas: int
    available_replicas: int
    unavailable_replicas: int
    paused: bool
    created_at: datetime | None


class Section(BaseModel):
    """One part of the overview. ``available=False`` means it could not be observed."""

    available: bool
    error: str | None = None
    data: dict[str, int] | None = None


class ClusterOverview(BaseModel):
    kubernetes_version: str
    nodes: Section
    pods: Section
    deployments: Section
    incidents: dict[str, int]
    resource_metrics: Section
