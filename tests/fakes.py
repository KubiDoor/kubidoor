"""Test doubles: object builders, a controllable clock and an in-memory Kubernetes gateway."""

import threading
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from kubidoor.core.exceptions import (
    KubernetesError,
    KubernetesUnavailableError,
    MetricsUnavailableError,
)
from kubidoor.kubernetes.models import (
    Condition,
    ContainerInfo,
    DeploymentInfo,
    EventInfo,
    NodeInfo,
    PodInfo,
    PVCInfo,
    ResourceUsage,
)

T0 = datetime(2026, 11, 3, 12, 0, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


def container(
    name: str = "app",
    *,
    state: str = "running",
    reason: str | None = None,
    ready: bool = True,
    restarts: int = 0,
    exit_code: int | None = None,
    last_reason: str | None = None,
    last_exit_code: int | None = None,
    last_finished_at: datetime | None = None,
    image: str = "nginx:1.27",
    requests: dict[str, str] | None = None,
    limits: dict[str, str] | None = None,
    message: str | None = None,
) -> ContainerInfo:
    return ContainerInfo(
        name=name,
        image=image,
        ready=ready,
        restart_count=restarts,
        state=state,
        state_reason=reason,
        state_message=message,
        exit_code=exit_code,
        last_reason=last_reason,
        last_exit_code=last_exit_code,
        last_finished_at=last_finished_at,
        requests=requests or {},
        limits=limits or {},
    )


def pod(
    name: str = "web-abc",
    namespace: str = "demo",
    *,
    uid: str | None = None,
    phase: str = "Running",
    containers: Sequence[ContainerInfo] | None = None,
    ready: bool | None = None,
    created_at: datetime | None = None,
    node: str | None = "node-1",
    owner_kind: str | None = "ReplicaSet",
    owner_uid: str | None = "rs-1",
    labels: dict[str, str] | None = None,
    conditions: Sequence[Condition] | None = None,
    deletion: datetime | None = None,
    image_pull_secrets: tuple[str, ...] = (),
    node_selector: dict[str, str] | None = None,
) -> PodInfo:
    cs = tuple(containers) if containers is not None else (container(),)
    if conditions is None:
        is_ready = all(c.ready for c in cs) if ready is None else ready
        conditions = (
            Condition("Ready", "True" if is_ready else "False", last_transition=T0),
            Condition("PodScheduled", "True" if node else "False", last_transition=T0),
        )
    return PodInfo(
        namespace=namespace,
        name=name,
        uid=uid or f"uid-{namespace}-{name}",
        phase=phase,
        node_name=node,
        created_at=created_at or T0 - timedelta(hours=1),
        labels=labels or {"app": "web"},
        owner_kind=owner_kind,
        owner_name="web-rs" if owner_kind else None,
        owner_uid=owner_uid if owner_kind else None,
        containers=cs,
        conditions=tuple(conditions),
        deletion_timestamp=deletion,
        image_pull_secrets=image_pull_secrets,
        node_selector=node_selector or {},
    )


def crashloop_pod(name: str = "crash-1", namespace: str = "demo", restarts: int = 4) -> PodInfo:
    return pod(
        name,
        namespace,
        containers=[
            container(
                state="waiting",
                reason="CrashLoopBackOff",
                ready=False,
                restarts=restarts,
                last_reason="Error",
                last_exit_code=1,
                last_finished_at=T0 - timedelta(seconds=30),
            )
        ],
    )


def node(
    name: str = "node-1",
    *,
    ready: str = "True",
    pressure: Sequence[str] = (),
    uid: str | None = None,
) -> NodeInfo:
    conds = [Condition("Ready", ready, "KubeletReady", "kubelet", T0 - timedelta(minutes=10))]
    conds += [Condition(p, "True", p, f"{p} detected", T0) for p in pressure]
    return NodeInfo(
        name=name,
        uid=uid or f"uid-node-{name}",
        created_at=T0 - timedelta(days=3),
        conditions=tuple(conds),
        capacity={"cpu": "4", "memory": "8Gi"},
        allocatable={"cpu": "3900m", "memory": "7Gi"},
    )


def deployment(
    name: str = "web",
    namespace: str = "demo",
    *,
    replicas: int = 3,
    ready: int | None = None,
    available: int | None = None,
    updated: int | None = None,
    uid: str | None = None,
    generation: int = 1,
    observed: int | None = None,
    conditions: Sequence[Condition] | None = None,
    paused: bool = False,
    age: timedelta = timedelta(hours=1),
    selector: dict[str, str] | None = None,
) -> DeploymentInfo:
    ready = replicas if ready is None else ready
    available = ready if available is None else available
    updated = replicas if updated is None else updated
    if conditions is None:
        conditions = (
            Condition("Available", "True" if available else "False", last_transition=T0 - age),
            Condition("Progressing", "True", "NewReplicaSetAvailable", None, T0 - age, T0 - age),
        )
    return DeploymentInfo(
        namespace=namespace,
        name=name,
        uid=uid or f"uid-dep-{namespace}-{name}",
        created_at=T0 - age,
        generation=generation,
        observed_generation=generation if observed is None else observed,
        replicas=replicas,
        updated_replicas=updated,
        ready_replicas=ready,
        available_replicas=available,
        unavailable_replicas=max(replicas - available, 0),
        paused=paused,
        conditions=tuple(conditions),
        selector=selector or {"app": "web"},
    )


def pvc(
    name: str = "data",
    namespace: str = "demo",
    *,
    phase: str = "Bound",
    age: timedelta = timedelta(hours=1),
    storage_class: str | None = "standard",
) -> PVCInfo:
    return PVCInfo(
        namespace=namespace,
        name=name,
        uid=f"uid-pvc-{namespace}-{name}",
        phase=phase,
        created_at=T0 - age,
        storage_class=storage_class,
        requested_storage="1Gi",
    )


def event(
    kind: str,
    name: str,
    reason: str,
    message: str,
    *,
    namespace: str | None = "demo",
    type_: str = "Warning",
    count: int = 1,
) -> EventInfo:
    return EventInfo(
        namespace=namespace,
        kind=kind,
        name=name,
        uid=None,
        type=type_,
        reason=reason,
        message=message,
        count=count,
        first_at=T0 - timedelta(minutes=5),
        last_at=T0 - timedelta(minutes=1),
    )


class FakeGateway:
    """In-memory KubeGateway. Any list/get can be made to fail with ``fail``."""

    def __init__(self) -> None:
        self.nodes: list[NodeInfo] = []
        self.pods: list[PodInfo] = []
        self.deployments: list[DeploymentInfo] = []
        self.pvcs: list[PVCInfo] = []
        self.events: list[EventInfo] = []
        self.node_metrics: dict[str, ResourceUsage] | None = None
        self.pod_metrics: dict[tuple[str, str], dict[str, ResourceUsage]] | None = None
        self.fail: dict[str, KubernetesError] = {}
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.version = "v1.31.0"
        # Hooks let a test decide what a mutation does to the cluster.
        self.on_restart: Callable[[FakeGateway, str, str], None] | None = None
        self.on_delete: Callable[[FakeGateway, str, str], None] | None = None
        self.write_error: KubernetesError | None = None
        self._lock = threading.Lock()

    # -- helpers --------------------------------------------------------------------
    def _check(self, op: str) -> None:
        if op in self.fail:
            raise self.fail[op]

    def break_all(self, message: str = "connection refused") -> None:
        for op in ("nodes", "pods", "deployments", "pvcs", "events", "ping"):
            self.fail[op] = KubernetesUnavailableError(message)

    def heal_all(self) -> None:
        self.fail.clear()

    def writes(self) -> list[tuple[str, tuple[object, ...]]]:
        return [c for c in self.calls if c[0] in ("restart_deployment", "delete_pod")]

    # -- KubeGateway ----------------------------------------------------------------
    def ping(self) -> str:
        self._check("ping")
        return self.version

    def list_nodes(self) -> list[NodeInfo]:
        self._check("nodes")
        return list(self.nodes)

    def list_pods(self, namespaces: Sequence[str] | None = None) -> list[PodInfo]:
        self._check("pods")
        return [p for p in self.pods if not namespaces or p.namespace in namespaces]

    def list_deployments(self, namespaces: Sequence[str] | None = None) -> list[DeploymentInfo]:
        self._check("deployments")
        return [d for d in self.deployments if not namespaces or d.namespace in namespaces]

    def list_pvcs(self, namespaces: Sequence[str] | None = None) -> list[PVCInfo]:
        self._check("pvcs")
        return [p for p in self.pvcs if not namespaces or p.namespace in namespaces]

    def list_events(self, namespaces: Sequence[str] | None = None) -> list[EventInfo]:
        self._check("events")
        return [e for e in self.events if not namespaces or e.namespace in namespaces]

    def get_node(self, name: str) -> NodeInfo | None:
        self._check("nodes")
        return next((n for n in self.nodes if n.name == name), None)

    def get_pod(self, namespace: str, name: str) -> PodInfo | None:
        self._check("pods")
        return next((p for p in self.pods if (p.namespace, p.name) == (namespace, name)), None)

    def get_deployment(self, namespace: str, name: str) -> DeploymentInfo | None:
        self._check("deployments")
        return next(
            (d for d in self.deployments if (d.namespace, d.name) == (namespace, name)), None
        )

    def get_pvc(self, namespace: str, name: str) -> PVCInfo | None:
        self._check("pvcs")
        return next((p for p in self.pvcs if (p.namespace, p.name) == (namespace, name)), None)

    def list_events_for(self, kind: str, namespace: str | None, name: str) -> list[EventInfo]:
        self._check("events")
        return [
            e
            for e in self.events
            if e.kind == kind and e.name == name and (e.namespace or None) == namespace
        ]

    def node_usage(self) -> dict[str, ResourceUsage]:
        if self.node_metrics is None:
            raise MetricsUnavailableError("metrics.k8s.io is not available")
        return self.node_metrics

    def pod_usage(
        self, namespace: str | None = None
    ) -> dict[tuple[str, str], dict[str, ResourceUsage]]:
        if self.pod_metrics is None:
            raise MetricsUnavailableError("metrics.k8s.io is not available")
        return self.pod_metrics

    def restart_deployment(self, namespace: str, name: str, uid: str, restarted_at: str) -> None:
        with self._lock:
            self.calls.append(("restart_deployment", (namespace, name, uid)))
            if self.write_error:
                raise self.write_error
            if self.on_restart:
                self.on_restart(self, namespace, name)
            else:  # default: controller observes the new generation and rolls out fine
                for i, d in enumerate(self.deployments):
                    if (d.namespace, d.name) == (namespace, name):
                        self.deployments[i] = replace(
                            d, generation=d.generation + 1, observed_generation=d.generation + 1
                        )

    def delete_pod(self, namespace: str, name: str, uid: str) -> None:
        with self._lock:
            self.calls.append(("delete_pod", (namespace, name, uid)))
            if self.write_error:
                raise self.write_error
            if self.on_delete:
                self.on_delete(self, namespace, name)
            else:  # default: old pod vanishes and its controller creates a healthy replacement
                old = next(p for p in self.pods if (p.namespace, p.name) == (namespace, name))
                self.pods = [p for p in self.pods if p is not old]
                self.pods.append(
                    pod(f"{name}-new", namespace, uid=f"{uid}-new", owner_uid=old.owner_uid)
                )
