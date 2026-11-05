"""Monitoring engine: one ``run_cycle`` inspects the cluster and reconciles incidents.

Safety design
* Each resource type is listed independently. A type that fails to list is recorded as an error
  and excluded from the cycle's ``Scope``, so its incidents are neither created nor resolved.
  A Kubernetes API outage (or missing RBAC) can therefore never be read as "everything healthy".
* Auto-resolution is limited to the resource types/namespaces actually observed.
* A database failure is recorded and counted; it never crashes the scheduler.
"""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Protocol, TypeVar

from sqlalchemy import delete
from sqlalchemy.exc import SQLAlchemyError

from kubidoor.core.clock import Clock, utcnow
from kubidoor.core.config import Settings
from kubidoor.core.exceptions import KubernetesError, MetricsUnavailableError
from kubidoor.core.types import ResourceType
from kubidoor.database.models import MonitoringHistory
from kubidoor.database.session import SessionFactory, session_scope
from kubidoor.diagnostics.engine import DiagnosticEngine
from kubidoor.incidents.models import Finding, Scope, SyncResult
from kubidoor.incidents.service import IncidentService
from kubidoor.kubernetes.client import KubeGateway
from kubidoor.kubernetes.models import EventInfo
from kubidoor.logging.setup import get_logger, redact
from kubidoor.metrics.prometheus import Metrics
from kubidoor.monitoring.checks import check_deployment, check_node, check_pod, check_pvc

logger = get_logger("monitoring")
T = TypeVar("T")


class RecoveryProposer(Protocol):
    def propose_for_incident(self, incident_id: str) -> None: ...


@dataclass(slots=True)
class CycleResult:
    skipped: bool = False
    status: str = "ok"  # ok | partial | failed
    findings: int = 0
    sync: SyncResult = field(default_factory=SyncResult)
    errors: dict[str, str] = field(default_factory=dict)
    duration_seconds: float = 0.0


