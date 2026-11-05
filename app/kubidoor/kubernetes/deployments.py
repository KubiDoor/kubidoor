"""Parse V1Deployment into DeploymentInfo."""

from typing import Any

from kubidoor.core.clock import ensure_utc
from kubidoor.kubernetes.models import DeploymentInfo
from kubidoor.kubernetes.pods import parse_conditions


def parse_deployment(dep: Any) -> DeploymentInfo:
    meta, spec, status = dep.metadata, dep.spec, dep.status
    selector = (spec.selector.match_labels or {}) if spec.selector else {}
    return DeploymentInfo(
        namespace=meta.namespace,
        name=meta.name,
        uid=meta.uid,
        created_at=ensure_utc(meta.creation_timestamp),
        generation=int(meta.generation or 0),
        observed_generation=int(status.observed_generation or 0),
        # spec.replicas defaults to 1 when omitted.
        replicas=1 if spec.replicas is None else int(spec.replicas),
        updated_replicas=int(status.updated_replicas or 0),
        ready_replicas=int(status.ready_replicas or 0),
        available_replicas=int(status.available_replicas or 0),
        unavailable_replicas=int(status.unavailable_replicas or 0),
        paused=bool(spec.paused),
        conditions=parse_conditions(status.conditions),
        selector=dict(selector),
    )
