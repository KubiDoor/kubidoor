"""Application exceptions. Each carries the HTTP status and a stable error code for the API."""

from typing import Any


class KubiDoorError(Exception):
    status_code = 500
    code = "internal_error"

    def __init__(self, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class NotFoundError(KubiDoorError):
    status_code = 404
    code = "not_found"


class ConflictError(KubiDoorError):
    status_code = 409
    code = "conflict"


class PolicyViolationError(KubiDoorError):
    status_code = 403
    code = "policy_violation"

    def __init__(self, violations: list[str]) -> None:
        super().__init__("Recovery policy violation: " + "; ".join(violations), violations)
        self.violations = violations


class AuthenticationError(KubiDoorError):
    status_code = 401
    code = "unauthorized"


class ConfigurationError(KubiDoorError):
    status_code = 500
    code = "configuration_error"


class KubernetesError(KubiDoorError):
    """Base for failures talking to the Kubernetes API."""

    status_code = 503
    code = "kubernetes_error"


class KubernetesUnavailableError(KubernetesError):
    code = "kubernetes_unavailable"


class KubernetesPermissionError(KubernetesError):
    status_code = 502
    code = "kubernetes_permission_denied"


class KubernetesNotFoundError(KubernetesError):
    status_code = 404
    code = "kubernetes_not_found"


class KubernetesConflictError(KubernetesError):
    status_code = 409
    code = "kubernetes_conflict"


class MetricsUnavailableError(KubiDoorError):
    """Resource metrics (metrics-server) are not available. Never replaced by fake values."""

    status_code = 503
    code = "metrics_unavailable"
