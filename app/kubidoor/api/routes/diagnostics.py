"""On-demand, read-only diagnostics."""

from fastapi import APIRouter

from kubidoor.api.dependencies import ServicesDep
from kubidoor.api.schemas.diagnostics import DiagnosisOut, DiagnosticsResponse
from kubidoor.core.exceptions import NotFoundError
from kubidoor.core.types import ResourceType

router = APIRouter(prefix="/diagnostics", tags=["diagnostics"])

_ALIASES = {
    "pod": ResourceType.POD,
    "pods": ResourceType.POD,
    "node": ResourceType.NODE,
    "nodes": ResourceType.NODE,
    "deployment": ResourceType.DEPLOYMENT,
    "deployments": ResourceType.DEPLOYMENT,
    "pvc": ResourceType.PVC,
    "persistentvolumeclaim": ResourceType.PVC,
}


@router.get("/{resource_type}/{namespace}/{resource_name}", response_model=DiagnosticsResponse)
def diagnose(
    resource_type: str, namespace: str, resource_name: str, services: ServicesDep
) -> DiagnosticsResponse:
    """Use ``_`` as the namespace for cluster-scoped resources (nodes)."""
    rtype = _ALIASES.get(resource_type.lower())
    if rtype is None:
        raise NotFoundError(
            f"Unknown resource type '{resource_type}'. Use one of: pod, node, deployment, pvc"
        )
    ns = None if namespace == "_" or rtype == ResourceType.NODE else namespace
    report = services.diagnostics.diagnose_resource(rtype, ns, resource_name)
    return DiagnosticsResponse(
        resource_type=report.resource_type.value,
        namespace=report.namespace,
        resource_name=report.resource_name,
        resource_uid=report.resource_uid,
        healthy=report.healthy,
        state_facts=report.state_facts,
        diagnoses=[
            DiagnosisOut(
                problem_type=e.problem_type,
                severity=e.severity,
                title=e.title,
                observed_facts=e.diagnosis.observed_facts,
                probable_cause=e.diagnosis.probable_cause,
                confidence=e.diagnosis.confidence,
                recommended_actions=e.diagnosis.recommended_actions,
            )
            for e in report.entries
        ],
        resource_metrics_available=report.metrics_available,
        resource_metrics_note=report.metrics_note,
        generated_at=report.generated_at,
    )
