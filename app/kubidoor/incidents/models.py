"""Incident-layer data transfer objects (independent of Kubernetes and of the database)."""

from dataclasses import dataclass, field
from typing import Any

from kubidoor.core.types import ResourceType, Severity
from kubidoor.incidents.fingerprints import compute_fingerprint


@dataclass(frozen=True, slots=True)
class Finding:
    """A problem observed during one monitoring cycle."""

    resource_type: ResourceType
    namespace: str | None
    resource_name: str
    resource_uid: str | None
    problem_type: str
    severity: Severity
    title: str
    description: str
    container: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    probable_cause: str = ""
    recommended_action: str = ""
    # The parsed Kubernetes object that produced this finding (used by diagnostics).
    subject: Any = field(default=None, compare=False, repr=False)

    @property
    def fingerprint(self) -> str:
        return compute_fingerprint(
            self.resource_type,
            self.namespace,
            self.resource_name,
            self.problem_type,
            self.container,
        )


@dataclass(frozen=True, slots=True)
class Scope:
    """What a monitoring cycle was able to observe. Incidents are only auto-resolved inside it."""

    resource_types: frozenset[ResourceType]
    namespaces: frozenset[str] | None = None  # None = cluster-wide


@dataclass(slots=True)
class SyncResult:
    created: list[str] = field(default_factory=list)
    reopened: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class IncidentFilters:
    severity: Severity | None = None
    status: str | None = None
    namespace: str | None = None
    resource_type: ResourceType | None = None
    active_only: bool = False
