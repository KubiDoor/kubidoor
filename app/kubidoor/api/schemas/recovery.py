"""Recovery API schemas."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from kubidoor.core.types import ActionType


class RecoveryActionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    action_id: str = Field(validation_alias="id")
    incident_id: str
    action_type: str
    resource_type: str
    resource_name: str
    resource_uid: str | None
    namespace: str | None
    requested_by: str
    approved_by: str | None
    status: str
    dry_run: bool
    requested_at: datetime
    executed_at: datetime | None
    verified_at: datetime | None
    result: dict[str, Any] | None
    error_message: str | None


class ActionRequest(BaseModel):
    incident_id: str = Field(min_length=36, max_length=36)
    action_type: ActionType


class RejectRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)
