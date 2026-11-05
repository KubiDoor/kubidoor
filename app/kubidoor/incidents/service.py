"""Incident lifecycle: creation, deduplication, reopen, acknowledge, resolve.

Correctness rules implemented here
* Findings are deduplicated by deterministic fingerprint (one DB row per fingerprint, enforced
  by a UNIQUE constraint); repeated cycles only bump ``occurrence_count``/``last_detected_at``.
* A monitoring cycle only resolves incidents inside its ``Scope`` - the resource types it
  *successfully* listed. An API outage therefore never "heals" anything.
* A resolved incident whose fingerprint is detected again is reopened (same row, history kept).
* Each sync is a single transaction.
"""

import uuid
from datetime import datetime

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session

from kubidoor.audit import service as audit_events
from kubidoor.audit.service import AuditService
from kubidoor.core.clock import Clock, utcnow
from kubidoor.core.exceptions import ConflictError, NotFoundError
from kubidoor.core.types import IncidentStatus, ResourceType
from kubidoor.database.models import AuditLog, Incident, RecoveryAction
from kubidoor.database.session import SessionFactory, session_scope
from kubidoor.incidents.models import Finding, IncidentFilters, Scope, SyncResult
from kubidoor.logging.setup import get_logger, log_context
from kubidoor.metrics.prometheus import Metrics

logger = get_logger("incidents")

MONITOR_ACTOR = "monitor"


