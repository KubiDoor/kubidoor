"""Pod / node / deployment / PVC health detection."""

from datetime import timedelta

import pytest

from kubidoor.core.config import MonitoringSettings
from kubidoor.core.types import ResourceType, Severity
from kubidoor.kubernetes.models import Condition
from kubidoor.monitoring.checks import check_deployment, check_node, check_pod, check_pvc
from tests.fakes import T0, container, crashloop_pod, deployment, event, node, pod, pvc

CFG = MonitoringSettings()


def problems(findings: list) -> set[str]:
    return {f.problem_type for f in findings}


# ----------------------------------------------------------------------------- pods
def test_healthy_pod_has_no_findings() -> None:
    assert check_pod(pod(), T0, CFG) == []


def test_crashloop_detected_with_evidence() -> None:
    [f] = check_pod(crashloop_pod(), T0, CFG)
    assert f.problem_type == "CrashLoopBackOff"
    assert f.severity == Severity.HIGH
    assert f.resource_type == ResourceType.POD
    assert f.container == "app"
    ev = f.evidence
    assert ev["pod_uid"] == "uid-demo-crash-1"
    assert ev["node_name"] == "node-1"
    assert ev["restart_count"] == 4
    assert ev["last_exit_code"] == 1
    assert ev["state_reason"] == "CrashLoopBackOff"
    assert ev["created_at"] is not None


@pytest.mark.parametrize("reason", ["ImagePullBackOff", "ErrImagePull"])
def test_image_pull_failures(reason: str) -> None:
    p = pod(
        containers=[container(state="waiting", reason=reason, ready=False, image="nope:1")],
        phase="Pending",
    )
    [f] = check_pod(p, T0, CFG)
    assert f.problem_type == "ImagePullBackOff"
    assert "nope:1" in f.description


def test_oomkilled_current_and_recent_termination() -> None:
    now_killed = pod(
        containers=[
            container(
                state="terminated", reason="OOMKilled", ready=False, exit_code=137, restarts=1
            )
        ]
    )
    recent = pod(
        containers=[
            container(
                state="waiting",
                reason="CrashLoopBackOff",
                ready=False,
                restarts=3,
                last_reason="OOMKilled",
                last_exit_code=137,
                last_finished_at=T0 - timedelta(seconds=20),
            )
        ]
    )
    assert problems(check_pod(now_killed, T0, CFG)) == {"OOMKilled"}
    # OOM is the root cause, so no duplicate CrashLoopBackOff incident for the same container
    assert problems(check_pod(recent, T0, CFG)) == {"OOMKilled"}


def test_old_oomkill_is_not_reported() -> None:
    old = pod(
        containers=[
            container(
                restarts=1,
                last_reason="OOMKilled",
                last_exit_code=137,
                last_finished_at=T0 - timedelta(hours=5),
            )
        ]
    )
    assert check_pod(old, T0, CFG) == []


def test_pending_pod_uses_threshold() -> None:
    young = pod(phase="Pending", node=None, created_at=T0 - timedelta(seconds=30), ready=False)
    old = pod(phase="Pending", node=None, created_at=T0 - timedelta(seconds=600), ready=False)
    assert check_pod(young, T0, CFG) == []  # not every Pending pod is unhealthy
    [f] = check_pod(old, T0, CFG)
    assert f.problem_type == "PodPending"
    assert f.evidence["pending_seconds"] == 600


def test_pending_pod_with_image_pull_error_is_not_double_reported() -> None:
    p = pod(
        phase="Pending",
        created_at=T0 - timedelta(hours=1),
        containers=[container(state="waiting", reason="ImagePullBackOff", ready=False)],
    )
    assert problems(check_pod(p, T0, CFG)) == {"ImagePullBackOff"}


def test_failed_and_unknown_phases() -> None:
    assert problems(check_pod(pod(phase="Failed", ready=False), T0, CFG)) == {"PodFailed"}
    assert problems(check_pod(pod(phase="Unknown", ready=False), T0, CFG)) == {"PodUnknown"}


def test_succeeded_and_terminating_pods_ignored() -> None:
    assert check_pod(pod(phase="Succeeded", ready=False), T0, CFG) == []
    assert check_pod(pod(phase="Running", ready=False, deletion=T0), T0, CFG) == []


def test_containers_not_ready_respects_threshold() -> None:
    def unready(since: timedelta):
        return pod(
            containers=[container(ready=False)],
            conditions=[Condition("Ready", "False", last_transition=T0 - since)],
        )

    assert check_pod(unready(timedelta(seconds=10)), T0, CFG) == []
    assert problems(check_pod(unready(timedelta(minutes=10)), T0, CFG)) == {"ContainersNotReady"}


