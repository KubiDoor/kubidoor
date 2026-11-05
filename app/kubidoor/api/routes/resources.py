"""Read-only listings of nodes, pods and deployments (paginated, filterable)."""

from typing import Annotated

from fastapi import APIRouter, Query

from kubidoor.api.dependencies import LimitQuery, OffsetQuery, ServicesDep
from kubidoor.api.schemas.cluster import DeploymentOut, NodeOut, PodOut
from kubidoor.api.schemas.common import Page, paginate

router = APIRouter(tags=["cluster"])

NamespaceQuery = Annotated[str | None, Query(description="Only this namespace")]


def _namespaces(services: ServicesDep, namespace: str | None) -> list[str] | None:
    if namespace:
        return [namespace]
    return services.settings.monitoring.namespaces or None


@router.get("/nodes", response_model=Page[NodeOut])
def list_nodes(
    services: ServicesDep, limit: LimitQuery = 100, offset: OffsetQuery = 0
) -> Page[NodeOut]:
    nodes = sorted(services.gateway.list_nodes(), key=lambda n: n.name)
    return paginate([NodeOut.model_validate(n) for n in nodes], limit, offset)


@router.get("/pods", response_model=Page[PodOut])
def list_pods(
    services: ServicesDep,
    namespace: NamespaceQuery = None,
    phase: Annotated[str | None, Query(description="Pod phase, e.g. Pending")] = None,
    unhealthy_only: Annotated[bool, Query(description="Only not-ready pods")] = False,
    limit: LimitQuery = 100,
    offset: OffsetQuery = 0,
) -> Page[PodOut]:
    pods = services.gateway.list_pods(_namespaces(services, namespace))
    if phase:
        pods = [p for p in pods if p.phase.lower() == phase.lower()]
    if unhealthy_only:
        pods = [p for p in pods if not p.ready and p.phase != "Succeeded"]
    pods.sort(key=lambda p: (p.namespace, p.name))
    return paginate([PodOut.model_validate(p) for p in pods], limit, offset)


@router.get("/deployments", response_model=Page[DeploymentOut])
def list_deployments(
    services: ServicesDep,
    namespace: NamespaceQuery = None,
    degraded_only: Annotated[
        bool, Query(description="Only deployments below desired replicas")
    ] = False,
    limit: LimitQuery = 100,
    offset: OffsetQuery = 0,
) -> Page[DeploymentOut]:
    deps = services.gateway.list_deployments(_namespaces(services, namespace))
    if degraded_only:
        deps = [d for d in deps if d.available_replicas < d.replicas]
    deps.sort(key=lambda d: (d.namespace, d.name))
    return paginate([DeploymentOut.model_validate(d) for d in deps], limit, offset)
