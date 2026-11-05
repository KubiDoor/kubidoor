"""Root-cause rules: facts vs cause vs confidence, evidence-gated conclusions."""

from datetime import timedelta

import pytest

from kubidoor.core.config import MonitoringSettings
from kubidoor.core.exceptions import NotFoundError
from kubidoor.core.types import ResourceType
from kubidoor.diagnostics.engine import DiagnosticEngine
from kubidoor.diagnostics.rules import RULES, Diagnosis, DiagnosticContext
from kubidoor.kubernetes.models import Condition, ResourceUsage
from kubidoor.monitoring.checks import check_deployment, check_node, check_pod
from tests.fakes import (
    T0,
    FakeClock,
    FakeGateway,
    container,
    crashloop_pod,
    deployment,
    event,
    node,
    pod,
)

CFG = MonitoringSettings()


def diagnose(finding, ctx=None) -> Diagnosis:
    return RULES[finding.problem_type](finding, finding.subject, ctx or DiagnosticContext(now=T0))


def one(findings):
    assert len(findings) == 1
    return findings[0]


def test_diagnosis_rejects_invalid_confidence() -> None:
    with pytest.raises(ValueError):
        Diagnosis([], "x", "certain", [])


def test_every_rule_separates_facts_cause_confidence_actions() -> None:
    f = one(check_pod(crashloop_pod(), T0, CFG))
    d = diagnose(f)
    assert d.observed_facts and d.recommended_actions
    assert d.confidence in ("high", "medium", "low")
    assert any("exit code" in fact.lower() for fact in d.observed_facts)
    assert any("kubectl logs" in a for a in d.recommended_actions)
    # cause is hedged, never stated as confirmed
    assert d.probable_cause.lower().startswith(("probably", "the reason"))


def test_crashloop_without_exit_code_evidence_is_low_confidence() -> None:
    p = pod(
        containers=[container(state="waiting", reason="CrashLoopBackOff", ready=False, restarts=5)]
    )
    d = diagnose(one(check_pod(p, T0, CFG)))
    assert d.confidence == "low"
    assert "cannot be determined" in d.probable_cause


def test_crashloop_liveness_probe_events_raise_confidence() -> None:
    f = one(check_pod(crashloop_pod(), T0, CFG))
    ctx = DiagnosticContext(
        now=T0,
        events=[event("Pod", "crash-1", "Unhealthy", "Liveness probe failed: HTTP 500", count=4)],
    )
    d = diagnose(f, ctx)
    assert d.confidence == "high" and "liveness" in d.probable_cause.lower()


def test_oom_with_limit_and_metrics() -> None:
    p = pod(
        containers=[
            container(
                state="terminated",
                reason="OOMKilled",
                ready=False,
                exit_code=137,
                restarts=2,
                requests={"memory": "32Mi"},
                limits={"memory": "64Mi"},
            )
        ]
    )
    f = one(check_pod(p, T0, CFG))
    ctx = DiagnosticContext(now=T0, pod_usage={"app": ResourceUsage(10, 62 * 2**20)})
    d = diagnose(f, ctx)
    text = " ".join(d.observed_facts)
    assert "64Mi" in text and "32Mi" in text and "62 MiB" in text and "90%" in text
    assert d.confidence == "high"
    assert any("never changes limits" in a for a in d.recommended_actions)


def test_oom_without_metrics_says_unavailable_and_never_invents_usage() -> None:
    p = pod(
        containers=[container(state="terminated", reason="OOMKilled", ready=False, exit_code=137)]
    )
    d = diagnose(
        one(check_pod(p, T0, CFG)),
        DiagnosticContext(now=T0, usage_note="metrics-server is not installed"),
    )
    text = " ".join(d.observed_facts)
    assert "unavailable" in text and "metrics-server is not installed" in text
    assert "MiB" not in text
    assert d.confidence == "medium"  # no limit configured => weaker claim


@pytest.mark.parametrize(
    "message,expected_fragment,confidence",
    [
        ("manifest for nginx:nope not found: manifest unknown", "does not exist", "high"),
        (
            "pull access denied, repository does not exist or may require authorization",
            "rejected",
            "medium",
        ),
        ("toomanyrequests: You have reached your pull rate limit", "rate-limiting", "high"),
        ("dial tcp: lookup registry.example: no such host", "unreachable", "medium"),
        ("some unrecognised failure", "no event detail", "low"),
    ],
)
def test_image_pull_causes_follow_event_evidence(
    message: str, expected_fragment: str, confidence: str
) -> None:
    p = pod(
        containers=[
            container(state="waiting", reason="ImagePullBackOff", ready=False, image="nginx:nope")
        ],
        phase="Pending",
        image_pull_secrets=("regcred",),
    )
    f = one(check_pod(p, T0, CFG))
    d = diagnose(f, DiagnosticContext(now=T0, events=[event("Pod", "web-abc", "Failed", message)]))
    assert expected_fragment in d.probable_cause
    assert d.confidence == confidence
    facts = " ".join(d.observed_facts)
    assert "nginx:nope" in facts and "regcred" in facts and "never read" in facts
    assert any("registry authentication" in a.lower() for a in d.recommended_actions)


def test_image_without_tag_is_noted() -> None:
    p = pod(
        containers=[container(state="waiting", reason="ErrImagePull", ready=False, image="nginx")],
        phase="Pending",
    )
    d = diagnose(one(check_pod(p, T0, CFG)))
    assert any("no explicit tag" in f for f in d.observed_facts)


