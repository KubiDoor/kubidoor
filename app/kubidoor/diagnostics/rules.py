"""Deterministic, rule-based root-cause rules.

Every rule returns a ``Diagnosis`` that keeps four things apart:
  * observed_facts      - things read from the cluster (state, events, spec values)
  * probable_cause      - an interpretation, worded as probable, never as confirmed
  * confidence          - high / medium / low, driven by how direct the evidence is
  * recommended_actions - investigation steps (never automatic changes)

No external services, no LLMs. Secret *values* are never read; only referenced Secret names
(``imagePullSecrets``) are mentioned.
"""

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from kubidoor.incidents.models import Finding
from kubidoor.kubernetes.metrics import parse_memory_bytes
from kubidoor.kubernetes.models import (
    ContainerInfo,
    DeploymentInfo,
    EventInfo,
    NodeInfo,
    PodInfo,
    PVCInfo,
    ResourceUsage,
)

Confidence = Literal["high", "medium", "low"]


CONFIDENCE_LEVELS = ("high", "medium", "low")


@dataclass(frozen=True, slots=True)
class Diagnosis:
    observed_facts: list[str]
    probable_cause: str
    confidence: str  # one of CONFIDENCE_LEVELS (enforced below)
    recommended_actions: list[str]

    def __post_init__(self) -> None:
        if self.confidence not in CONFIDENCE_LEVELS:
            raise ValueError(f"invalid confidence level: {self.confidence!r}")


@dataclass(slots=True)
class DiagnosticContext:
    now: datetime
    events: list[EventInfo] = field(default_factory=list)  # already filtered to this resource
    nodes: list[NodeInfo] | None = None
    pods: list[PodInfo] | None = None
    pod_usage: dict[str, ResourceUsage] | None = None  # container name -> usage
    usage_note: str = "Resource metrics were not requested"


def _event_facts(events: list[EventInfo], limit: int = 5) -> list[str]:
    return [f"Event {e.type}/{e.reason} (x{e.count}): {e.message[:240]}" for e in events[:limit]]


def _event_text(events: list[EventInfo]) -> str:
    return " ".join(e.message.lower() for e in events if e.type == "Warning")


def _container(pod: PodInfo, finding: Finding) -> ContainerInfo | None:
    return next((c for c in pod.containers if c.name == finding.container), None)


def _kubectl_logs(pod: PodInfo, c: ContainerInfo | None) -> str:
    cname = f" -c {c.name}" if c else ""
    return f"kubectl logs -n {pod.namespace} {pod.name}{cname} --previous"


# ------------------------------------------------------------------------------ Pod rules --
_EXIT_CODE_HINTS: dict[int, tuple[str, Confidence]] = {
    0: (
        "the process exited successfully (code 0) but the pod's restart policy keeps restarting "
        "it; a long-running service should not exit",
        "medium",
    ),
    1: (
        "the application exited with a general error (code 1), typically an unhandled "
        "startup/runtime failure",
        "medium",
    ),
    2: ("the process exited with code 2, commonly invalid command-line arguments", "medium"),
    126: (
        "the entrypoint was found but could not be executed (code 126, e.g. permissions)",
        "medium",
    ),
    127: ("the entrypoint/command was not found in the image (code 127)", "medium"),
    137: (
        "the process was killed with SIGKILL (code 137): out-of-memory or an external kill",
        "medium",
    ),
    139: ("the process crashed with a segmentation fault (code 139)", "medium"),
    143: (
        "the process was terminated by SIGTERM (code 143), e.g. by a failing liveness probe",
        "low",
    ),
}


