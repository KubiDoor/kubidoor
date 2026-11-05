"""API authentication helpers (optional shared bearer token) and actor identity."""

import hmac
import re
from typing import Annotated

from fastapi import Depends, Header, Request

from kubidoor.core.exceptions import AuthenticationError, KubiDoorError

_ACTOR_RE = re.compile(r"^[A-Za-z0-9_.@:-]{1,64}$")


class InvalidActorError(KubiDoorError):
    status_code = 400
    code = "invalid_actor"


def require_auth(request: Request, authorization: Annotated[str | None, Header()] = None) -> None:
    """When ``api_token`` is configured every /api/v1 call must present it as a Bearer token."""
    token = request.app.state.services.settings.api_token
    if token is None:
        return
    expected = token.get_secret_value()
    scheme, _, supplied = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(supplied.strip(), expected):
        raise AuthenticationError("Missing or invalid bearer token")


def get_actor(x_kubidoor_actor: Annotated[str | None, Header()] = None) -> str:
    """Identity recorded in audit logs. Self-asserted: pair with ``api_token`` for real auth."""
    actor = x_kubidoor_actor or "api"
    if not _ACTOR_RE.match(actor):
        raise InvalidActorError("X-KubiDoor-Actor must match [A-Za-z0-9_.@:-]{1,64}")
    return actor


AuthDep = Depends(require_auth)
ActorDep = Annotated[str, Depends(get_actor)]
