"""ORM models: incidents, recovery_actions, monitoring_history, audit_logs."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from kubidoor.database.base import Base, UTCDateTime


class Incident(Base):
    __tablename__ = "incidents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_name: Mapped[str] = mapped_column(String(253), nullable=False)
    resource_uid: Mapped[str | None] = mapped_column(String(64))
    namespace: Mapped[str | None] = mapped_column(String(63))
    problem_type: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    probable_cause: Mapped[str] = mapped_column(Text, nullable=False, default="")
    recommended_action: Mapped[str] = mapped_column(Text, nullable=False, default="")
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    first_detected_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    last_detected_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    reopen_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    acknowledged_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    acknowledged_by: Mapped[str | None] = mapped_column(String(64))
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    resolved_by: Mapped[str | None] = mapped_column(String(64))
    resolution_note: Mapped[str | None] = mapped_column(Text)

    actions: Mapped[list["RecoveryAction"]] = relationship(back_populates="incident")

    __table_args__ = (
        Index("ix_incidents_status_severity", "status", "severity"),
        Index("ix_incidents_namespace", "namespace"),
        Index("ix_incidents_resource_type", "resource_type"),
        Index("ix_incidents_last_detected_at", "last_detected_at"),
    )


class RecoveryAction(Base):
    __tablename__ = "recovery_actions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)  # action_id
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id"), nullable=False)
    action_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_name: Mapped[str] = mapped_column(String(253), nullable=False)
    resource_uid: Mapped[str | None] = mapped_column(String(64))
    namespace: Mapped[str | None] = mapped_column(String(63))
    requested_by: Mapped[str] = mapped_column(String(64), nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    requested_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    executed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_message: Mapped[str | None] = mapped_column(Text)

    incident: Mapped[Incident] = relationship(back_populates="actions")

    __table_args__ = (
        Index("ix_recovery_actions_incident_id", "incident_id"),
        Index("ix_recovery_actions_status", "status"),
        Index("ix_recovery_actions_executed_at", "executed_at"),
        Index(
            "ix_recovery_actions_target",
            "resource_type",
            "namespace",
            "resource_name",
            "executed_at",
        ),
    )


class MonitoringHistory(Base):
    __tablename__ = "monitoring_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    finished_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    duration_seconds: Mapped[float] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # ok | partial | failed
    findings_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    incidents_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    incidents_reopened: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    incidents_resolved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errors: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    __table_args__ = (Index("ix_monitoring_history_started_at", "started_at"),)


class AuditLog(Base):
    """Append-only at the application level (see ``kubidoor.audit.service``)."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    incident_id: Mapped[str | None] = mapped_column(String(36))
    action_id: Mapped[str | None] = mapped_column(String(36))
    resource_type: Mapped[str | None] = mapped_column(String(32))
    resource_name: Mapped[str | None] = mapped_column(String(253))
    namespace: Mapped[str | None] = mapped_column(String(63))
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    __table_args__ = (
        Index("ix_audit_logs_incident_id", "incident_id"),
        Index("ix_audit_logs_action_id", "action_id"),
        Index("ix_audit_logs_timestamp", "timestamp"),
        Index("ix_audit_logs_event_type", "event_type"),
    )