def diagnose_crashloop(finding: Finding, pod: PodInfo, ctx: DiagnosticContext) -> Diagnosis:
    c = _container(pod, finding)
    facts = [f"Pod phase is {pod.phase}."]
    cause = "The reason the container keeps exiting cannot be determined from cluster state alone."
    confidence: Confidence = "low"
    if c is not None:
        facts.append(
            f"Container '{c.name}' is {c.state}"
            + (f" ({c.state_reason})" if c.state_reason else "")
            + f" with {c.restart_count} restart(s)."
        )
        if c.last_reason or c.last_exit_code is not None:
            facts.append(
                f"Previous termination: reason={c.last_reason or 'unknown'}, "
                f"exit code={c.last_exit_code}."
            )
        if c.last_exit_code in _EXIT_CODE_HINTS:
            hint, confidence = _EXIT_CODE_HINTS[c.last_exit_code]
            cause = f"Probably {hint}."
    liveness = [e for e in ctx.events if "liveness probe failed" in e.message.lower()]
    if liveness:
        facts.append(f"Liveness probe failures recorded in events ({len(liveness)}).")
        cause = (
            "Probably the liveness probe is failing, so the kubelet kills and restarts "
            "the container."
        )
        confidence = "high"
    facts.extend(_event_facts(ctx.events))
    return Diagnosis(
        facts,
        cause,
        confidence,
        [
            f"Inspect the previous container's logs: {_kubectl_logs(pod, c)}",
            "Check application startup configuration (environment, ConfigMaps, mounted files).",
            "Check that dependencies (databases, APIs, DNS) are reachable from the pod.",
            "Verify the container entrypoint and command/args in the pod spec.",
            "Investigate the previous termination reason and exit code listed above.",
        ],
    )


def diagnose_oom(finding: Finding, pod: PodInfo, ctx: DiagnosticContext) -> Diagnosis:
    c = _container(pod, finding)
    facts = ["The container was terminated with reason OOMKilled."]
    limit_text = c.limits.get("memory") if c else None
    request_text = c.requests.get("memory") if c else None
    if c is not None:
        facts.append(
            f"Exit code: {c.exit_code if c.exit_code is not None else c.last_exit_code}; "
            f"restarts: {c.restart_count}."
        )
    facts.append(
        f"Configured memory limit: {limit_text or 'none'}; request: {request_text or 'none'}."
    )
    usage = (ctx.pod_usage or {}).get(c.name) if c else None
    if usage is not None:
        facts.append(f"Current memory usage (metrics-server): {usage.memory_bytes // 2**20} MiB.")
        if limit_text and usage.memory_bytes >= 0.9 * parse_memory_bytes(limit_text):
            facts.append("Current usage is at least 90% of the memory limit.")
    else:
        facts.append(f"Usage metrics unavailable: {ctx.usage_note}.")
    facts.extend(_event_facts(ctx.events, 3))

    if limit_text:
        cause = (
            f"The container probably exceeded its memory limit of {limit_text}; the limit may be "
            "too low for the workload or the application may be leaking memory."
        )
        confidence: Confidence = "high"
    else:
        cause = (
            "The container was OOM-killed although it has no memory limit; the node probably "
            "ran out of memory (check node MemoryPressure)."
        )
        confidence = "medium"
    return Diagnosis(
        facts,
        cause,
        confidence,
        [
            "Review the container's real memory consumption over time (metrics/Prometheus).",
            "Review the container's memory requests and limits against that consumption "
            "(KubiDoor never changes limits automatically).",
            "Look for memory leaks or unbounded caches/queries in the application.",
            "Check whether the node reports MemoryPressure.",
        ],
    )


def _image_has_tag(image: str) -> bool:
    last = image.rsplit("/", 1)[-1]
    return ":" in last or "@" in image


