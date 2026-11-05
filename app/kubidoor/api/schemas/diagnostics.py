"""Diagnostics API schemas."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class DiagnosisOut(BaseModel):
    problem_type: str
    severity: str
    title: str
    observed_facts: list[str]
    probable_cause: str
    confidence: Literal["high", "medium", "low"]
    recommended_actions: list[str]


class DiagnosticsResponse(BaseModel):
    resource_type: str
    namespace: str | None
    resource_name: str
    resource_uid: str | None
    healthy: bool
    state_facts: list[str]
    diagnoses: list[DiagnosisOut]
    resource_metrics_available: bool
    resource_metrics_note: str
    generated_at: datetime | None
