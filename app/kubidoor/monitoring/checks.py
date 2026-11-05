"""Pure health checks: Kubernetes domain objects in, ``Finding`` objects out.

No I/O and no clock access (``now`` is passed in), so every rule is deterministic and unit-testable.
Thresholds come from ``MonitoringSettings`` so "not every Pending pod is unhealthy".
"""

from datetime import datetime

from kubidoor.core.config import MonitoringSettings
from kubidoor.core.types import ResourceType, Severity
from kubidoor.incidents.models import Finding
from kubidoor.kubernetes.models import (
    Condition,
    ContainerInfo,
    DeploymentInfo,
    EventInfo,
    NodeInfo,
    PodInfo,
    PVCInfo,
)

IMAGE_PULL_REASONS = frozenset({"ImagePullBackOff", "ErrImagePull"})
CREATE_ERROR_REASONS = frozenset(
    {"CreateContainerConfigError", "CreateContainerError", "InvalidImageName", "RunContainerError"}
)
NODE_PRESSURE = {
    "MemoryPressure": ("NodeMemoryPressure", Severity.HIGH),
    "DiskPressure": ("NodeDiskPressure", Severity.HIGH),
    "PIDPressure": ("NodePIDPressure", Severity.HIGH),
    "NetworkUnavailable": ("NodeNetworkUnavailable", Severity.HIGH),
}


def _age_seconds(since: datetime | None, now: datetime) -> float | None:
    return None if since is None else max((now - since).total_seconds(), 0.0)


def _container_facts(c: ContainerInfo) -> dict[str, object]:
    return {
        "container": c.name,
        "image": c.image,
        "state": c.state,
        "state_reason": c.state_reason,
        "restart_count": c.restart_count,
        "exit_code": c.exit_code,
        "last_termination_reason": c.last_reason,
        "last_exit_code": c.last_exit_code,
    }


# ----------------------------------------------------------------------------------- pods --
def check_pod(pod: PodInfo, now: datetime, cfg: MonitoringSettings) -> list[Finding]:
    if pod.phase == "Succeeded" or pod.deletion_timestamp is not None:
        return []  # completed Jobs and terminating pods are not problems

    base = {
        "namespace": pod.namespace,
        "pod_uid": pod.uid,
        "node_name": pod.node_name,
        "created_at": pod.created_at.isoformat() if pod.created_at else None,
    }

    def make(
        problem: str,
        severity: Severity,
        title: str,
        description: str,
        container: ContainerInfo | None = None,
        extra: dict[str, object] | None = None,
    ) -> Finding:
        evidence: dict[str, object] = {**base, "phase": pod.phase}
        if container is not None:
            evidence.update(_container_facts(container))
        if extra:
            evidence.update(extra)
        return Finding(
            resource_type=ResourceType.POD,
            namespace=pod.namespace,
            resource_name=pod.name,
            resource_uid=pod.uid,
            problem_type=problem,
            severity=severity,
            title=title,
            description=description,
            container=container.name if container else None,
            evidence=evidence,
            subject=pod,
        )

    findings: list[Finding] = []
    where = f"{pod.namespace}/{pod.name}"

    if pod.phase == "Failed":
        findings.append(
            make(
                "PodFailed",
                Severity.HIGH,
                f"Pod {where} failed",
                f"Pod is in phase Failed (reason: {pod.reason or 'unknown'}). {pod.message or ''}".strip(),
                extra={"reason": pod.reason, "message": pod.message},
            )
        )
    elif pod.phase == "Unknown":
        findings.append(
            make(
                "PodUnknown",
                Severity.HIGH,
                f"Pod {where} is in Unknown state",
                "The pod's state cannot be determined, usually because its node is unreachable.",
            )
        )

    explained = False  # whether container-level findings already explain a Pending/NotReady pod
    recent = cfg.recent_termination_seconds
    for c in pod.containers:
        last_age = _age_seconds(c.last_finished_at, now)
        last_recent = last_age is not None and last_age <= recent

        oom_now = c.state == "terminated" and c.state_reason == "OOMKilled"
        oom_recent = c.last_reason == "OOMKilled" and last_recent
        if oom_now or oom_recent:
            findings.append(
                make(
                    "OOMKilled",
                    Severity.HIGH,
                    f"Container {c.name} in {where} was OOMKilled",
                    f"Container {c.name} was terminated because it ran out of memory "
                    f"(restarts: {c.restart_count}).",
                    c,
                )
            )
            explained = True
            continue  # root cause already captured; don't add a duplicate CrashLoop incident

        if c.state == "waiting" and c.state_reason == "CrashLoopBackOff":
            findings.append(
                make(
                    "CrashLoopBackOff",
                    Severity.HIGH,
                    f"Container {c.name} in {where} is in CrashLoopBackOff",
                    f"Container {c.name} keeps crashing and is being restarted with back-off "
                    f"(restarts: {c.restart_count}, last exit code: {c.last_exit_code}).",
                    c,
                )
            )
            explained = True
        elif c.state == "waiting" and c.state_reason in IMAGE_PULL_REASONS:
            findings.append(
                make(
                    "ImagePullBackOff",
                    Severity.HIGH,
                    f"Container {c.name} in {where} cannot pull image",
                    f"Kubernetes cannot pull image '{c.image}' ({c.state_reason}).",
                    c,
                )
            )
            explained = True
        elif c.state == "waiting" and c.state_reason in CREATE_ERROR_REASONS:
            findings.append(
                make(
                    "ContainerCreateError",
                    Severity.MEDIUM,
                    f"Container {c.name} in {where} cannot be created ({c.state_reason})",
                    c.state_message or f"Container creation failed: {c.state_reason}.",
                    c,
                )
            )
            explained = True
        elif not c.is_init and c.restart_count >= cfg.restart_threshold and last_recent:
            findings.append(
                make(
                    "ExcessiveRestarts",
                    Severity.MEDIUM,
                    f"Container {c.name} in {where} restarted {c.restart_count} times",
                    f"Restart count {c.restart_count} reached the threshold of "
                    f"{cfg.restart_threshold} and the last termination was recent.",
                    c,
                )
            )
            explained = True

    if pod.phase == "Pending" and not explained:
        age = _age_seconds(pod.created_at, now)
        if age is not None and age >= cfg.pending_threshold_seconds:
            sched = pod.condition("PodScheduled")
            findings.append(
                make(
                    "PodPending",
                    Severity.MEDIUM,
                    f"Pod {where} has been Pending for {int(age)}s",
                    "Pod has not started within the configured threshold "
                    f"({cfg.pending_threshold_seconds}s).",
                    extra={
                        "pending_seconds": int(age),
                        "scheduled": sched.status if sched else None,
                        "scheduling_reason": sched.reason if sched else None,
                        "scheduling_message": sched.message if sched else None,
                    },
                )
            )
    elif pod.phase == "Running" and not explained:
        not_ready = [c for c in pod.containers if not c.is_init and not c.ready]
        ready_cond = pod.condition("Ready")
        if not_ready and ready_cond is not None and ready_cond.status != "True":
            since = _age_seconds(ready_cond.last_transition, now)
            if since is not None and since >= cfg.not_ready_threshold_seconds:
                names = ", ".join(c.name for c in not_ready)
                findings.append(
                    make(
                        "ContainersNotReady",
                        Severity.MEDIUM,
                        f"Pod {where} has containers not ready: {names}",
                        f"Containers {names} have been not ready for {int(since)}s.",
                        not_ready[0],
                        extra={"not_ready_containers": [c.name for c in not_ready]},
                    )
                )
    return findings


