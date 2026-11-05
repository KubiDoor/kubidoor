"""Liveness, readiness and Prometheus metrics endpoints (unauthenticated by design)."""

from typing import Any

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST

from kubidoor.api.dependencies import ServicesDep
from kubidoor.core.exceptions import KubernetesError
from kubidoor.database.session import check_database

router = APIRouter()


@router.get("/health", tags=["health"], summary="Liveness probe")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready", tags=["health"], summary="Readiness probe")
def ready(services: ServicesDep) -> JSONResponse:
    checks: dict[str, Any] = {"database": check_database(services.db_engine)}
    detail: dict[str, Any] = {}
    try:
        detail["kubernetes_version"] = services.gateway.ping()
        checks["kubernetes"] = True
    except KubernetesError as exc:
        checks["kubernetes"] = False
        detail["kubernetes_error"] = exc.message
    ok = all(checks.values())
    return JSONResponse(
        status_code=200 if ok else 503,
        content={"status": "ready" if ok else "not_ready", "checks": checks, **detail},
    )


@router.get("/metrics", tags=["metrics"], summary="Prometheus metrics")
def metrics(services: ServicesDep) -> Response:
    return Response(services.metrics.render(), media_type=CONTENT_TYPE_LATEST)