def diagnose_image_pull(finding: Finding, pod: PodInfo, ctx: DiagnosticContext) -> Diagnosis:
    c = _container(pod, finding)
    image = c.image if c else "unknown"
    facts = [f"Image reference: {image}."]
    if not _image_has_tag(image):
        facts.append("The image has no explicit tag (defaults to :latest).")
    if c and c.state_reason:
        facts.append(f"Container waiting reason: {c.state_reason}.")
    secrets = pod.image_pull_secrets
    facts.append(
        f"imagePullSecrets referenced: {', '.join(secrets)} (names only; values are never read)."
        if secrets
        else "No imagePullSecrets are referenced by the pod."
    )
    pull_events = [
        e for e in ctx.events if e.reason in ("Failed", "BackOff", "Pulling", "ErrImagePull")
    ]
    facts.extend(_event_facts(pull_events or ctx.events, 4))
    text = (
        _event_text(ctx.events) + " " + (c.state_message or "").lower()
        if c
        else _event_text(ctx.events)
    )

    if c and c.state_reason == "InvalidImageName":
        cause, confidence = "The image reference is malformed.", "high"
    elif any(k in text for k in ("manifest unknown", "not found", "name unknown")):
        cause = "The image name or tag probably does not exist in the registry."
        confidence = "high"
    elif any(k in text for k in ("toomanyrequests", "rate limit")):
        cause, confidence = "The registry is probably rate-limiting image pulls.", "high"
    elif any(
        k in text
        for k in ("unauthorized", "authentication required", "pull access denied", "denied")
    ):
        cause = (
            "The registry probably rejected the request: missing/invalid pull credentials, or "
            "the repository is private or does not exist."
        )
        confidence = "medium"
    elif any(
        k in text for k in ("no such host", "i/o timeout", "connection refused", "x509", "tls")
    ):
        cause = "The registry is probably unreachable from the node (DNS, network, or TLS problem)."
        confidence = "medium"
    else:
        cause = "The image pull is failing, but no event detail identifies why."
        confidence = "low"
    return Diagnosis(
        facts,
        cause,
        confidence,
        [
            "Check the image name for typos.",
            "Check that the image tag exists in the registry.",
            "Check registry availability and network/DNS access from the node.",
            "Check registry authentication: the referenced imagePullSecrets (exists, correct "
            "registry, not expired) and the service account's pull secrets.",
        ],
    )


def diagnose_pending(finding: Finding, pod: PodInfo, ctx: DiagnosticContext) -> Diagnosis:
    sched = pod.condition("PodScheduled")
    facts = [f"Pod phase is {pod.phase}."]
    sched_events = [e for e in ctx.events if e.reason == "FailedScheduling"]
    message = (sched.message if sched and sched.message else "") or (
        sched_events[0].message if sched_events else ""
    )
    if sched:
        facts.append(
            f"PodScheduled={sched.status}" + (f" ({sched.reason})" if sched.reason else "") + "."
        )
    if message:
        facts.append(f"Scheduler message: {message[:300]}")
    requests = {c.name: dict(c.requests) for c in pod.containers if c.requests and not c.is_init}
    facts.append(f"Resource requests: {requests or 'none specified'}.")
    facts.append(
        f"Node selector: {dict(pod.node_selector) or 'none'}; tolerations: {len(pod.tolerations)}."
    )
    if ctx.nodes is not None:
        ready = sum(1 for n in ctx.nodes if n.ready_status == "True")
        facts.append(f"Nodes: {len(ctx.nodes)} total, {ready} Ready.")
    facts.extend(_event_facts(ctx.events, 4))

    low = message.lower()
    unscheduled = sched is not None and sched.status == "False"
    if pod.node_name is None and (unscheduled or message):
        if "insufficient" in low:
            cause, confidence = (
                "The scheduler reports insufficient node resources for the pod's requests.",
                "high",
            )
        elif "untolerated taint" in low or "had taint" in low:
            cause, confidence = (
                "Available nodes carry taints the pod does not tolerate.",
                "high",
            )
        elif "node affinity" in low or "node selector" in low or "nodeselector" in low:
            cause, confidence = (
                "No node matches the pod's node selector or affinity rules.",
                "high",
            )
        elif "persistentvolumeclaim" in low or "unbound immediate" in low:
            cause, confidence = (
                "The pod is waiting for PersistentVolumeClaim(s) that are not bound.",
                "high",
            )
        elif "no nodes available" in low or "0/0 nodes" in low:
            cause, confidence = "There are no schedulable nodes in the cluster.", "high"
        elif message:
            cause, confidence = (
                "The scheduler could not place the pod; see the scheduler message above.",
                "medium",
            )
        else:
            cause, confidence = (
                "The pod is marked unschedulable but the scheduler recorded no reason.",
                "low",
            )
    elif pod.node_name is not None:
        mount = [e for e in ctx.events if e.reason in ("FailedMount", "FailedAttachVolume")]
        sandbox = [
            e for e in ctx.events if e.reason in ("FailedCreatePodSandBox", "NetworkNotReady")
        ]
        if mount:
            cause, confidence = "A volume could not be mounted or attached.", "high"
        elif sandbox:
            cause, confidence = (
                "The pod sandbox/network could not be created (CNI or runtime problem).",
                "high",
            )
        else:
            cause, confidence = (
                f"The pod was scheduled to {pod.node_name} but its containers have not started; "
                "no event explains why.",
                "low",
            )
    else:
        cause, confidence = (
            "No scheduling failure has been recorded yet; the scheduler may not have processed the pod.",
            "low",
        )
    return Diagnosis(
        facts,
        cause,
        confidence,
        [
            f"Run: kubectl describe pod -n {pod.namespace} {pod.name} and read the Events section.",
            "Compare the pod's resource requests with node allocatable resources.",
            "Check node taints and the pod's tolerations, node selector and affinity.",
            "Check that referenced PVCs exist and are bound.",
        ],
    )