class IncidentService:
    def __init__(
        self,
        session_factory: SessionFactory,
        audit: AuditService,
        metrics: Metrics,
        clock: Clock = utcnow,
    ) -> None:
        self._factory = session_factory
        self._audit = audit
        self._metrics = metrics
        self._clock = clock

    # ------------------------------------------------------------------ monitoring sync --
    def sync(self, findings: list[Finding], scope: Scope) -> SyncResult:
        now = self._clock()
        result = SyncResult()
        unique = {f.fingerprint: f for f in findings}  # same problem seen twice => one finding

        with session_scope(self._factory) as session:
            existing = {
                i.fingerprint: i
                for i in session.scalars(
                    select(Incident).where(Incident.fingerprint.in_(list(unique)))
                )
            }
            for fingerprint, finding in unique.items():
                incident = existing.get(fingerprint)
                if incident is None:
                    result.created.append(self._create(session, finding, fingerprint, now))
                elif incident.status == IncidentStatus.RESOLVED:
                    self._reopen(session, incident, finding, now)
                    result.reopened.append(incident.id)
                else:
                    self._refresh(incident, finding, now)
                    result.updated.append(incident.id)

            result.resolved.extend(self._auto_resolve(session, set(unique), scope, now))
        return result

    def _create(self, session: Session, f: Finding, fingerprint: str, now: datetime) -> str:
        incident = Incident(
            id=str(uuid.uuid4()),
            fingerprint=fingerprint,
            resource_type=f.resource_type.value,
            resource_name=f.resource_name,
            resource_uid=f.resource_uid,
            namespace=f.namespace,
            problem_type=f.problem_type,
            severity=f.severity.value,
            status=IncidentStatus.OPEN.value,
            title=f.title,
            description=f.description,
            probable_cause=f.probable_cause,
            recommended_action=f.recommended_action,
            evidence=f.evidence,
            first_detected_at=now,
            last_detected_at=now,
            occurrence_count=1,
            reopen_count=0,
        )
        session.add(incident)
        self._audit.record(
            session,
            audit_events.INCIDENT_CREATED,
            MONITOR_ACTOR,
            incident_id=incident.id,
            resource_type=incident.resource_type,
            resource_name=incident.resource_name,
            namespace=incident.namespace,
            details={"problem_type": f.problem_type, "severity": f.severity.value},
        )
        self._metrics.incidents_detected_total.labels(
            severity=f.severity.value, resource_type=f.resource_type.value
        ).inc()
        logger.info(
            "incident created: %s",
            f.title,
            extra=log_context(
                incident_id=incident.id,
                resource_type=incident.resource_type,
                resource_name=incident.resource_name,
                namespace=incident.namespace,
            ),
        )
        return incident.id

    def _reopen(self, session: Session, incident: Incident, f: Finding, now: datetime) -> None:
        self._refresh(incident, f, now)
        incident.status = IncidentStatus.OPEN.value
        incident.resolved_at = None
        incident.resolved_by = None
        incident.acknowledged_at = None
        incident.acknowledged_by = None
        incident.reopen_count += 1
        self._audit.record(
            session,
            audit_events.INCIDENT_REOPENED,
            MONITOR_ACTOR,
            incident_id=incident.id,
            resource_type=incident.resource_type,
            resource_name=incident.resource_name,
            namespace=incident.namespace,
            details={"reopen_count": incident.reopen_count},
        )
        self._metrics.incidents_detected_total.labels(
            severity=f.severity.value, resource_type=f.resource_type.value
        ).inc()
        logger.info(
            "incident reopened: %s",
            incident.title,
            extra=log_context(incident_id=incident.id, namespace=incident.namespace),
        )

    @staticmethod
    def _refresh(incident: Incident, f: Finding, now: datetime) -> None:
        incident.last_detected_at = now
        incident.occurrence_count += 1
        incident.resource_uid = f.resource_uid
        incident.severity = f.severity.value
        incident.title = f.title
        incident.description = f.description
        incident.probable_cause = f.probable_cause
        incident.recommended_action = f.recommended_action
        incident.evidence = f.evidence

    def _auto_resolve(
        self, session: Session, seen: set[str], scope: Scope, now: datetime
    ) -> list[str]:
        if not scope.resource_types:
            return []
        stmt = select(Incident).where(
            Incident.status != IncidentStatus.RESOLVED.value,
            Incident.resource_type.in_([t.value for t in scope.resource_types]),
        )
        resolved: list[str] = []
        for incident in session.scalars(stmt):
            if incident.fingerprint in seen:
                continue
            if (
                scope.namespaces is not None
                and incident.namespace is not None
                and incident.namespace not in scope.namespaces
            ):
                continue  # outside what this cycle observed
            self.resolve_in_session(
                session,
                incident,
                actor=MONITOR_ACTOR,
                note="Resource observed healthy (or gone) in a successful monitoring cycle",
                event_type=audit_events.INCIDENT_AUTO_RESOLVED,
                now=now,
            )
            resolved.append(incident.id)
        return resolved

    # --------------------------------------------------------------------------- queries --
    @staticmethod
    def _filtered(filters: IncidentFilters) -> list[ColumnElement[bool]]:
        conditions: list[ColumnElement[bool]] = []
        if filters.severity:
            conditions.append(Incident.severity == filters.severity.value)
        if filters.status:
            conditions.append(Incident.status == filters.status)
        if filters.namespace:
            conditions.append(Incident.namespace == filters.namespace)
        if filters.resource_type:
            conditions.append(Incident.resource_type == filters.resource_type.value)
        if filters.active_only:
            conditions.append(Incident.status != IncidentStatus.RESOLVED.value)
        return conditions

    def list_incidents(
        self, filters: IncidentFilters, limit: int, offset: int
    ) -> tuple[list[Incident], int]:
        where = self._filtered(filters)
        with session_scope(self._factory) as session:
            total = session.scalar(select(func.count()).select_from(Incident).where(*where)) or 0
            rows = session.scalars(
                select(Incident)
                .where(*where)
                .order_by(Incident.last_detected_at.desc(), Incident.id)
                .limit(limit)
                .offset(offset)
            ).all()
        return list(rows), total

    def get(self, incident_id: str) -> Incident:
        with session_scope(self._factory) as session:
            incident = session.get(Incident, incident_id)
            if incident is None:
                raise NotFoundError(f"Incident {incident_id} not found")
            return incident

    def get_detail(self, incident_id: str) -> tuple[Incident, list[AuditLog], list[RecoveryAction]]:
        with session_scope(self._factory) as session:
            incident = session.get(Incident, incident_id)
            if incident is None:
                raise NotFoundError(f"Incident {incident_id} not found")
            history = self._audit.for_incident(session, incident_id)
            actions = list(
                session.scalars(
                    select(RecoveryAction)
                    .where(RecoveryAction.incident_id == incident_id)
                    .order_by(RecoveryAction.requested_at.asc())
                )
            )
            return incident, history, actions

    # -------------------------------------------------------------------- manual changes --
    def acknowledge(self, incident_id: str, actor: str, note: str | None = None) -> Incident:
        with session_scope(self._factory) as session:
            incident = session.get(Incident, incident_id)
            if incident is None:
                raise NotFoundError(f"Incident {incident_id} not found")
            if incident.status == IncidentStatus.RESOLVED:
                raise ConflictError("Incident is already resolved; it cannot be acknowledged")
            if incident.acknowledged_at is not None:
                return incident  # idempotent
            incident.acknowledged_at = self._clock()
            incident.acknowledged_by = actor
            if incident.status == IncidentStatus.OPEN:
                incident.status = IncidentStatus.ACKNOWLEDGED.value
            self._audit.record(
                session,
                audit_events.INCIDENT_ACKNOWLEDGED,
                actor,
                incident_id=incident.id,
                resource_type=incident.resource_type,
                resource_name=incident.resource_name,
                namespace=incident.namespace,
                details={"note": note} if note else None,
            )
            return incident

    def resolve(self, incident_id: str, actor: str, note: str | None = None) -> Incident:
        """Manual resolution. If the problem still exists the next cycle reopens the incident."""
        with session_scope(self._factory) as session:
            incident = session.get(Incident, incident_id)
            if incident is None:
                raise NotFoundError(f"Incident {incident_id} not found")
            if incident.status == IncidentStatus.RESOLVED:
                return incident  # idempotent
            self.resolve_in_session(
                session,
                incident,
                actor=actor,
                note=note,
                event_type=audit_events.INCIDENT_RESOLVED,
                now=self._clock(),
            )
            return incident

    def resolve_in_session(
        self,
        session: Session,
        incident: Incident,
        *,
        actor: str,
        note: str | None,
        event_type: str,
        now: datetime | None = None,
    ) -> None:
        incident.status = IncidentStatus.RESOLVED.value
        incident.resolved_at = now or self._clock()
        incident.resolved_by = actor
        incident.resolution_note = note
        self._audit.record(
            session,
            event_type,
            actor,
            incident_id=incident.id,
            resource_type=incident.resource_type,
            resource_name=incident.resource_name,
            namespace=incident.namespace,
            details={"note": note} if note else None,
        )

    @staticmethod
    def mark_investigating(incident: Incident) -> None:
        """A recovery action is in flight for this incident."""
        if incident.status in (IncidentStatus.OPEN, IncidentStatus.ACKNOWLEDGED):
            incident.status = IncidentStatus.INVESTIGATING.value

    @staticmethod
    def resource_type_of(incident: Incident) -> ResourceType:
        return ResourceType(incident.resource_type)
