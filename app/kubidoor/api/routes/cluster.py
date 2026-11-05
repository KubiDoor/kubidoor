"""Read-only cluster views. Failures are reported explicitly, never as empty/healthy data."""

from collections import Counter
from collections.abc import Callable

from fastapi import APIRouter
from sqlalchemy import func, select

from kubidoor.api.dependencies import ServicesDep
from kubidoor.api.schemas.cluster import ClusterOverview, Section
from kubidoor.core.exceptions import KubernetesError, MetricsUnavailableError
from kubidoor.core.types import IncidentStatus
from kubidoor.database.models import Incident
from kubidoor.database.session import session_scope

router = APIRouter(prefix="/cluster", tags=["cluster"])


def _section(fn: Callable[[], dict[str, int]]) -> Section:
    try:
        return Section(available=True, data=fn())
    except (KubernetesError, MetricsUnavailableError) as exc:
        return Section(available=False, error=exc.message)


@router.get("/overview", response_model=ClusterOverview)
def overview(services: ServicesDep) -> ClusterOverview:
    gw = services.gateway
    namespaces = services.settings.monitoring.namespaces or None
    version = gw.ping()  # unreachable cluster => 503 (not a fabricated overview)

    def nodes() -> dict[str, int]:
        items = gw.list_nodes()
        ready = sum(1 for n in items if n.ready_status == "True")
        return {"total": len(items), "ready": ready, "not_ready": len(items) - ready}

    def pods() -> dict[str, int]:
        items = gw.list_pods(namespaces)
        counts = Counter(p.phase for p in items)
        return {"total": len(items), **{k.lower(): v for k, v in sorted(counts.items())}}

    def deployments() -> dict[str, int]:
        items = gw.list_deployments(namespaces)
        healthy = sum(1 for d in items if d.available_replicas >= d.replicas)
        return {"total": len(items), "fully_available": healthy, "degraded": len(items) - healthy}

    def usage() -> dict[str, int]:
        used = gw.node_usage()
        return {
            "nodes_reporting": len(used),
            "cpu_millicores_total": int(sum(u.cpu_millicores for u in used.values())),
            "memory_bytes_total": sum(u.memory_bytes for u in used.values()),
        }

    with session_scope(services.session_factory) as s:
        rows = s.execute(select(Incident.status, func.count()).group_by(Incident.status)).all()
    by_status = {str(st): n for st, n in rows}
    return ClusterOverview(
        kubernetes_version=version,
        nodes=_section(nodes),
        pods=_section(pods),
        deployments=_section(deployments),
        incidents={st.value: by_status.get(st.value, 0) for st in IncidentStatus},
        resource_metrics=_section(usage),
    )
