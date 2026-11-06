"""Recovery endpoints. Policy is enforced by the engine; routes only translate HTTP."""

from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from kubidoor.api.dependencies import LimitQuery, OffsetQuery, ServicesDep
from kubidoor.api.schemas.common import Page
from kubidoor.api.schemas.recovery import ActionRequest, RecoveryActionOut, RejectRequest
from kubidoor.core.config import RecoverySettings
from kubidoor.core.security import ActorDep
from kubidoor.core.types import ActionStatus

router = APIRouter(prefix="/recovery", tags=["recovery"])


@router.get("/policy", response_model=RecoverySettings)
def get_policy(services: ServicesDep) -> RecoverySettings:
    """The active recovery safety policy (read-only; changed only through configuration)."""
    return services.settings.recovery


@router.get("/actions", response_model=Page[RecoveryActionOut])
def list_actions(
    services: ServicesDep,
    status_: Annotated[ActionStatus | None, Query(alias="status")] = None,
    incident_id: Annotated[str | None, Query()] = None,
    limit: LimitQuery = 50,
    offset: OffsetQuery = 0,
) -> Page[RecoveryActionOut]:
    rows, total = services.recovery.list_actions(
        status=status_, incident_id=incident_id, limit=limit, offset=offset
    )
    return Page[RecoveryActionOut](
        items=[RecoveryActionOut.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("/actions", response_model=RecoveryActionOut)
def request_action(
    body: ActionRequest, services: ServicesDep, actor: ActorDep, response: Response
) -> RecoveryActionOut:
    """Request a recovery action for an incident (policy-checked; usually needs approval)."""
    action, created = services.recovery.request_action(body.incident_id, body.action_type, actor)
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return RecoveryActionOut.model_validate(action)


@router.get("/actions/{action_id}", response_model=RecoveryActionOut)
def get_action(action_id: str, services: ServicesDep) -> RecoveryActionOut:
    return RecoveryActionOut.model_validate(services.recovery.get_action(action_id))


@router.post("/actions/{action_id}/approve", response_model=RecoveryActionOut)
def approve(action_id: str, services: ServicesDep, actor: ActorDep) -> RecoveryActionOut:
    return RecoveryActionOut.model_validate(services.recovery.approve(action_id, actor))


@router.post("/actions/{action_id}/reject", response_model=RecoveryActionOut)
def reject(
    action_id: str, services: ServicesDep, actor: ActorDep, body: RejectRequest | None = None
) -> RecoveryActionOut:
    reason = body.reason if body else None
    return RecoveryActionOut.model_validate(services.recovery.reject(action_id, actor, reason))


@router.post(
    "/actions/{action_id}/execute", response_model=RecoveryActionOut, status_code=status.HTTP_200_OK
)
def execute(action_id: str, services: ServicesDep, actor: ActorDep) -> RecoveryActionOut:
    """Claim and run an APPROVED action. Verification continues in the background; poll GET.

    The response may show RUNNING while the outcome is being verified.
    """
    action = services.recovery.execute(action_id, actor)
    if action.status == ActionStatus.RUNNING:
        services.recovery.start_verification(action.id)
    return RecoveryActionOut.model_validate(action)