def diagnose_pod_failed(finding: Finding, pod: PodInfo, ctx: DiagnosticContext) -> Diagnosis:
    facts = [
        f"Pod phase is Failed; reason={pod.reason or 'none'}.",
        *([pod.message[:300]] if pod.message else []),
    ]
    facts.extend(_event_facts(ctx.events, 4))
    if pod.reason == "Evicted":
        cause, confidence = "The pod was evicted, usually due to node resource pressure.", "high"
    else:
        cause, confidence = "The pod failed; the pod status gives no further explanation.", "low"
    return Diagnosis(
        facts,
        cause,
        confidence,
        [
            "Check events and the node's conditions (MemoryPressure/DiskPressure).",
            f"Inspect logs: {_kubectl_logs(pod, None)}",
            "Delete the failed pod once understood so its controller can replace it.",
        ],
    )


def diagnose_pod_unknown(finding: Finding, pod: PodInfo, ctx: DiagnosticContext) -> Diagnosis:
    facts = [f"Pod phase is Unknown; assigned node: {pod.node_name or 'none'}."]
    return Diagnosis(
        facts,
        "The control plane lost contact with the node running this pod.",
        "medium",
        [
            "Check the status of the pod's node (kubectl get node).",
            "Investigate node/kubelet connectivity.",
        ],
    )


def diagnose_not_ready(finding: Finding, pod: PodInfo, ctx: DiagnosticContext) -> Diagnosis:
    c = _container(pod, finding)
    facts = [
        f"Pod phase Running but container(s) not ready: {finding.evidence.get('not_ready_containers')}."
    ]
    facts.extend(_event_facts(ctx.events, 4))
    probe = [e for e in ctx.events if "readiness probe failed" in e.message.lower()]
    if probe:
        cause, confidence = "The readiness probe is failing.", "high"
    else:
        cause, confidence = (
            "The container is running but not reporting ready; the reason is not recorded in events.",
            "low",
        )
    return Diagnosis(
        facts,
        cause,
        confidence,
        [
            "Check the readiness probe configuration (path, port, timeouts).",
            f"Inspect logs: kubectl logs -n {pod.namespace} {pod.name}"
            + (f" -c {c.name}" if c else ""),
            "Check that the application's dependencies are available.",
        ],
    )


def diagnose_create_error(finding: Finding, pod: PodInfo, ctx: DiagnosticContext) -> Diagnosis:
    c = _container(pod, finding)
    msg = (c.state_message if c else None) or ""
    facts = [
        f"Container waiting reason: {c.state_reason if c else 'unknown'}.",
        *([msg[:300]] if msg else []),
    ]
    facts.extend(_event_facts(ctx.events, 3))
    if c and c.state_reason == "CreateContainerConfigError" and "not found" in msg.lower():
        cause, confidence = "A ConfigMap or Secret referenced by the pod does not exist.", "high"
    else:
        cause, confidence = "The container could not be created; see the message above.", "medium"
    return Diagnosis(
        facts,
        cause,
        confidence,
        [
            "Verify all ConfigMaps/Secrets referenced by the pod exist in its namespace.",
            "Check image name validity and the container command/securityContext.",
        ],
    )


