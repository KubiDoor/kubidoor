"""End-to-end test against a REAL kind cluster (optional; NOT run by `make test`).

    make demo-up        # creates the kind cluster and the demo scenarios
    make e2e            # runs this file (sets KUBIDOOR_E2E=1)

Safety: pinned to the `kind-kubidoor` context and the `kubidoor-demo` namespace; skipped unless
KUBIDOOR_E2E=1. The only mutation performed is deleting ONE demo pod after explicit approval.
"""

import os
import shutil
import subprocess
import time

import pytest

from kubidoor.container import build_services
from kubidoor.core.config import KubernetesSettings, MonitoringSettings, RecoverySettings, Settings
from kubidoor.core.types import ActionType
from kubidoor.database.base import Base
from kubidoor.incidents.models import IncidentFilters

CONTEXT = "kind-kubidoor"
NS = "kubidoor-demo"


def _cluster_ready() -> bool:
    if os.environ.get("KUBIDOOR_E2E") != "1" or not shutil.which("kubectl"):
        return False
    r = subprocess.run(  # noqa: S603
        ["kubectl", "--context", CONTEXT, "-n", NS, "get", "deploy", "demo-crashloop"],  # noqa: S607
        capture_output=True,
        timeout=30,
    )
    return r.returncode == 0


pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not _cluster_ready(), reason="set KUBIDOOR_E2E=1 and run `make demo-up` first"
    ),
]


@pytest.fixture(scope="module")
def svc(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    settings = Settings(
        database_url=f"sqlite:///{tmp_path_factory.mktemp('e2e') / 'e2e.db'}",
        auto_migrate=False,
        kubernetes=KubernetesSettings(context=CONTEXT),
        monitoring=MonitoringSettings(
            namespaces=[NS],
            deployment_grace_seconds=30,
            pending_threshold_seconds=30,
            not_ready_threshold_seconds=30,
            restart_threshold=2,
        ),
        recovery=RecoverySettings(
            enabled=True,
            dry_run=False,
            require_approval=True,
            allowed_namespaces=[NS],
            cooldown_seconds=0,
            verification_timeout_seconds=60,
            verification_poll_seconds=3,
        ),
    )
    s = build_services(settings)
    Base.metadata.create_all(s.db_engine)
    yield s
    s.recovery.shutdown(timeout=90)
    s.db_engine.dispose()


def _incident_problems(svc) -> dict[str, str]:  # type: ignore[no-untyped-def]
    rows, _ = svc.incidents.list_incidents(IncidentFilters(), 200, 0)
    return {f"{r.resource_name}": r.problem_type for r in rows}


def test_all_demo_scenarios_are_detected_and_healthy_workload_is_not(svc) -> None:  # type: ignore[no-untyped-def]
    wanted = {"CrashLoopBackOff", "ImagePullBackOff", "OOMKilled"}
    deadline = time.time() + 360
    problems: dict[str, str] = {}
    while time.time() < deadline:
        res = svc.monitoring.run_cycle()
        assert res.status in ("ok", "partial"), res.errors
        problems = _incident_problems(svc)
        found = set(problems.values())
        unavailable = any(v in ("ReplicasUnavailable", "ProgressDeadlineExceeded") for v in found)
        if wanted <= found and unavailable:
            break
        time.sleep(10)
    found = set(problems.values())
    assert wanted <= found, problems
    assert {"ReplicasUnavailable", "ProgressDeadlineExceeded"} & found, problems
    assert not any(name.startswith("demo-healthy") for name in problems), problems


def test_diagnostics_are_evidence_based_on_a_real_cluster(svc) -> None:  # type: ignore[no-untyped-def]
    rows, _ = svc.incidents.list_incidents(IncidentFilters(), 200, 0)
    oom = next(r for r in rows if r.problem_type == "OOMKilled")
    facts = " ".join(oom.evidence["diagnosis"]["observed_facts"])
    assert "64Mi" in facts  # the real configured limit
    # kind has no metrics-server: KubiDoor must say so instead of inventing numbers
    assert "unavailable" in facts


def test_recovery_deletes_one_demo_pod_only_after_approval(svc) -> None:  # type: ignore[no-untyped-def]
    rows, _ = svc.incidents.list_incidents(IncidentFilters(), 200, 0)
    crash = next(r for r in rows if r.problem_type == "CrashLoopBackOff")
    action, _ = svc.recovery.request_action(crash.id, ActionType.POD_DELETE, "e2e")
    assert action.status == "PENDING_APPROVAL"
    svc.recovery.approve(action.id, "e2e-approver")
    done = svc.recovery.execute_and_verify(action.id, "e2e-approver")
    # The pod WAS deleted and its controller recreated it, but a crash-looping workload cannot become
    # Ready, so verification must report FAILED (an API success is not recovery) and keep the incident.
    assert done.status == "FAILED" and "verification" in str(done.result)
    assert svc.incidents.get(crash.id).status != "RESOLVED"
