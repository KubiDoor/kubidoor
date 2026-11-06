"""KubiDoor application entry point (FastAPI app factory)."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from kubidoor import __version__
from kubidoor.api.routes import cluster, diagnostics, health, incidents, recovery, resources
from kubidoor.api.schemas.common import ErrorResponse
from kubidoor.container import Services, build_services
from kubidoor.core.config import Settings, get_settings
from kubidoor.core.exceptions import KubiDoorError
from kubidoor.core.security import require_auth
from kubidoor.database.session import render_url, run_migrations
from kubidoor.logging.setup import configure_logging, get_logger

logger = get_logger("main")

UI_DIR = Path(__file__).parent / "ui" / "static"
UI_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


def _error(status: int, code: str, message: str, details: object = None) -> JSONResponse:
    body = ErrorResponse(error={"code": code, "message": message, "details": details})
    return JSONResponse(status_code=status, content=body.model_dump(mode="json"))


def create_app(settings: Settings | None = None, services: Services | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        svc = services or build_services(settings)
        app.state.services = svc
        if settings.auto_migrate:
            run_migrations(settings.database_url, settings.migrations_dir)
        interrupted = svc.recovery.fail_interrupted()
        if interrupted:
            logger.warning("marked %d interrupted recovery action(s) as FAILED", interrupted)
        logger.info(
            "KubiDoor %s starting: db=%s recovery_enabled=%s dry_run=%s require_approval=%s",
            __version__,
            render_url(settings.database_url),
            settings.recovery.enabled,
            settings.recovery.dry_run,
            settings.recovery.require_approval,
        )
        if settings.monitoring.enabled:
            svc.scheduler.start()
        try:
            yield
        finally:
            await svc.scheduler.stop()
            # In-flight recovery verifications must finish before the database goes away.
            unfinished = await asyncio.to_thread(svc.recovery.shutdown)
            if unfinished:
                logger.warning("%d recovery verification(s) unfinished at shutdown", unfinished)
            svc.db_engine.dispose()

    app = FastAPI(
        title="KubiDoor",
        version=__version__,
        description=(
            "KubiDoor is a lightweight Kubernetes operations tool for intelligent health "
            "monitoring, incident detection, root cause analysis, and safe automated recovery."
        ),
        lifespan=lifespan,
        responses={"default": {"model": ErrorResponse}},
    )
    # Services must exist before the lifespan runs for tests that call endpoints without it.
    if services is not None:
        app.state.services = services

    api = APIRouter(prefix="/api/v1", dependencies=[Depends(require_auth)])
    for module in (cluster, resources, incidents, diagnostics, recovery):
        api.include_router(module.router)
    app.include_router(health.router)
    app.include_router(api)

    # Web dashboard: static files only. All data still comes from the (token-protected) API.
    app.mount("/ui", StaticFiles(directory=UI_DIR, html=True), name="ui")

    @app.get("/", include_in_schema=False)
    def _root() -> RedirectResponse:
        return RedirectResponse("/ui/")

    @app.middleware("http")
    async def _ui_security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        if request.url.path.startswith("/ui"):
            response.headers["Content-Security-Policy"] = UI_CSP
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.exception_handler(KubiDoorError)
    async def _app_error(_: Request, exc: KubiDoorError) -> JSONResponse:
        if exc.status_code >= 500:
            logger.error("request failed: %s", exc.message)
        return _error(exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {"loc": list(e["loc"]), "message": e["msg"], "type": e["type"]} for e in exc.errors()
        ]
        return _error(422, "validation_error", "Request validation failed", details)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _error(exc.status_code, "http_error", str(exc.detail))

    @app.exception_handler(SQLAlchemyError)
    async def _db(_: Request, exc: SQLAlchemyError) -> JSONResponse:
        logger.error("database error: %s", type(exc).__name__)
        return _error(503, "database_error", "Database operation failed")

    return app


def run() -> None:
    """Console entry point: ``kubidoor`` (host/port from KUBIDOOR_HOST / KUBIDOOR_PORT)."""
    settings = get_settings()
    uvicorn.run(
        "kubidoor.main:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_config=None,
    )


if __name__ == "__main__":
    run()