# ------------------------------------------------------------------------------ Node rules --
def diagnose_node_not_ready(finding: Finding, node: NodeInfo, ctx: DiagnosticContext) -> Diagnosis:
    ready = node.condition("Ready")
    facts: list[str] = []
    if ready is None:
        facts.append("The node has no Ready condition.")
    else:
        since = (ctx.now - ready.last_transition).total_seconds() if ready.last_transition else None
        facts.append(
            f"Ready={ready.status} (reason: {ready.reason or 'none'})"
            + (f", last transition {int(since)}s ago." if since is not None else ".")
        )
        if ready.message:
            facts.append(f"Condition message: {ready.message[:300]}")
    pressure = [c.type for c in node.conditions if c.type != "Ready" and c.status == "True"]
    if pressure:
        facts.append(f"Other conditions that are True: {', '.join(pressure)}.")
    facts.extend(_event_facts(ctx.events, 4))
    if ready is not None and ready.status == "Unknown":
        cause, confidence = (
            "The kubelet stopped reporting status: the node, kubelet, or its network path to the "
            "API server is probably down.",
            "medium",
        )
    elif ready is not None and ready.status == "False":
        cause, confidence = (
            f"The kubelet itself reports the node is not ready ({ready.reason or 'no reason given'}).",
            "high",
        )
    else:
        cause, confidence = "The node's readiness cannot be determined.", "low"
    return Diagnosis(
        facts,
        cause,
        confidence,
        [
            "Check that the node host is running and reachable (cloud console / hypervisor / SSH).",
            "Check the kubelet service and logs (systemctl status kubelet; journalctl -u kubelet).",
            "Check the container runtime and node network connectivity to the API server.",
            "Check node resource exhaustion (disk, memory, PIDs).",
        ],
    )


_PRESSURE_ACTIONS = {
    "NodeMemoryPressure": "Identify high-memory pods on the node; consider rescheduling workloads or adding memory.",
    "NodeDiskPressure": "Free disk space (images, logs, ephemeral storage) or expand the node's disk.",
    "NodePIDPressure": "Look for processes/pods spawning excessive PIDs; check pod PID limits.",
    "NodeNetworkUnavailable": "Check the CNI plugin and node network configuration.",
}


def diagnose_node_pressure(finding: Finding, node: NodeInfo, ctx: DiagnosticContext) -> Diagnosis:
    cond_type = str(finding.evidence.get("condition"))
    cond = node.condition(cond_type)
    facts = [
        f"Condition {cond_type}=True"
        + (f": {cond.message[:300]}" if cond and cond.message else ".")
    ]
    facts.extend(_event_facts(ctx.events, 3))
    return Diagnosis(
        facts,
        f"The kubelet reports {cond_type}; workloads on this node may be evicted or fail.",
        "high",
        [
            _PRESSURE_ACTIONS.get(finding.problem_type, "Investigate the node."),
            "Check events and running pods on the node (kubectl describe node).",
        ],
    )


# ------------------------------------------------------------------------ Deployment rules --
def _pod_problem(pod: PodInfo) -> str | None:
    for c in pod.containers:
        if c.state == "waiting" and c.state_reason and c.state_reason != "ContainerCreating":
            return c.state_reason
        if c.last_reason == "OOMKilled" and c.restart_count > 0 and not c.ready:
            return "OOMKilled"
    if pod.phase in ("Pending", "Failed", "Unknown"):
        return pod.phase
    return None


def _owned_pods(dep: DeploymentInfo, ctx: DiagnosticContext) -> list[PodInfo] | None:
    if ctx.pods is None or not dep.selector:
        return None
    return [
        p
        for p in ctx.pods
        if p.namespace == dep.namespace
        and all(p.labels.get(k) == v for k, v in dep.selector.items())
    ]


