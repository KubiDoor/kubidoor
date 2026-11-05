"""Incident API schemas."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from kubidoor.api.schemas.recovery import RecoveryActionOut


class IncidentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    fingerprint: str
    resource_type: str
    resource_name: str
    resource_uid: str | None
    namespace: str | None
    problem_type: str
    severity: str
    status: str
    title: str
    description: str
    probable_cause: str
    recommended_action: str
    evidence: dict[str, Any]
    first_detected_at: datetime
    last_detected_at: datetime
    occurrence_count: int
    reopen_count: int
    acknowledged_at: datetime | None
    acknowledged_by: str | None
    resolved_at: datetime | None
    resolved_by: str | None
    resolution_note: str | None


class HistoryEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    timestamp: datetime
    event_type: str
    actor: str
    action_id: str | None
    details: dict[str, Any] | None


class IncidentDetail(IncidentOut):
    history: list[HistoryEntry]
    recovery_actions: list[RecoveryActionOut]


class NoteRequest(BaseModel):
    note: str | None = Field(default=None, max_length=500)