class MonitoringEngine:
    def __init__(
        self,
        settings: Settings,
        gateway: KubeGateway,
        incidents: IncidentService,
        diagnostics: DiagnosticEngine,
        session_factory: SessionFactory,
        metrics: Metrics,
        recovery: RecoveryProposer | None = None,
        clock: Clock = utcnow,
    ) -> None:
        self._settings = settings
        self._gw = gateway
        self._incidents = incidents
        self._diag = diagnostics
        self._factory = session_factory
        self._metrics = metrics
        self._recovery = recovery
        self._clock = clock
        self._lock = threading.Lock()

    def run_cycle(self) -> CycleResult:
        if not self._lock.acquire(blocking=False):
            logger.warning("monitoring cycle skipped: previous cycle still running")
            return CycleResult(skipped=True)
        try:
            return self._run()
        finally:
            self._lock.release()

    # ------------------------------------------------------------------------------------
    def _run(self) -> CycleResult:
        cfg = self._settings.monitoring
        started = self._clock()
        t0 = time.perf_counter()
        result = CycleResult()
        namespaces = cfg.namespaces or None

        def collect(stage: str, fn: Callable[[], T]) -> T | None:
            try:
                return fn()
            except KubernetesError as exc:
                result.errors[stage] = redact(exc.message)
                self._metrics.monitoring_errors_total.labels(stage=stage).inc()
                logger.error("monitoring stage %s failed: %s", stage, exc.message)
                return None

        pods = collect("pods", lambda: self._gw.list_pods(namespaces))
        deployments = collect("deployments", lambda: self._gw.list_deployments(namespaces))
        pvcs = collect("pvcs", lambda: self._gw.list_pvcs(namespaces))
        nodes = collect("nodes", self._gw.list_nodes) if cfg.monitor_nodes else None

        observed: set[ResourceType] = set()
        if pods is not None:
            observed.add(ResourceType.POD)
        if deployments is not None:
            observed.add(ResourceType.DEPLOYMENT)
        if pvcs is not None:
            observed.add(ResourceType.PVC)
        if nodes is not None:
            observed.add(ResourceType.NODE)

        events_cache: list[list[EventInfo]] = []

        def get_events() -> list[EventInfo]:
            if not events_cache:
                events_cache.append(
                    collect("events", lambda: self._gw.list_events(namespaces)) or []
                )
            return events_cache[0]

        now = self._clock()
        findings: list[Finding] = []
        for pod in pods or []:
            findings.extend(check_pod(pod, now, cfg))
        for node in nodes or []:
            findings.extend(check_node(node, now))
        for dep in deployments or []:
            findings.extend(check_deployment(dep, now, cfg))
        for pvc in pvcs or []:
            events = get_events() if pvc.phase == "Pending" else []
            findings.extend(check_pvc(pvc, now, cfg, events))

        result.findings = len(findings)
        findings = self._enrich(findings, pods, nodes, get_events)

        scope = Scope(
            resource_types=frozenset(observed),
            namespaces=frozenset(cfg.namespaces) if cfg.namespaces else None,
        )
        try:
            if observed:
                result.sync = self._incidents.sync(findings, scope)
        except SQLAlchemyError:
            result.errors["database"] = "database error while updating incidents"
            self._metrics.monitoring_errors_total.labels(stage="database").inc()
            logger.exception("monitoring cycle could not persist incidents")

        self._propose_recovery(result.sync)

        result.duration_seconds = time.perf_counter() - t0
        if not observed or "database" in result.errors:
            result.status = "failed"
        elif result.errors:
            result.status = "partial"
        self._metrics.monitoring_cycles_total.inc()
        self._metrics.monitoring_cycle_duration_seconds.observe(result.duration_seconds)
        self._record_history(started, result)
        logger.info(
            "monitoring cycle %s: %d finding(s), %d created, %d reopened, %d resolved in %.2fs",
            result.status,
            result.findings,
            len(result.sync.created),
            len(result.sync.reopened),
            len(result.sync.resolved),
            result.duration_seconds,
        )
        return result

    def _enrich(
        self,
        findings: list[Finding],
        pods: Any,
        nodes: Any,
        get_events: Callable[[], list[EventInfo]],
    ) -> list[Finding]:
        if not findings:
            return findings
        events = get_events()
        usage: dict[tuple[str, str], dict[str, Any]] | None = None
        usage_note = "Resource metrics were not needed for these findings"
        if any(f.problem_type == "OOMKilled" for f in findings):
            try:
                usage = self._gw.pod_usage(None)
                usage_note = "metrics-server data available"
            except MetricsUnavailableError as exc:
                usage_note = exc.message
            except KubernetesError as exc:  # transport failure while reading metrics
                usage_note = exc.message
        enriched: list[Finding] = []
        for f in findings:
            ctx = self._diag.context_for(
                f, events, nodes=nodes, pods=pods, pod_usage=usage, usage_note=usage_note
            )
            enriched.append(self._diag.enrich(f, ctx))
        return enriched

    def _propose_recovery(self, sync: SyncResult) -> None:
        if self._recovery is None or not self._settings.recovery.enabled:
            return
        for incident_id in [*sync.created, *sync.reopened]:
            try:
                self._recovery.propose_for_incident(incident_id)
            except Exception:  # a proposal problem must never break monitoring
                logger.exception("recovery proposal failed", extra={"incident_id": incident_id})

    def _record_history(self, started: Any, result: CycleResult) -> None:
        cfg = self._settings.monitoring
        finished = self._clock()
        try:
            with session_scope(self._factory) as session:
                session.add(
                    MonitoringHistory(
                        started_at=started,
                        finished_at=finished,
                        duration_seconds=result.duration_seconds,
                        status=result.status,
                        findings_count=result.findings,
                        incidents_created=len(result.sync.created),
                        incidents_reopened=len(result.sync.reopened),
                        incidents_resolved=len(result.sync.resolved),
                        errors=result.errors or None,
                    )
                )
                session.execute(
                    delete(MonitoringHistory).where(
                        MonitoringHistory.started_at
                        < finished - timedelta(days=cfg.history_retention_days)
                    )
                )
        except SQLAlchemyError:
            logger.exception("could not record monitoring history")