# ---------------------------------------------------------------------------------- nodes --
def check_node(node: NodeInfo, now: datetime) -> list[Finding]:
    findings: list[Finding] = []

    def make(
        problem: str, severity: Severity, title: str, desc: str, cond: Condition | None
    ) -> Finding:
        return Finding(
            resource_type=ResourceType.NODE,
            namespace=None,
            resource_name=node.name,
            resource_uid=node.uid,
            problem_type=problem,
            severity=severity,
            title=title,
            description=desc,
            evidence={
                "condition": cond.type if cond else None,
                "status": cond.status if cond else None,
                "reason": cond.reason if cond else None,
                "message": cond.message if cond else None,
                "last_transition": cond.last_transition.isoformat()
                if cond and cond.last_transition
                else None,
                "capacity": dict(node.capacity),
                "allocatable": dict(node.allocatable),
            },
            subject=node,
        )

    ready = node.condition("Ready")
    if ready is None or ready.status != "True":
        status = ready.status if ready else "Missing"
        findings.append(
            make(
                "NodeNotReady",
                Severity.CRITICAL,
                f"Node {node.name} is NotReady",
                f"Node Ready condition is '{status}'"
                + (f": {ready.message}" if ready and ready.message else "."),
                ready,
            )
        )
    for cond_type, (problem, severity) in NODE_PRESSURE.items():
        cond = node.condition(cond_type)
        if cond is not None and cond.status == "True":
            findings.append(
                make(
                    problem,
                    severity,
                    f"Node {node.name} reports {cond_type}",
                    cond.message or f"Node condition {cond_type} is True.",
                    cond,
                )
            )
    return findings


