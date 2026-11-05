"""Application metrics (prefix ``kubidoor_``).

Label values are always drawn from small fixed sets (severity, resource type, action type,
action status, monitoring stage). Pod UIDs, incident IDs, names and other unbounded values are
never used as labels.
"""

from collections.abc import Callable, Iterable

from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest
from prometheus_client.core import GaugeMetricFamily, Metric
from prometheus_client.registry import Collector
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from kubidoor.core.types import IncidentStatus, Severity
from kubidoor.database.models import Incident
from kubidoor.database.session import SessionFactory, session_scope
from kubidoor.logging.setup import get_logger

logger = get_logger("metrics")

# Fixed label vocabularies for monitoring errors.
MONITORING_STAGES = ("pods", "nodes", "deployments", "pvcs", "events", "database", "cycle")

_CYCLE_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60)
_RECOVERY_BUCKETS = (0.1, 0.5, 1, 5, 10, 30, 60, 120, 300)


class ActiveIncidentsCollector(Collector):
    """Computes kubidoor_active_incidents from the database at scrape time (always consistent)."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._factory = session_factory

    def collect(self) -> Iterable[Metric]:
        family = GaugeMetricFamily(
            "kubidoor_active_incidents",
            "Number of unresolved incidents by severity",
            labels=["severity"],
        )
        try:
            with session_scope(self._factory) as session:
                rows = session.execute(
                    select(Incident.severity, func.count())
                    .where(Incident.status != IncidentStatus.RESOLVED.value)
                    .group_by(Incident.severity)
                ).all()
        except SQLAlchemyError:
            logger.exception("could not read active incidents for /metrics")
            return []
        counts = {severity: count for severity, count in rows}
        for severity in Severity:
            family.add_metric([severity.value], float(counts.get(severity.value, 0)))
        return [family]


class Metrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()
        self.monitoring_cycles_total = Counter(
            "kubidoor_monitoring_cycles_total",
            "Completed monitoring cycles",
            registry=self.registry,
        )
        self.monitoring_errors_total = Counter(
            "kubidoor_monitoring_errors_total",
            "Errors during monitoring cycles, by stage",
            ["stage"],
            registry=self.registry,
        )
        self.monitoring_cycle_duration_seconds = Histogram(
            "kubidoor_monitoring_cycle_duration_seconds",
            "Duration of a monitoring cycle",
            buckets=_CYCLE_BUCKETS,
            registry=self.registry,
        )
        self.incidents_detected_total = Counter(
            "kubidoor_incidents_detected_total",
            "New or reopened incidents",
            ["severity", "resource_type"],
            registry=self.registry,
        )
        self.recovery_actions_total = Counter(
            "kubidoor_recovery_actions_total",
            "Recovery action state transitions, by action type and resulting status",
            ["action_type", "status"],
            registry=self.registry,
        )
        self.recovery_failures_total = Counter(
            "kubidoor_recovery_failures_total",
            "Recovery actions that failed (execution or verification)",
            ["action_type"],
            registry=self.registry,
        )
        self.recovery_duration_seconds = Histogram(
            "kubidoor_recovery_duration_seconds",
            "Time from execution to verified outcome",
            ["action_type"],
            buckets=_RECOVERY_BUCKETS,
            registry=self.registry,
        )

    def bind_incident_gauge(self, session_factory: SessionFactory) -> None:
        self.registry.register(ActiveIncidentsCollector(session_factory))

    def render(self) -> bytes:
        return generate_latest(self.registry)


MetricsGetter = Callable[[], Metrics]
