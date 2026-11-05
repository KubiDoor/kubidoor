"""Append-only audit trail.

Records are written in the *same transaction* as the state change they describe. The service
exposes no update/delete API, and an ORM guard rejects any attempt to modify or delete an
``AuditLog`` through a Session.
"""

from typing import Any

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from kubidoor.core.clock import Clock, utcnow
from kubidoor.database.models import AuditLog
from kubidoor.logging.setup import get_logger, log_context

logger = get_logger("audit")

# Event types
INCIDENT_ACKNOWLEDGED = "INCIDENT_ACKNOWLEDGED"
INCIDENT_RESOLVED = "INCIDENT_RESOLVED"
INCIDENT_AUTO_RESOLVED = "INCIDENT_AUTO_RESOLVED"
INCIDENT_REOPENED = "INCIDENT_REOPENED"
INCIDENT_CREATED = "INCIDENT_CREATED"
RECOVERY_REQUESTED = "RECOVERY_REQUESTED"
RECOVERY_REQUEST_DENIED = "RECOVERY_REQUEST_DENIED"
RECOVERY_APPROVED = "RECOVERY_APPROVED"
RECOVERY_REJECTED = "RECOVERY_REJECTED"
RECOVERY_BLOCKED = "RECOVERY_BLOCKED"
RECOVERY_DRY_RUN = "RECOVERY_DRY_RUN"
RECOVERY_EXECUTED = "RECOVERY_EXECUTED"
RECOVERY_VERIFIED = "RECOVERY_VERIFIED"
RECOVERY_FAILED = "RECOVERY_FAILED"


class AuditImmutableError(RuntimeError):
    pass


@event.listens_for(Session, "before_flush")
def _guard_audit_logs(session: Session, _ctx: Any, _instances: Any) -> None:
    for obj in session.deleted:
        if isinstance(obj, AuditLog):
            raise AuditImmutableError("audit_logs is append-only: delete rejected")
    for obj in session.dirty:
        if isinstance(obj, AuditLog) and session.is_modified(obj):
            raise AuditImmutableError("audit_logs is append-only: update rejected")


class AuditService:
    def __init__(self, clock: Clock = utcnow) -> None:
        self._clock = clock

    def record(
        self,
        session: Session,
        event_type: str,
        actor: str,
        *,
        incident_id: str | None = None,
        action_id: str | None = None,
        resource_type: str | None = None,
        resource_name: str | None = None,
        namespace: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditLog:
        entry = AuditLog(
            timestamp=self._clock(),
            event_type=event_type,
            actor=actor,
            incident_id=incident_id,
            action_id=action_id,
            resource_type=resource_type,
            resource_name=resource_name,
            namespace=namespace,
            details=details,
        )
        session.add(entry)
        logger.info(
            "audit %s by %s",
            event_type,
            actor,
            extra=log_context(
                incident_id=incident_id,
                action_id=action_id,
                resource_type=resource_type,
                resource_name=resource_name,
                namespace=namespace,
            ),
        )
        return entry

    @staticmethod
    def for_incident(session: Session, incident_id: str, limit: int = 200) -> list[AuditLog]:
        stmt = (
            select(AuditLog)
            .where(AuditLog.incident_id == incident_id)
            .order_by(AuditLog.id.asc())
            .limit(limit)
        )
        return list(session.scalars(stmt))