def diagnose_deployment(finding: Finding, dep: DeploymentInfo, ctx: DiagnosticContext) -> Diagnosis:
    facts = [
        f"Replicas: desired={dep.replicas}, updated={dep.updated_replicas}, ready={dep.ready_replicas}, "
        f"available={dep.available_replicas}, unavailable={dep.unavailable_replicas}."
    ]
    for cond in dep.conditions:
        if cond.status == "False" or cond.type == "ReplicaFailure":
            facts.append(
                f"Condition {cond.type}={cond.status} ({cond.reason}): {(cond.message or '')[:240]}"
            )
    pods = _owned_pods(dep, ctx)
    problems: Counter[str] = Counter()
    if pods is None:
        facts.append("Pod details are unavailable for correlation.")
    else:
        for p in pods:
            if (problem := _pod_problem(p)) is not None:
                problems[problem] += 1
        facts.append(
            f"{len(pods)} pod(s) match the selector; "
            + (
                ", ".join(f"{n}x {k}" for k, n in problems.items())
                if problems
                else "none show a failure state"
            )
            + "."
        )
    facts.extend(_event_facts(ctx.events, 3))

    replica_failure = dep.condition("ReplicaFailure")
    if replica_failure and replica_failure.status == "True":
        cause, confidence = (
            "The ReplicaSet cannot create pods (often quota, LimitRange or admission rejection): "
            f"{(replica_failure.message or '')[:200]}",
            "high",
        )
    elif problems:
        top, _ = problems.most_common(1)[0]
        cause = (
            f"The Deployment's pods are failing ({top} is the most common state); "
            "the underlying pod incidents are the likely root cause."
        )
        confidence = "medium"
    elif pods is not None and not pods:
        cause, confidence = "No pods currently match the Deployment's selector.", "medium"
    else:
        cause, confidence = (
            "Replicas are unavailable but no failing pods were observed; the rollout may simply be slow.",
            "low",
        )
    return Diagnosis(
        facts,
        cause,
        confidence,
        [
            f"Check rollout status: kubectl rollout status deployment/{dep.name} -n {dep.namespace}",
            f"Inspect the pods' events and logs: kubectl describe pods -n {dep.namespace} -l <selector>",
            "A rollout restart does not fix image, configuration or quota errors; fix the cause first.",
            "Review recent changes to the Deployment (image, env, resources, probes).",
        ],
    )


# ---------------------------------------------------------------------------------- PVC --
def diagnose_pvc(finding: Finding, pvc: PVCInfo, ctx: DiagnosticContext) -> Diagnosis:
    facts = [
        f"PVC phase is {pvc.phase}; storage class: {pvc.storage_class or 'none set'}; "
        f"requested: {pvc.requested_storage or 'unknown'}; volume: {pvc.volume_name or 'none'}."
    ]
    facts.extend(_event_facts(ctx.events, 4))
    failed = [e for e in ctx.events if e.reason in ("ProvisioningFailed", "FailedBinding")]
    if pvc.phase == "Lost":
        cause, confidence = "The PersistentVolume this claim was bound to no longer exists.", "high"
    elif failed:
        cause, confidence = f"Provisioning/binding failed: {failed[0].message[:200]}", "high"
    elif pvc.storage_class is None:
        cause, confidence = (
            "No storageClassName is set; binding depends on a default StorageClass or a matching "
            "pre-created PersistentVolume, and none was found.",
            "low",
        )
    else:
        cause, confidence = "The claim is not bound and no provisioning error is recorded.", "low"
    return Diagnosis(
        facts,
        cause,
        confidence,
        [
            "Check that the StorageClass exists and its provisioner is running.",
            "Check events on the PVC (kubectl describe pvc).",
            "Verify requested size/access mode against available PersistentVolumes.",
            "KubiDoor never modifies or deletes PVCs/PVs; handle storage changes manually.",
        ],
    )


# --------------------------------------------------------------------------- registry --
Rule = Callable[[Finding, Any, DiagnosticContext], Diagnosis]

RULES: dict[str, Rule] = {
    "CrashLoopBackOff": diagnose_crashloop,
    "ExcessiveRestarts": diagnose_crashloop,
    "OOMKilled": diagnose_oom,
    "ImagePullBackOff": diagnose_image_pull,
    "PodPending": diagnose_pending,
    "PodFailed": diagnose_pod_failed,
    "PodUnknown": diagnose_pod_unknown,
    "ContainersNotReady": diagnose_not_ready,
    "ContainerCreateError": diagnose_create_error,
    "NodeNotReady": diagnose_node_not_ready,
    "NodeMemoryPressure": diagnose_node_pressure,
    "NodeDiskPressure": diagnose_node_pressure,
    "NodePIDPressure": diagnose_node_pressure,
    "NodeNetworkUnavailable": diagnose_node_pressure,
    "ProgressDeadlineExceeded": diagnose_deployment,
    "ReplicasUnavailable": diagnose_deployment,
    "ReplicaFailure": diagnose_deployment,
    "PVCPending": diagnose_pvc,
    "PVCLost": diagnose_pvc,
}
