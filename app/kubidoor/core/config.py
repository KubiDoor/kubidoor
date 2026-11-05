"""Centralised, validated configuration.

Every setting is read from environment variables prefixed ``KUBIDOOR_`` (nested values use
``__``, e.g. ``KUBIDOOR_RECOVERY__ENABLED=true``). List values must be JSON, e.g.
``KUBIDOOR_RECOVERY__ALLOWED_NAMESPACES='["kubidoor-demo"]'``. A local ``.env`` file is also read.
"""

from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from kubidoor.core.types import ActionType


class RecoverySettings(BaseModel):
    """Safe defaults: recovery is OFF, dry-run, approval required, kube-system blocked."""

    enabled: bool = False
    dry_run: bool = True
    require_approval: bool = True
    allowed_namespaces: list[str] = Field(default_factory=lambda: ["demo"])
    blocked_namespaces: list[str] = Field(default_factory=lambda: ["kube-system"])
    allowed_actions: list[ActionType] = Field(
        default_factory=lambda: [ActionType.DEPLOYMENT_RESTART, ActionType.POD_DELETE]
    )
    cooldown_seconds: int = Field(300, ge=0)
    max_actions_per_hour: int = Field(5, ge=0)
    verify_after_action: bool = True
    verification_timeout_seconds: int = Field(120, ge=1)
    verification_poll_seconds: float = Field(5.0, gt=0)
    # Loop prevention: stop retrying a target after this many consecutive failed actions.
    max_consecutive_failures: int = Field(3, ge=1)


class MonitoringSettings(BaseModel):
    enabled: bool = True
    interval_seconds: float = Field(30.0, gt=0)
    # Empty list = monitor all namespaces.
    namespaces: list[str] = Field(default_factory=list)
    monitor_nodes: bool = True
    pending_threshold_seconds: int = Field(300, ge=0)
    not_ready_threshold_seconds: int = Field(120, ge=0)
    restart_threshold: int = Field(5, ge=1)
    # How long a past termination (OOMKilled, restart burst) keeps counting as "recent".
    recent_termination_seconds: int = Field(600, ge=0)
    deployment_grace_seconds: int = Field(300, ge=0)
    pvc_pending_threshold_seconds: int = Field(120, ge=0)
    history_retention_days: int = Field(7, ge=1)


class KubernetesSettings(BaseModel):
    kubeconfig: str | None = None
    context: str | None = None
    # None = auto (in-cluster first, then kubeconfig); True/False force one mode.
    in_cluster: bool | None = None
    request_timeout_seconds: int = Field(10, ge=1)
    metrics_enabled: bool = True


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="KUBIDOOR_",
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
    )

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    host: str = "127.0.0.1"
    port: int = Field(8000, ge=1, le=65535)
    database_url: str = "sqlite:///./kubidoor.db"
    auto_migrate: bool = True
    migrations_dir: str = "migrations"
    # Optional shared bearer token protecting /api/v1/*. /health, /ready, /metrics stay open.
    api_token: SecretStr | None = None

    monitoring: MonitoringSettings = Field(default_factory=MonitoringSettings)
    kubernetes: KubernetesSettings = Field(default_factory=KubernetesSettings)
    recovery: RecoverySettings = Field(default_factory=RecoverySettings)

    @field_validator("api_token", mode="before")
    @classmethod
    def _blank_token_means_disabled(cls, value: object) -> object:
        """An empty KUBIDOOR_API_TOKEN (e.g. from an unset docker-compose variable) disables auth
        instead of enabling it with an empty secret that nobody could present."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("database_url")
    @classmethod
    def _supported_database(cls, value: str) -> str:
        if not value.startswith(("sqlite", "postgresql")):
            raise ValueError("database_url must be a sqlite:// or postgresql:// URL")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
