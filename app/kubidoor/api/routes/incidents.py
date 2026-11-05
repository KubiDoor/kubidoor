"""Incident endpoints."""

from typing import Annotated

from fastapi import APIRouter, Query

from kubidoor.api.dependencies import LimitQuery, OffsetQuery, ServicesDep
from kubidoor.api.schemas.common import Page
from kubidoor.api.schemas.incidents import (
    HistoryEntry,
    IncidentDetail,
    IncidentOut,
    NoteRequest,
)
from kubidoor.api.schemas.recovery import RecoveryActionOut
from kubidoor.core.security import ActorDep
from kubidoor.core.types import IncidentStatus, ResourceType, Severity
from kubidoor.incidents.models import IncidentFilters

router = APIRouter(prefix="/incidents", tags=["incidents"])


@router.get("", response_model=Page[IncidentOut])
def list_incidents(
    services: ServicesDep,
    severity: Annotated[Severity | None, Query()] = None,
    status: Annotated[IncidentStatus | None, Query()] = None,
    namespace: Annotated[str | None, Query()] = None,
    resource_type: Annotated[ResourceType | None, Query()] = None,
    limit: LimitQuery = 50,
    offset: OffsetQuery = 0,
) -> Page[IncidentOut]:
    filters = IncidentFilters(
        severity=severity,
        status=status.value if status else None,
        namespace=namespace,
        resource_type=resource_type,
    )
    rows, total = services.incidents.list_incidents(filters, limit, offset)
    return Page[IncidentOut](
        items=[IncidentOut.model_validate(r) for r in rows], total=total, limit=limit, offset=offset
    )


@router.get("/{incident_id}", response_model=IncidentDetail)
def get_incident(incident_id: str, services: ServicesDep) -> IncidentDetail:
    incident, history, actions = services.incidents.get_detail(incident_id)
    return IncidentDetail(
        **IncidentOut.model_validate(incident).model_dump(),
        history=[HistoryEntry.model_validate(h) for h in history],
        recovery_actions=[RecoveryActionOut.model_validate(a) for a in actions],
    )


@router.post("/{incident_id}/acknowledge", response_model=IncidentOut)
def acknowledge(
    incident_id: str, services: ServicesDep, actor: ActorDep, body: NoteRequest | None = None
) -> IncidentOut:
    incident = services.incidents.acknowledge(incident_id, actor, body.note if body else None)
    return IncidentOut.model_validate(incident)


@router.post("/{incident_id}/resolve", response_model=IncidentOut)
def resolve(
    incident_id: str, services: ServicesDep, actor: ActorDep, body: NoteRequest | None = None
) -> IncidentOut:
    incident = services.incidents.resolve(incident_id, actor, body.note if body else None)
    return IncidentOut.model_validate(incident)