def test_excessive_restarts_requires_recent_termination() -> None:
    kwargs = dict(restarts=9, last_reason="Error", last_exit_code=1)
    recent = pod(containers=[container(**kwargs, last_finished_at=T0 - timedelta(seconds=60))])
    stale = pod(containers=[container(**kwargs, last_finished_at=T0 - timedelta(days=2))])
    assert problems(check_pod(recent, T0, CFG)) == {"ExcessiveRestarts"}
    assert check_pod(stale, T0, CFG) == []


def test_each_failing_container_gets_its_own_finding() -> None:
    p = pod(
        containers=[
            container("a", state="waiting", reason="CrashLoopBackOff", ready=False, restarts=5),
            container("b", state="waiting", reason="CrashLoopBackOff", ready=False, restarts=5),
        ]
    )
    findings = check_pod(p, T0, CFG)
    assert len(findings) == 2
    assert len({f.fingerprint for f in findings}) == 2


# ---------------------------------------------------------------------------- nodes
def test_node_ready_is_healthy() -> None:
    assert check_node(node(), T0) == []


@pytest.mark.parametrize("status", ["False", "Unknown"])
def test_node_not_ready(status: str) -> None:
    [f] = check_node(node(ready=status), T0)
    assert f.problem_type == "NodeNotReady"
    assert f.severity == Severity.CRITICAL
    assert f.namespace is None
    assert f.evidence["allocatable"]["cpu"] == "3900m"


@pytest.mark.parametrize(
    "condition,problem",
    [
        ("MemoryPressure", "NodeMemoryPressure"),
        ("DiskPressure", "NodeDiskPressure"),
        ("PIDPressure", "NodePIDPressure"),
        ("NetworkUnavailable", "NodeNetworkUnavailable"),
    ],
)
def test_node_pressure_conditions(condition: str, problem: str) -> None:
    assert problems(check_node(node(pressure=[condition]), T0)) == {problem}


# ------------------------------------------------------------------------ deployments
def test_healthy_deployment() -> None:
    assert check_deployment(deployment(), T0, CFG) == []


def test_scaling_up_deployment_is_not_reported_within_grace() -> None:
    fresh = deployment(replicas=5, ready=2, age=timedelta(seconds=30))
    assert check_deployment(fresh, T0, CFG) == []


def test_stalled_deployment_reported_after_grace() -> None:
    stalled = deployment(replicas=3, ready=1, age=timedelta(minutes=30))
    [f] = check_deployment(stalled, T0, CFG)
    assert f.problem_type == "ReplicasUnavailable"
    assert f.severity == Severity.MEDIUM
    zero = deployment(replicas=3, ready=0, age=timedelta(minutes=30))
    assert check_deployment(zero, T0, CFG)[0].severity == Severity.HIGH


def test_progress_deadline_exceeded() -> None:
    d = deployment(
        replicas=3,
        ready=1,
        conditions=[
            Condition("Progressing", "False", "ProgressDeadlineExceeded", "timed out", T0, T0)
        ],
    )
    assert problems(check_deployment(d, T0, CFG)) == {"ProgressDeadlineExceeded"}


def test_replica_failure_condition() -> None:
    d = deployment(
        replicas=3,
        ready=3,
        conditions=[Condition("ReplicaFailure", "True", "FailedCreate", "quota exceeded", T0)],
    )
    assert problems(check_deployment(d, T0, CFG)) == {"ReplicaFailure"}


def test_paused_or_scaled_to_zero_deployment_ignored() -> None:
    assert (
        check_deployment(
            deployment(replicas=3, ready=0, paused=True, age=timedelta(days=1)), T0, CFG
        )
        == []
    )
    assert check_deployment(deployment(replicas=0, age=timedelta(days=1)), T0, CFG) == []


# ----------------------------------------------------------------------------- PVCs
def test_bound_pvc_is_healthy() -> None:
    assert check_pvc(pvc(), T0, CFG, []) == []


def test_pending_pvc_threshold_and_lost() -> None:
    assert check_pvc(pvc(phase="Pending", age=timedelta(seconds=5)), T0, CFG, []) == []
    [f] = check_pvc(pvc(phase="Pending", age=timedelta(minutes=10)), T0, CFG, [])
    assert f.problem_type == "PVCPending"
    [lost] = check_pvc(pvc(phase="Lost"), T0, CFG, [])
    assert lost.problem_type == "PVCLost"
    assert lost.severity == Severity.HIGH


def test_pvc_waiting_for_first_consumer_is_not_a_problem() -> None:
    events = [
        event("PersistentVolumeClaim", "data", "WaitForFirstConsumer", "waiting", type_="Normal")
    ]
    assert check_pvc(pvc(phase="Pending", age=timedelta(hours=1)), T0, CFG, events) == []
    failing = events + [event("PersistentVolumeClaim", "data", "ProvisioningFailed", "boom")]
    assert problems(check_pvc(pvc(phase="Pending", age=timedelta(hours=1)), T0, CFG, failing)) == {
        "PVCPending"
    }
