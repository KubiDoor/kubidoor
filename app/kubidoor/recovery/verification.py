"""Post-action verification.

A successful API response proves nothing about recovery. These functions judge the *resulting
cluster state* and return pending / success / failed. ``wait_for_outcome`` polls until a verdict
or timeout (clock and sleep are injectable so tests run instantly and deterministically).
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from kubidoor.core.exceptions import KubernetesError
from kubidoor.core.types import ActionType
from kubidoor.kubernetes.client import KubeGateway
from kubidoor.kubernetes.models import DeploymentInfo, PodInfo

Verdict = Literal["pending", "success", "failed"]


@dataclass(frozen=True, slots=True)
class Outcome:
    verdict: Verdict
    detail: str


def judge_deployment_rollout(
    live: DeploymentInfo | None, expected_uid: str, generation_before: int
) -> Outcome:
    if live is None:
        return Outcome("failed", "deployment disappeared during verification")
    if live.uid != expected_uid:
        return Outcome("failed", "deployment was replaced by a different object")
    if live.observed_generation <= generation_before:
        return Outcome("pending", "controller has not yet observed the restart")
    progressing = live.condition("Progressing")
    if (
        progressing
        and progressing.status == "False"
        and progressing.reason == "ProgressDeadlineExceeded"
    ):
        return Outcome("failed", "rollout exceeded its progress deadline")
    healthy = (
        live.updated_replicas == live.replicas
        and live.ready_replicas == live.replicas
        and live.available_replicas == live.replicas
        and live.unavailable_replicas == 0
    )
    if healthy:
        return Outcome(
            "success",
            f"rollout complete: {live.available_replicas}/{live.replicas} replicas available",
        )
    return Outcome(
        "pending",
        f"rollout in progress: updated={live.updated_replicas} ready={live.ready_replicas} "
        f"available={live.available_replicas} desired={live.replicas}",
    )


def judge_pod_replacement(pods: list[PodInfo], old_uid: str, owner_uid: str | None) -> Outcome:
    if any(p.uid == old_uid for p in pods):
        return Outcome("pending", "original pod still exists (terminating)")
    replacements = [p for p in pods if owner_uid is not None and p.owner_uid == owner_uid]
    if not replacements:
        return Outcome("pending", "original pod is gone; waiting for the controller to recreate it")
    if any(p.phase == "Running" and p.ready for p in replacements):
        return Outcome("success", "a replacement pod is Running and Ready")
    return Outcome("pending", "replacement pod exists but is not Ready yet")


def check_once(
    gateway: KubeGateway,
    action_type: ActionType,
    namespace: str,
    name: str,
    uid: str,
    context: dict[str, object],
) -> Outcome:
    if action_type == ActionType.DEPLOYMENT_RESTART:
        live = gateway.get_deployment(namespace, name)
        return judge_deployment_rollout(live, uid, int(str(context.get("generation_before", 0))))
    pods = gateway.list_pods([namespace])
    owner = context.get("owner_uid")
    return judge_pod_replacement(pods, uid, str(owner) if owner else None)


def wait_for_outcome(
    check: Callable[[], Outcome],
    timeout_seconds: float,
    poll_seconds: float,
    *,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Outcome:
    start = monotonic()
    last = Outcome("pending", "no observation yet")
    while True:
        try:
            last = check()
        except KubernetesError as exc:  # transient API trouble: keep trying until the timeout
            last = Outcome("pending", f"could not observe cluster state: {exc.message}")
        if last.verdict != "pending":
            return last
        if monotonic() - start >= timeout_seconds:
            return Outcome(
                "failed", f"verification timed out after {int(timeout_seconds)}s: {last.detail}"
            )
        sleep(poll_seconds)
