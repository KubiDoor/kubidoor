"""Composition root: builds and wires all services (manual dependency injection)."""

from dataclasses import dataclass

from sqlalchemy import Engine

from kubidoor.audit.service import AuditService
from kubidoor.core.clock import Clock, utcnow
from kubidoor.core.config import Settings
from kubidoor.database.session import SessionFactory, create_db_engine, create_session_factory
from kubidoor.diagnostics.engine import DiagnosticEngine
from kubidoor.incidents.service import IncidentService
from kubidoor.kubernetes.client import KubeClient, KubeGateway
from kubidoor.metrics.prometheus import Metrics
from kubidoor.monitoring.engine import MonitoringEngine
from kubidoor.monitoring.scheduler import MonitoringScheduler
from kubidoor.recovery.engine import RecoveryEngine


@dataclass(slots=True)
class Services:
    settings: Settings
    db_engine: Engine
    session_factory: SessionFactory
    metrics: Metrics
    gateway: KubeGateway
    audit: AuditService
    incidents: IncidentService
    diagnostics: DiagnosticEngine
    recovery: RecoveryEngine
    monitoring: MonitoringEngine
    scheduler: MonitoringScheduler


def build_services(
    settings: Settings,
    gateway: KubeGateway | None = None,
    clock: Clock = utcnow,
    metrics: Metrics | None = None,
) -> Services:
    db_engine = create_db_engine(settings.database_url)
    factory = create_session_factory(db_engine)
    metrics = metrics or Metrics()
    metrics.bind_incident_gauge(factory)
    gateway = gateway or KubeClient(settings.kubernetes)
    audit = AuditService(clock)
    incidents = IncidentService(factory, audit, metrics, clock)
    diagnostics = DiagnosticEngine(gateway, settings.monitoring, clock)
    recovery = RecoveryEngine(settings.recovery, factory, gateway, audit, incidents, metrics, clock)
    monitoring = MonitoringEngine(
        settings, gateway, incidents, diagnostics, factory, metrics, recovery, clock
    )
    scheduler = MonitoringScheduler(monitoring, settings.monitoring.interval_seconds)
    return Services(
        settings=settings,
        db_engine=db_engine,
        session_factory=factory,
        metrics=metrics,
        gateway=gateway,
        audit=audit,
        incidents=incidents,
        diagnostics=diagnostics,
        recovery=recovery,
        monitoring=monitoring,
        scheduler=scheduler,
    )