def pending(message: str | None = None, node_name: str | None = None):
    conds = [
        Condition(
            "PodScheduled", "False" if not node_name else "True", "Unschedulable", message, T0
        )
    ]
    return pod(
        phase="Pending",
        node=node_name,
        ready=False,
        created_at=T0 - timedelta(hours=1),
        conditions=conds,
    )


@pytest.mark.parametrize(
    "message,fragment",
    [
        ("0/3 nodes are available: 3 Insufficient memory.", "insufficient"),
        ("0/3 nodes are available: 3 node(s) had untolerated taint {k: v}.", "taints"),
        (
            "0/3 nodes are available: 3 node(s) didn't match Pod's node affinity/selector.",
            "node selector",
        ),
    ],
)
def test_pending_scheduling_causes_are_taken_from_scheduler_message(
    message: str, fragment: str
) -> None:
    f = one(check_pod(pending(message), T0, CFG))
    d = diagnose(f, DiagnosticContext(now=T0, nodes=[node()]))
    assert fragment in d.probable_cause.lower()
    assert d.confidence == "high"
    assert any("Nodes: 1 total, 1 Ready" in x for x in d.observed_facts)


def test_pending_never_claims_insufficient_resources_without_evidence() -> None:
    f = one(check_pod(pending(None), T0, CFG))
    d = diagnose(f)
    assert "insufficient" not in d.probable_cause.lower()
    assert d.confidence == "low"


def test_pending_on_assigned_node_uses_mount_events() -> None:
    f = one(check_pod(pending(None, node_name="node-1"), T0, CFG))
    d = diagnose(
        f,
        DiagnosticContext(now=T0, events=[event("Pod", "web-abc", "FailedMount", "mount failed")]),
    )
    assert "volume" in d.probable_cause.lower() and d.confidence == "high"


def test_node_not_ready_unknown_vs_false() -> None:
    unknown = diagnose(one(check_node(node(ready="Unknown"), T0)), DiagnosticContext(now=T0))
    false = diagnose(one(check_node(node(ready="False"), T0)), DiagnosticContext(now=T0))
    assert unknown.confidence == "medium" and "stopped reporting" in unknown.probable_cause
    assert false.confidence == "high"
    assert any("last transition" in f for f in unknown.observed_facts)
    assert any("kubelet" in a for a in unknown.recommended_actions)


def test_node_pressure_rule() -> None:
    f = one(check_node(node(pressure=["DiskPressure"]), T0))
    d = diagnose(f)
    assert "DiskPressure" in d.probable_cause and any(
        "disk" in a.lower() for a in d.recommended_actions
    )


def test_deployment_diagnosis_correlates_with_failing_pods() -> None:
    dep = deployment(replicas=3, ready=0, age=timedelta(hours=1))
    f = one(check_deployment(dep, T0, CFG))
    bad = [
        pod(
            f"w{i}",
            containers=[container(state="waiting", reason="ImagePullBackOff", ready=False)],
            phase="Pending",
        )
        for i in range(3)
    ]
    d = diagnose(f, DiagnosticContext(now=T0, pods=bad))
    assert "3x ImagePullBackOff" in " ".join(d.observed_facts)
    assert "pod incidents are the likely root cause" in d.probable_cause
    assert any("does not fix image" in a for a in d.recommended_actions)


def test_deployment_without_failing_pods_is_low_confidence() -> None:
    f = one(check_deployment(deployment(replicas=3, ready=1, age=timedelta(hours=1)), T0, CFG))
    d = diagnose(f, DiagnosticContext(now=T0, pods=[pod("ok")]))
    assert d.confidence == "low"


# ----------------------------------------------------------------- on-demand engine
def make_engine(gw: FakeGateway) -> DiagnosticEngine:
    return DiagnosticEngine(gw, CFG, FakeClock())


def test_on_demand_report_for_unhealthy_pod_ignores_alert_thresholds() -> None:
    gw = FakeGateway()
    young_pending = pending("0/1 nodes are available: 1 Insufficient cpu.")
    gw.pods = [young_pending]
    gw.nodes = [node()]
    report = make_engine(gw).diagnose_resource(ResourceType.POD, "demo", young_pending.name)
    assert not report.healthy and report.entries[0].problem_type == "PodPending"
    assert report.metrics_available is False and "not available" in report.metrics_note


def test_on_demand_report_healthy_and_missing() -> None:
    gw = FakeGateway()
    gw.pods = [pod()]
    engine = make_engine(gw)
    assert engine.diagnose_resource(ResourceType.POD, "demo", "web-abc").healthy
    with pytest.raises(NotFoundError):
        engine.diagnose_resource(ResourceType.POD, "demo", "ghost")
    with pytest.raises(NotFoundError):
        engine.diagnose_resource(ResourceType.DEPLOYMENT, None, "web")  # namespace required


def test_on_demand_uses_real_metrics_when_available() -> None:
    gw = FakeGateway()
    oom = pod(
        "oom",
        containers=[
            container(
                state="terminated",
                reason="OOMKilled",
                ready=False,
                exit_code=137,
                limits={"memory": "64Mi"},
            )
        ],
    )
    gw.pods = [oom]
    gw.pod_metrics = {("demo", "oom"): {"app": ResourceUsage(5, 40 * 2**20)}}
    report = make_engine(gw).diagnose_resource(ResourceType.POD, "demo", "oom")
    assert report.metrics_available
    assert "40 MiB" in " ".join(report.entries[0].diagnosis.observed_facts)
