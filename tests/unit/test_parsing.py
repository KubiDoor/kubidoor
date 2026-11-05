"""Parsing real kubernetes.client objects, quantities and metrics-server payloads."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from kubernetes import client

from kubidoor.kubernetes.deployments import parse_deployment
from kubidoor.kubernetes.metrics import (
    parse_cpu_millicores,
    parse_memory_bytes,
    parse_node_metrics,
    parse_pod_metrics,
    parse_quantity,
)
from kubidoor.kubernetes.nodes import parse_node, parse_pvc
from kubidoor.kubernetes.pods import parse_pod

TS = datetime(2026, 11, 3, 12, 0, 0, tzinfo=UTC)


def k8s_pod() -> client.V1Pod:
    return client.V1Pod(
        metadata=client.V1ObjectMeta(
            name="web-1",
            namespace="demo",
            uid="u-1",
            creation_timestamp=TS,
            labels={"app": "web"},
            owner_references=[
                client.V1OwnerReference(
                    api_version="apps/v1",
                    kind="ReplicaSet",
                    name="web-rs",
                    uid="rs-1",
                    controller=True,
                )
            ],
        ),
        spec=client.V1PodSpec(
            node_name="n1",
            image_pull_secrets=[client.V1LocalObjectReference(name="regcred")],
            containers=[
                client.V1Container(
                    name="app",
                    image="busybox:1",
                    env=[client.V1EnvVar(name="DB_PASSWORD", value="hunter2")],
                    resources=client.V1ResourceRequirements(
                        requests={"memory": "32Mi"}, limits={"memory": "64Mi"}
                    ),
                )
            ],
        ),
        status=client.V1PodStatus(
            phase="Running",
            conditions=[
                client.V1PodCondition(type="Ready", status="False", last_transition_time=TS)
            ],
            container_statuses=[
                client.V1ContainerStatus(
                    name="app",
                    image="busybox:1",
                    image_id="x",
                    ready=False,
                    restart_count=7,
                    state=client.V1ContainerState(
                        waiting=client.V1ContainerStateWaiting(
                            reason="CrashLoopBackOff", message="back-off"
                        )
                    ),
                    last_state=client.V1ContainerState(
                        terminated=client.V1ContainerStateTerminated(
                            exit_code=137, reason="OOMKilled", finished_at=TS
                        )
                    ),
                )
            ],
        ),
    )


def test_parse_pod_extracts_required_fields() -> None:
    p = parse_pod(k8s_pod())
    assert (p.namespace, p.name, p.uid, p.node_name) == ("demo", "web-1", "u-1", "n1")
    assert p.created_at == TS and p.phase == "Running"
    c = p.containers[0]
    assert c.state == "waiting" and c.state_reason == "CrashLoopBackOff"
    assert c.restart_count == 7
    assert (c.last_reason, c.last_exit_code, c.last_finished_at) == ("OOMKilled", 137, TS)
    assert c.limits == {"memory": "64Mi"} and c.requests == {"memory": "32Mi"}
    assert (p.owner_kind, p.owner_uid) == ("ReplicaSet", "rs-1")
    assert p.image_pull_secrets == ("regcred",)  # name only


def test_parsed_pod_never_carries_env_values() -> None:
    assert "hunter2" not in repr(parse_pod(k8s_pod()))


def test_parse_pod_without_container_statuses_is_waiting_not_crashing() -> None:
    raw = k8s_pod()
    raw.status.container_statuses = None
    raw.spec.node_name = None
    raw.status.phase = "Pending"
    p = parse_pod(raw)
    assert p.containers[0].state == "waiting" and p.containers[0].restart_count == 0


def test_parse_node_and_pvc_and_deployment() -> None:
    n = parse_node(
        client.V1Node(
            metadata=client.V1ObjectMeta(name="n1", uid="n-u", creation_timestamp=TS),
            spec=client.V1NodeSpec(unschedulable=True),
            status=client.V1NodeStatus(
                capacity={"cpu": "4"},
                allocatable={"cpu": "3"},
                conditions=[
                    client.V1NodeCondition(
                        type="Ready", status="Unknown", reason="NodeStatusUnknown"
                    )
                ],
            ),
        )
    )
    assert n.ready_status == "Unknown" and n.unschedulable and n.allocatable == {"cpu": "3"}

    c = parse_pvc(
        client.V1PersistentVolumeClaim(
            metadata=client.V1ObjectMeta(
                name="d", namespace="demo", uid="p-u", creation_timestamp=TS
            ),
            spec=client.V1PersistentVolumeClaimSpec(
                storage_class_name="fast",
                resources=client.V1VolumeResourceRequirements(requests={"storage": "5Gi"}),
            ),
            status=client.V1PersistentVolumeClaimStatus(phase="Pending"),
        )
    )
    assert (c.phase, c.storage_class, c.requested_storage) == ("Pending", "fast", "5Gi")

    d = parse_deployment(
        client.V1Deployment(
            metadata=client.V1ObjectMeta(
                name="web", namespace="demo", uid="d-u", generation=4, creation_timestamp=TS
            ),
            spec=client.V1DeploymentSpec(
                selector=client.V1LabelSelector(match_labels={"app": "web"}),
                template=client.V1PodTemplateSpec(),
            ),
            status=client.V1DeploymentStatus(
                observed_generation=3, ready_replicas=None, unavailable_replicas=1
            ),
        )
    )
    assert d.replicas == 1  # spec.replicas omitted => Kubernetes default of 1
    assert (d.generation, d.observed_generation, d.ready_replicas, d.unavailable_replicas) == (
        4,
        3,
        0,
        1,
    )
    assert d.selector == {"app": "web"}


@pytest.mark.parametrize(
    "text,expected",
    [
        ("2", 2),
        ("250m", Decimal("0.25")),
        ("64Mi", 64 * 2**20),
        ("1Gi", 2**30),
        ("1k", 1000),
        ("1e3", 1000),
    ],
)
def test_parse_quantity(text: str, expected: object) -> None:
    assert parse_quantity(text) == Decimal(str(expected))


def test_quantity_helpers_and_errors() -> None:
    assert parse_cpu_millicores("250m") == 250
    assert parse_cpu_millicores("2") == 2000
    assert parse_memory_bytes("128Mi") == 128 * 2**20
    with pytest.raises(ValueError):
        parse_quantity("lots")
    with pytest.raises(ValueError):
        parse_quantity("12Zi")


def test_metrics_server_payload_parsing() -> None:
    nodes = parse_node_metrics(
        {"items": [{"metadata": {"name": "n1"}, "usage": {"cpu": "500m", "memory": "1Gi"}}]}
    )
    assert nodes["n1"].cpu_millicores == 500 and nodes["n1"].memory_bytes == 2**30
    pods = parse_pod_metrics(
        {
            "items": [
                {
                    "metadata": {"name": "p", "namespace": "demo"},
                    "containers": [{"name": "app", "usage": {"cpu": "10m", "memory": "20Mi"}}],
                }
            ]
        }
    )
    assert pods[("demo", "p")]["app"].memory_bytes == 20 * 2**20
    assert parse_node_metrics({}) == {}
