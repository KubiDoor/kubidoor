"""The only two mutating operations KubiDoor can perform, plus their dry-run descriptions."""

from datetime import datetime
from typing import Any

from kubidoor.core.types import ActionType
from kubidoor.kubernetes.client import KubeGateway


def describe(action_type: ActionType, namespace: str, name: str) -> str:
    if action_type == ActionType.DEPLOYMENT_RESTART:
        return f"Would trigger a rollout restart of deployment {namespace}/{name}"
    return f"Would delete pod {namespace}/{name} (UID-guarded) so its controller recreates it"


def perform(
    gateway: KubeGateway,
    action_type: ActionType,
    namespace: str,
    name: str,
    uid: str,
    now: datetime,
) -> dict[str, Any]:
    """Execute exactly one action against exactly one named, UID-checked object."""
    if action_type == ActionType.DEPLOYMENT_RESTART:
        restarted_at = now.isoformat()
        gateway.restart_deployment(namespace, name, uid, restarted_at)
        return {"operation": "rollout_restart", "restarted_at": restarted_at}
    if action_type == ActionType.POD_DELETE:
        gateway.delete_pod(namespace, name, uid)
        return {"operation": "pod_delete"}
    raise ValueError(f"unsupported action type: {action_type}")  # unreachable: enum is closed
