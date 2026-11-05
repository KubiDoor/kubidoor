"""Recovery safety policy. Pure functions: each returns a list of human-readable violations
(empty list = allowed). Nothing here touches Kubernetes or the database, so every rule is
unit-testable in isolation; the engine feeds in live facts.
"""

from datetime import datetime, timedelta

from kubidoor.core.config import RecoverySettings
from kubidoor.core.types import ActionStatus, ActionType, ResourceType
from kubidoor.kubernetes.models import DeploymentInfo, PodInfo

# Which resource type each action may ever target. There is deliberately no action that can
# touch Nodes, PVs, PVCs, Secrets, RBAC objects, resource limits, or run shell commands.
ACTION_TARGETS: dict[ActionType, ResourceType] = {
    ActionType.DEPLOYMENT_RESTART: ResourceType.DEPLOYMENT,
    ActionType.POD_DELETE: ResourceType.POD,
}

# A pod may only be deleted if a controller of one of these kinds will recreate it.
RECREATING_OWNERS = frozenset({"ReplicaSet", "StatefulSet", "DaemonSet", "Job"})

# Statuses of actions that really touched the cluster (dry-runs and cancellations never count).
EXECUTED_STATUSES = (ActionStatus.RUNNING, ActionStatus.SUCCEEDED, ActionStatus.FAILED)


def evaluate_static(
    cfg: RecoverySettings,
    action_type: ActionType,
    resource_type: ResourceType,
    namespace: str | None,
) -> list[str]:
    """Checks that need no cluster access: enabled, allowlisted, right target, namespace rules."""
    violations: list[str] = []
    if not cfg.enabled:
        violations.append("recovery is globally disabled (recovery.enabled=false)")
    if action_type not in cfg.allowed_actions:
        violations.append(f"action '{action_type.value}' is not in recovery.allowed_actions")
    expected = ACTION_TARGETS.get(action_type)
    if expected is None or resource_type != expected:
        violations.append(
            f"action '{action_type.value}' is not appropriate for resource type "
            f"'{resource_type.value}'"
        )
    if not namespace:
        violations.append("target has no namespace")
    else:
        if namespace in cfg.blocked_namespaces:
            violations.append(f"namespace '{namespace}' is blocked (recovery.blocked_namespaces)")
        if namespace not in cfg.allowed_namespaces:
            violations.append(
                f"namespace '{namespace}' is not explicitly permitted (recovery.allowed_namespaces)"
            )
    return violations


def evaluate_approval(cfg: RecoverySettings, approved_by: str | None) -> list[str]:
    if cfg.require_approval and not approved_by:
        return ["approval is required but this action has not been approved"]
    return []


def evaluate_limits(
    cfg: RecoverySettings,
    now: datetime,
    last_target_execution: datetime | None,
    executions_in_last_hour: int,
    consecutive_failures: int,
) -> list[str]:
    """Cooldown, hourly rate limit and failed-action loop prevention."""
    violations: list[str] = []
    if last_target_execution is not None:
        remaining = last_target_execution + timedelta(seconds=cfg.cooldown_seconds) - now
        if remaining > timedelta(0):
            violations.append(
                f"cooldown active for this target: {int(remaining.total_seconds())}s remaining"
            )
    if executions_in_last_hour >= cfg.max_actions_per_hour:
        violations.append(
            f"rate limit reached: {executions_in_last_hour} action(s) in the last hour "
            f"(max {cfg.max_actions_per_hour})"
        )
    if consecutive_failures >= cfg.max_consecutive_failures:
        violations.append(
            f"{consecutive_failures} consecutive failed actions on this target; "
            "automatic retries are stopped until investigated"
        )
    return violations


def evaluate_pod_target(live: PodInfo | None, expected_uid: str | None) -> list[str]:
    if live is None:
        return ["target pod no longer exists (it may already have been replaced)"]
    violations: list[str] = []
    if expected_uid is None or live.uid != expected_uid:
        violations.append(
            f"pod UID mismatch: expected {expected_uid}, found {live.uid}; refusing to act on a "
            "different object"
        )
    if live.deletion_timestamp is not None:
        violations.append("pod is already terminating")
    if live.owner_kind not in RECREATING_OWNERS:
        violations.append(
            "pod has no controller that would recreate it "
            f"(owner: {live.owner_kind or 'none'}); deleting it would destroy the workload"
        )
    return violations


def evaluate_deployment_target(live: DeploymentInfo | None, expected_uid: str | None) -> list[str]:
    if live is None:
        return ["target deployment no longer exists"]
    violations: list[str] = []
    if expected_uid is None or live.uid != expected_uid:
        violations.append(
            f"deployment UID mismatch: expected {expected_uid}, found {live.uid}; refusing to "
            "act on a different object"
        )
    if live.replicas == 0:
        violations.append("deployment is scaled to zero; a restart would have no effect")
    if live.paused:
        violations.append("deployment is paused")
    return violations
