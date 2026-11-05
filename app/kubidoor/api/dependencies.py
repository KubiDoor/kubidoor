"""FastAPI dependencies."""

from typing import Annotated

from fastapi import Depends, Query, Request

from kubidoor.container import Services


def get_services(request: Request) -> Services:
    services: Services = request.app.state.services
    return services


ServicesDep = Annotated[Services, Depends(get_services)]
LimitQuery = Annotated[int, Query(ge=1, le=500, description="Page size")]
OffsetQuery = Annotated[int, Query(ge=0, description="Items to skip")]