# ----------------------------------------------------------------------------- deployments --
def check_deployment(dep: DeploymentInfo, now: datetime, cfg: MonitoringSettings) -> list[Finding]:
    if dep.paused:
        return []  # a paused rollout is intentional; nothing progresses by design
    where = f"{dep.namespace}/{dep.name}"
    base: dict[str, object] = {
        "desired_replicas": dep.replicas,
        "updated_replicas": dep.updated_replicas,
        "ready_replicas": dep.ready_replicas,
        "available_replicas": dep.available_replicas,
        "unavailable_replicas": dep.unavailable_replicas,
        "generation": dep.generation,
        "observed_generation": dep.observed_generation,
        "conditions": [
            {"type": c.type, "status": c.status, "reason": c.reason, "message": c.message}
            for c in dep.conditions
        ],
    }

    def make(problem: str, severity: Severity, title: str, desc: str) -> Finding:
        return Finding(
            resource_type=ResourceType.DEPLOYMENT,
            namespace=dep.namespace,
            resource_name=dep.name,
            resource_uid=dep.uid,
            problem_type=problem,
            severity=severity,
            title=title,
            description=desc,
            evidence=dict(base),
            subject=dep,
        )

    progressing = dep.condition("Progressing")
    findings: list[Finding] = []

    if (
        progressing
        and progressing.status == "False"
        and progressing.reason == "ProgressDeadlineExceeded"
    ):
        findings.append(
            make(
                "ProgressDeadlineExceeded",
                Severity.HIGH,
                f"Deployment {where} rollout failed (ProgressDeadlineExceeded)",
                progressing.message
                or "The rollout did not progress within progressDeadlineSeconds.",
            )
        )
        return findings  # replica shortfall is the same underlying problem

    replica_failure = dep.condition("ReplicaFailure")
    if replica_failure and replica_failure.status == "True":
        findings.append(
            make(
                "ReplicaFailure",
                Severity.HIGH,
                f"Deployment {where} cannot create replicas",
                replica_failure.message or "The ReplicaSet reports a ReplicaFailure condition.",
            )
        )

    if dep.replicas > 0 and dep.available_replicas < dep.replicas:
        # Rolling out/scaling is normal: only flag once there has been no progress for the grace period.
        marks = [
            t
            for t in (
                progressing.last_update if progressing else None,
                progressing.last_transition if progressing else None,
                (dep.condition("Available") or Condition("", "")).last_transition,
            )
            if t is not None
        ]
        reference = max(marks) if marks else dep.created_at
        stalled_for = _age_seconds(reference, now)
        if stalled_for is not None and stalled_for >= cfg.deployment_grace_seconds:
            unavailable = dep.replicas - dep.available_replicas
            findings.append(
                make(
                    "ReplicasUnavailable",
                    Severity.HIGH if dep.available_replicas == 0 else Severity.MEDIUM,
                    f"Deployment {where} has {dep.available_replicas}/{dep.replicas} replicas available",
                    f"{unavailable} replica(s) unavailable with no progress for {int(stalled_for)}s "
                    f"(grace period {cfg.deployment_grace_seconds}s).",
                )
            )
    return findings


# ---------------------------------------------------------------------------------- volumes --
def check_pvc(
    pvc: PVCInfo, now: datetime, cfg: MonitoringSettings, events: list[EventInfo]
) -> list[Finding]:
    where = f"{pvc.namespace}/{pvc.name}"
    evidence: dict[str, object] = {
        "phase": pvc.phase,
        "storage_class": pvc.storage_class,
        "volume_name": pvc.volume_name,
        "requested_storage": pvc.requested_storage,
    }

    def make(problem: str, severity: Severity, title: str, desc: str) -> Finding:
        return Finding(
            resource_type=ResourceType.PVC,
            namespace=pvc.namespace,
            resource_name=pvc.name,
            resource_uid=pvc.uid,
            problem_type=problem,
            severity=severity,
            title=title,
            description=desc,
            evidence=dict(evidence),
            subject=pvc,
        )

    if pvc.phase == "Lost":
        return [
            make(
                "PVCLost",
                Severity.HIGH,
                f"PVC {where} is Lost",
                "The bound PersistentVolume no longer exists.",
            )
        ]
    if pvc.phase == "Pending":
        mine = [e for e in events if e.kind == "PersistentVolumeClaim" and e.name == pvc.name]
        waiting_for_consumer = any(e.reason == "WaitForFirstConsumer" for e in mine) and not any(
            e.reason in ("ProvisioningFailed", "FailedBinding") for e in mine
        )
        if waiting_for_consumer:
            return []  # WaitForFirstConsumer binding mode: Pending is expected until a pod uses it
        age = _age_seconds(pvc.created_at, now)
        if age is not None and age >= cfg.pvc_pending_threshold_seconds:
            return [
                make(
                    "PVCPending",
                    Severity.MEDIUM,
                    f"PVC {where} is unbound after {int(age)}s",
                    "The claim has not been bound to a PersistentVolume within the threshold.",
                )
            ]
    return []
