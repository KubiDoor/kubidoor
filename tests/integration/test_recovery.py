"""Recovery engine end-to-end against the DB with a fake cluster."""

import threading
from dataclasses import replace

import pytest
from sqlalchemy import select

from kubidoor.container import Services
from kubidoor.core.exceptions import (
    ConflictError,
    KubernetesUnavailableError,
    NotFoundError,
    PolicyViolationError,
)
from kubidoor.core.types import ActionType, ResourceType
from kubidoor.database.models import AuditLog, Incident, RecoveryAction
from kubidoor.database.session import session_scope
from kubidoor.incidents.models import IncidentFilters
from tests.fakes import (
    FakeClock,
    FakeGateway,
    container,
    crashloop_pod,
    deployment,
    node,
    pod,
)

pytestmark = pytest.mark.integration


def audit_events(svc: Services, action_id: str | None = None) -> list[str]:
    with session_scope(svc.session_factory) as s:
        q = select(AuditLog.event_type).order_by(AuditLog.id)
        if action_id:
            q = q.where(AuditLog.action_id == action_id)
        return list(s.scalars(q))


def only_incident(svc: Services) -> Incident:
    rows, _ = svc.incidents.list_incidents(IncidentFilters(), 10, 0)
    assert len(rows) == 1
    return rows[0]


def crash_incident(svc: Services, gw: FakeGateway, namespace: str = "demo") -> Incident:
    gw.pods = [crashloop_pod(namespace=namespace)]
    svc.monitoring.run_cycle()
    return only_incident(svc)


def approved_pod_action(svc: Services, gw: FakeGateway, approver: str = "alice") -> RecoveryAction:
    inc = crash_incident(svc, gw)
    action, _ = svc.recovery.request_action(inc.id, ActionType.POD_DELETE, "tester")
    return svc.recovery.approve(action.id, approver)


def healthy_rollout(gw: FakeGateway, ns: str, name: str) -> None:
    for i, d in enumerate(gw.deployments):
        if (d.namespace, d.name) == (ns, name):
            gw.deployments[i] = replace(
                d,
                generation=d.generation + 1,
                observed_generation=d.generation + 1,
                ready_replicas=d.replicas,
                available_replicas=d.replicas,
                updated_replicas=d.replicas,
                unavailable_replicas=0,
            )


# ------------------------------------------------------------------ safe defaults
def test_recovery_is_disabled_by_default(gateway: FakeGateway, make_services) -> None:
    svc = make_services(enabled=False)
    inc = crash_incident(svc, gateway)
    with pytest.raises(PolicyViolationError) as e:
        svc.recovery.request_action(inc.id, ActionType.POD_DELETE, "alice")
    assert any("globally disabled" in v for v in e.value.violations)
    assert gateway.writes() == []
    assert "RECOVERY_REQUEST_DENIED" in audit_events(svc)
    assert svc.recovery.list_actions(status=None, incident_id=None, limit=10, offset=0)[1] == 0


def test_monitor_does_not_propose_when_disabled(gateway: FakeGateway, make_services) -> None:
    svc = make_services(enabled=False)
    crash_incident(svc, gateway)
    assert svc.recovery.list_actions(status=None, incident_id=None, limit=10, offset=0)[1] == 0


def test_untouched_settings_object_has_safe_defaults() -> None:
    from kubidoor.core.config import Settings

    r = Settings(database_url="sqlite://").recovery
    assert (r.enabled, r.dry_run, r.require_approval) == (False, True, True)


# ----------------------------------------------------------------------- approval
def test_monitor_proposes_action_pending_approval_and_nothing_runs(
    gateway: FakeGateway, services: Services
) -> None:
    crash_incident(services, gateway)
    [a], total = services.recovery.list_actions(status=None, incident_id=None, limit=10, offset=0)
    assert total == 1 and a.status == "PENDING_APPROVAL" and a.requested_by == "monitor"
    assert a.action_type == "pod_delete" and a.resource_uid == "uid-demo-crash-1"
    assert gateway.writes() == []
    assert only_incident(services).status == "OPEN"  # a proposal alone doesn't mean "investigating"


def test_execute_requires_approval(gateway: FakeGateway, services: Services) -> None:
    crash_incident(services, gateway)
    [a], _ = services.recovery.list_actions(status=None, incident_id=None, limit=10, offset=0)
    with pytest.raises(ConflictError):
        services.recovery.execute(a.id, "alice")
    assert gateway.writes() == []


def test_reject_blocks_execution_and_is_audited(gateway: FakeGateway, services: Services) -> None:
    crash_incident(services, gateway)
    [a], _ = services.recovery.list_actions(status=None, incident_id=None, limit=10, offset=0)
    rejected = services.recovery.reject(a.id, "bob", "not now")
    assert rejected.status == "REJECTED" and rejected.error_message == "not now"
    with pytest.raises(ConflictError):
        services.recovery.execute(a.id, "bob")
    with pytest.raises(ConflictError):
        services.recovery.approve(a.id, "bob")
    assert gateway.writes() == []
    assert audit_events(services, a.id) == ["RECOVERY_REQUESTED", "RECOVERY_REJECTED"]


def test_approve_twice_conflicts(gateway: FakeGateway, services: Services) -> None:
    act = approved_pod_action(services, gateway)
    assert act.status == "APPROVED" and act.approved_by == "alice"
    with pytest.raises(ConflictError):
        services.recovery.approve(act.id, "carol")
    assert services.recovery.get_action(act.id).approved_by == "alice"


def test_unknown_action_is_not_found(services: Services) -> None:
    with pytest.raises(NotFoundError):
        services.recovery.get_action("nope")
    with pytest.raises(NotFoundError):
        services.recovery.approve("nope", "alice")
    with pytest.raises(NotFoundError):
        services.recovery.execute("nope", "alice")


def test_request_is_idempotent_while_active(gateway: FakeGateway, services: Services) -> None:
    inc = crash_incident(services, gateway)  # the monitor has already proposed pod_delete
    [proposed], _ = services.recovery.list_actions(
        status=None, incident_id=inc.id, limit=5, offset=0
    )
    a1, created1 = services.recovery.request_action(inc.id, ActionType.POD_DELETE, "alice")
    a2, created2 = services.recovery.request_action(inc.id, ActionType.POD_DELETE, "bob")
    assert (created1, created2) == (False, False)
    assert a1.id == a2.id == proposed.id
    assert services.recovery.list_actions(status=None, incident_id=None, limit=10, offset=0)[1] == 1


def test_new_action_allowed_once_previous_one_is_finished(
    gateway: FakeGateway, services: Services
) -> None:
    inc = crash_incident(services, gateway)
    [first], _ = services.recovery.list_actions(status=None, incident_id=inc.id, limit=5, offset=0)
    services.recovery.reject(first.id, "alice")
    second, created = services.recovery.request_action(inc.id, ActionType.POD_DELETE, "alice")
    assert created and second.id != first.id


# ------------------------------------------------------------------------ dry run
def test_dry_run_never_touches_the_cluster(gateway: FakeGateway, make_services) -> None:
    svc = make_services(dry_run=True)
    act = approved_pod_action(svc, gateway)
    assert act.dry_run is True
    out = svc.recovery.execute(act.id, "alice")
    assert out.status == "DRY_RUN" and out.executed_at is None
    assert "Would delete pod demo/crash-1" in out.result["summary"]
    assert gateway.writes() == []
    assert svc.incidents.get(out.incident_id).status == "OPEN"
    assert audit_events(svc, act.id)[-1] == "RECOVERY_DRY_RUN"


def test_dry_run_can_be_tightened_after_request(gateway: FakeGateway, services: Services) -> None:
    act = approved_pod_action(services, gateway)
    assert act.dry_run is False
    services.settings.recovery.dry_run = True  # operator flips the switch before execution
    assert services.recovery.execute(act.id, "alice").status == "DRY_RUN"
    assert gateway.writes() == []


def test_dry_run_does_not_consume_cooldown_or_rate_limit(
    gateway: FakeGateway, make_services
) -> None:
    svc = make_services(dry_run=True, max_actions_per_hour=1)
    inc = crash_incident(svc, gateway)
    for _ in range(3):
        a, _ = svc.recovery.request_action(inc.id, ActionType.POD_DELETE, "alice")
        svc.recovery.approve(a.id, "alice")
        assert svc.recovery.execute(a.id, "alice").status == "DRY_RUN"


# --------------------------------------------------------------- policy at request time
def test_namespace_not_allowed_is_denied(gateway: FakeGateway, services: Services) -> None:
    gateway.pods = [crashloop_pod(namespace="production")]
    services.monitoring.run_cycle()
    inc = only_incident(services)
    with pytest.raises(PolicyViolationError) as e:
        services.recovery.request_action(inc.id, ActionType.POD_DELETE, "alice")
    assert any("not explicitly permitted" in v for v in e.value.violations)


def test_kube_system_is_blocked_even_when_allowlisted(gateway: FakeGateway, make_services) -> None:
    svc = make_services(allowed_namespaces=["demo", "kube-system"])
    gateway.pods = [crashloop_pod(namespace="kube-system")]
    svc.monitoring.run_cycle()
    inc = only_incident(svc)
    with pytest.raises(PolicyViolationError) as e:
        svc.recovery.request_action(inc.id, ActionType.POD_DELETE, "alice")
    assert any("blocked" in v for v in e.value.violations)
    assert gateway.writes() == []


def test_action_not_allowlisted_is_denied(gateway: FakeGateway, make_services) -> None:
    svc = make_services(allowed_actions=["deployment_restart"])
    inc = crash_incident(svc, gateway)
    with pytest.raises(PolicyViolationError):
        svc.recovery.request_action(inc.id, ActionType.POD_DELETE, "alice")


def test_node_and_pvc_incidents_can_never_get_an_action(
    gateway: FakeGateway, services: Services
) -> None:
    gateway.nodes = [node(ready="False")]
    services.monitoring.run_cycle()
    inc = only_incident(services)
    assert inc.resource_type == ResourceType.NODE
    for action in ActionType:
        with pytest.raises(PolicyViolationError):
            services.recovery.request_action(inc.id, action, "alice")
    assert gateway.writes() == []


def test_wrong_action_for_resource_type_is_denied(gateway: FakeGateway, services: Services) -> None:
    inc = crash_incident(services, gateway)  # a Pod incident
    with pytest.raises(PolicyViolationError) as e:
        services.recovery.request_action(inc.id, ActionType.DEPLOYMENT_RESTART, "alice")
    assert any("not appropriate" in v for v in e.value.violations)


def test_action_for_resolved_incident_is_refused(gateway: FakeGateway, services: Services) -> None:
    inc = crash_incident(services, gateway)
    services.incidents.resolve(inc.id, "alice")
    with pytest.raises(ConflictError):
        services.recovery.request_action(inc.id, ActionType.POD_DELETE, "alice")


# ------------------------------------------------------------------ execution + verify
def test_pod_delete_executes_verifies_and_resolves(
    gateway: FakeGateway, services: Services
) -> None:
    act = approved_pod_action(services, gateway)
    running = services.recovery.execute(act.id, "alice")
    assert running.status == "RUNNING" and running.executed_at is not None
    assert gateway.writes() == [("delete_pod", ("demo", "crash-1", "uid-demo-crash-1"))]
    assert services.incidents.get(act.incident_id).status == "INVESTIGATING"

    done = services.recovery.verify(act.id)
    assert done.status == "SUCCEEDED" and done.verified_at is not None
    assert "replacement pod is Running and Ready" in done.result["verification"]
    inc = services.incidents.get(act.incident_id)
    assert inc.status == "RESOLVED" and inc.resolved_by == "recovery"
    assert audit_events(services, act.id) == [
        "RECOVERY_REQUESTED",
        "RECOVERY_APPROVED",
        "RECOVERY_EXECUTED",
        "RECOVERY_VERIFIED",
    ]
    text = services.metrics.render().decode()
    assert (
        'kubidoor_recovery_actions_total{action_type="pod_delete",status="SUCCEEDED"} 1.0' in text
    )
    assert 'kubidoor_recovery_duration_seconds_count{action_type="pod_delete"} 1.0' in text


def test_deployment_restart_executes_and_verifies(gateway: FakeGateway, services: Services) -> None:
    gateway.deployments = [deployment("web", replicas=3, ready=0, available=0)]
    gateway.on_restart = lambda gw, ns, n: healthy_rollout(gw, ns, n)
    services.monitoring.run_cycle()
    inc = only_incident(services)
    assert inc.problem_type == "ReplicasUnavailable"
    [proposed], _ = services.recovery.list_actions(status=None, incident_id=None, limit=5, offset=0)
    assert proposed.action_type == "deployment_restart"
    services.recovery.approve(proposed.id, "alice")
    done = services.recovery.execute_and_verify(proposed.id, "alice")
    assert done.status == "SUCCEEDED"
    assert gateway.writes() == [("restart_deployment", ("demo", "web", "uid-dep-demo-web"))]
    assert services.incidents.get(inc.id).status == "RESOLVED"


def test_api_success_is_not_recovery_verification_failure_preserves_incident(
    gateway: FakeGateway, services: Services
) -> None:
    """Restart call succeeds, but the rollout never becomes healthy => FAILED, incident stays open."""
    gateway.deployments = [
        deployment("web", replicas=3, ready=0, available=0)
    ]  # default hook: not healthy
    services.monitoring.run_cycle()
    [proposed], _ = services.recovery.list_actions(status=None, incident_id=None, limit=5, offset=0)
    services.recovery.approve(proposed.id, "alice")
    done = services.recovery.execute_and_verify(proposed.id, "alice")
    assert done.status == "FAILED" and done.verified_at is None
    assert "timed out" in done.error_message
    assert (
        services.incidents.get(done.incident_id).status == "INVESTIGATING"
    )  # preserved, not resolved
    assert (
        'kubidoor_recovery_failures_total{action_type="deployment_restart"} 1.0'
        in services.metrics.render().decode()
    )
    assert "RECOVERY_FAILED" in audit_events(services, done.id)


def test_pod_replacement_that_never_becomes_ready_fails(
    gateway: FakeGateway, services: Services
) -> None:
    def broken_replacement(gw: FakeGateway, ns: str, name: str) -> None:
        old = next(p for p in gw.pods if p.name == name)
        gw.pods = [pod("r", ns, uid="r-uid", owner_uid=old.owner_uid, phase="Pending", ready=False)]

    gateway.on_delete = broken_replacement
    act = approved_pod_action(services, gateway)
    assert services.recovery.execute_and_verify(act.id, "alice").status == "FAILED"


def test_kubernetes_rejection_marks_failed_and_preserves_incident(
    gateway: FakeGateway, services: Services
) -> None:
    gateway.write_error = KubernetesUnavailableError("api down")
    act = approved_pod_action(services, gateway)
    out = services.recovery.execute(act.id, "alice")
    assert out.status == "FAILED" and "Kubernetes rejected" in out.error_message
    assert services.incidents.get(act.incident_id).status != "RESOLVED"


def test_uid_mismatch_cancels_without_touching_cluster(
    gateway: FakeGateway, services: Services
) -> None:
    act = approved_pod_action(services, gateway)
    gateway.pods = [replace(gateway.pods[0], uid="a-different-pod")]  # same name, new object
    out = services.recovery.execute(act.id, "alice")
    assert out.status == "CANCELLED" and "UID mismatch" in out.error_message
    assert gateway.writes() == []
    assert "RECOVERY_BLOCKED" in audit_events(services, act.id)


def test_target_already_gone_cancels(gateway: FakeGateway, services: Services) -> None:
    act = approved_pod_action(services, gateway)
    gateway.pods = []
    assert services.recovery.execute(act.id, "alice").status == "CANCELLED"
    assert gateway.writes() == []


def test_bare_pod_without_controller_is_never_deleted(
    gateway: FakeGateway, services: Services
) -> None:
    act = approved_pod_action(services, gateway)
    gateway.pods = [replace(gateway.pods[0], owner_kind=None, owner_uid=None)]
    out = services.recovery.execute(act.id, "alice")
    assert out.status == "CANCELLED" and "no controller" in out.error_message
    assert gateway.writes() == []


def test_policy_is_rechecked_at_execution_time(gateway: FakeGateway, services: Services) -> None:
    act = approved_pod_action(services, gateway)
    services.settings.recovery.enabled = False  # operator disables recovery after approval
    out = services.recovery.execute(act.id, "alice")
    assert out.status == "CANCELLED" and "globally disabled" in out.error_message
    assert gateway.writes() == []


def test_cannot_verify_target_when_api_is_down_cancels_safely(
    gateway: FakeGateway, services: Services
) -> None:
    act = approved_pod_action(services, gateway)
    gateway.fail["pods"] = KubernetesUnavailableError("down")
    out = services.recovery.execute(act.id, "alice")
    assert out.status == "CANCELLED" and "cannot verify the target" in out.error_message
    assert gateway.writes() == []


def test_verify_disabled_marks_succeeded_without_resolving_incident(
    gateway: FakeGateway, make_services
) -> None:
    svc = make_services(verify_after_action=False)
    act = approved_pod_action(svc, gateway)
    out = svc.recovery.execute(act.id, "alice")
    assert out.status == "SUCCEEDED" and "skipped" in out.result["verification"]
    assert (
        svc.incidents.get(act.incident_id).status == "INVESTIGATING"
    )  # unverified => not resolved


def test_verify_is_idempotent_on_finished_actions(gateway: FakeGateway, services: Services) -> None:
    act = approved_pod_action(services, gateway)
    services.recovery.execute_and_verify(act.id, "alice")
    again = services.recovery.verify(act.id)
    assert again.status == "SUCCEEDED"
    assert audit_events(services, act.id).count("RECOVERY_VERIFIED") == 1


# ------------------------------------------------------- cooldown / rate limit / loops
def failing_cycle(svc: Services, gw: FakeGateway, inc_id: str) -> RecoveryAction:
    a, _ = svc.recovery.request_action(inc_id, ActionType.DEPLOYMENT_RESTART, "alice")
    svc.recovery.approve(a.id, "alice")
    return svc.recovery.execute_and_verify(a.id, "alice")


def stuck_deployment(svc: Services, gw: FakeGateway, name: str = "web") -> Incident:
    gw.deployments = [deployment(name, replicas=3, ready=0, available=0)]
    svc.monitoring.run_cycle()
    rows, _ = svc.incidents.list_incidents(IncidentFilters(), 10, 0)
    return next(r for r in rows if r.resource_name == name)


def test_cooldown_blocks_repeat_action_on_same_target(
    gateway: FakeGateway, services: Services, clock: FakeClock
) -> None:
    inc = stuck_deployment(services, gateway)
    assert failing_cycle(services, gateway, inc.id).status == "FAILED"
    writes_after_first = len(gateway.writes())
    second = failing_cycle(services, gateway, inc.id)
    assert second.status == "CANCELLED" and "cooldown active" in second.error_message
    assert len(gateway.writes()) == writes_after_first
    clock.advance(seconds=400)  # cooldown (300s) expired
    third = failing_cycle(services, gateway, inc.id)
    assert third.status == "FAILED" and len(gateway.writes()) == writes_after_first + 1


def test_rate_limit_applies_across_targets(gateway: FakeGateway, make_services) -> None:
    svc = make_services(max_actions_per_hour=1, cooldown_seconds=0)
    gateway.deployments = [
        deployment("a", replicas=2, ready=0, available=0),
        deployment("b", replicas=2, ready=0, available=0),
    ]
    gateway.on_restart = lambda gw, ns, n: healthy_rollout(gw, ns, n)
    svc.monitoring.run_cycle()
    rows, _ = svc.incidents.list_incidents(IncidentFilters(), 10, 0)
    first, second = sorted(rows, key=lambda r: r.resource_name)
    assert failing_cycle(svc, gateway, first.id).status == "SUCCEEDED"
    out = failing_cycle(svc, gateway, second.id)
    assert out.status == "CANCELLED" and "rate limit" in out.error_message
    assert len(gateway.writes()) == 1


def test_failed_actions_are_not_retried_forever(
    gateway: FakeGateway, make_services, clock: FakeClock
) -> None:
    svc = make_services(cooldown_seconds=0, max_actions_per_hour=100, max_consecutive_failures=2)
    inc = stuck_deployment(svc, gateway)
    assert failing_cycle(svc, gateway, inc.id).status == "FAILED"
    assert failing_cycle(svc, gateway, inc.id).status == "FAILED"
    third = failing_cycle(svc, gateway, inc.id)
    assert third.status == "CANCELLED" and "consecutive failed" in third.error_message
    assert len(gateway.writes()) == 2


# ----------------------------------------------------------- auto path (no approval)
def test_no_approval_mode_with_dry_run_default_only_dry_runs(
    gateway: FakeGateway, make_services
) -> None:
    svc = make_services(require_approval=False, dry_run=True)
    crash_incident(svc, gateway)
    [a], _ = svc.recovery.list_actions(status=None, incident_id=None, limit=5, offset=0)
    assert a.status == "DRY_RUN" and a.approved_by == "policy:auto"
    assert gateway.writes() == []


def test_no_approval_mode_executes_automatically_when_not_dry_run(
    gateway: FakeGateway, make_services
) -> None:
    svc = make_services(require_approval=False, verify_after_action=False)
    crash_incident(svc, gateway)
    [a], _ = svc.recovery.list_actions(status=None, incident_id=None, limit=5, offset=0)
    assert a.status == "SUCCEEDED" and gateway.writes()[0][0] == "delete_pod"


def test_only_suggested_problem_types_get_proposals(
    gateway: FakeGateway, services: Services
) -> None:
    gateway.pods = [
        pod(
            "oom",
            containers=[
                container(state="terminated", reason="OOMKilled", ready=False, exit_code=137)
            ],
        )
    ]
    services.monitoring.run_cycle()
    assert (
        services.recovery.list_actions(status=None, incident_id=None, limit=5, offset=0)[1] == 0
    )  # OOM: humans decide


# ------------------------------------------------------------------ concurrency
def test_concurrent_execute_runs_exactly_once(
    gateway: FakeGateway, make_services, tmp_path
) -> None:
    svc = make_services(database_url=f"sqlite:///{tmp_path / 'race.db'}")
    from kubidoor.database.base import Base

    Base.metadata.create_all(svc.db_engine)
    act = approved_pod_action(svc, gateway)

    barrier, results = threading.Barrier(8), []

    def worker() -> None:
        barrier.wait()
        try:
            results.append(svc.recovery.execute(act.id, "alice").status)
        except ConflictError:
            results.append("CONFLICT")

    threads = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count("RUNNING") == 1 and results.count("CONFLICT") == 7
    assert len(gateway.writes()) == 1


def test_concurrent_approve_has_a_single_winner(
    gateway: FakeGateway, make_services, tmp_path
) -> None:
    svc = make_services(database_url=f"sqlite:///{tmp_path / 'approve.db'}")
    from kubidoor.database.base import Base

    Base.metadata.create_all(svc.db_engine)
    inc = crash_incident(svc, gateway)
    [a], _ = svc.recovery.list_actions(status=None, incident_id=inc.id, limit=5, offset=0)
    barrier, results = threading.Barrier(6), []

    def worker(i: int) -> None:
        barrier.wait()
        try:
            svc.recovery.approve(a.id, f"user{i}")
            results.append("OK")
        except ConflictError:
            results.append("CONFLICT")

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert results.count("OK") == 1 and results.count("CONFLICT") == 5
    assert audit_events(svc, a.id).count("RECOVERY_APPROVED") == 1


def test_repeated_execute_after_completion_never_reruns(
    gateway: FakeGateway, services: Services
) -> None:
    act = approved_pod_action(services, gateway)
    services.recovery.execute_and_verify(act.id, "alice")
    for _ in range(3):
        with pytest.raises(ConflictError):
            services.recovery.execute(act.id, "alice")
    assert len(gateway.writes()) == 1


def test_interrupted_running_actions_are_failed_on_startup(
    gateway: FakeGateway, services: Services
) -> None:
    act = approved_pod_action(services, gateway)
    services.recovery.execute(act.id, "alice")  # RUNNING, never verified (process "dies")
    assert services.recovery.fail_interrupted() == 1
    out = services.recovery.get_action(act.id)
    assert out.status == "FAILED" and "interrupted" in out.error_message
    assert services.recovery.fail_interrupted() == 0


def test_secrets_in_error_messages_are_redacted(gateway: FakeGateway, services: Services) -> None:
    gateway.write_error = KubernetesUnavailableError("denied token=abc123secret")
    act = approved_pod_action(services, gateway)
    out = services.recovery.execute(act.id, "alice")
    assert "abc123secret" not in out.error_message


def test_background_verification_is_tracked_and_joined_on_shutdown(
    gateway: FakeGateway, services: Services
) -> None:
    act = approved_pod_action(services, gateway)
    assert services.recovery.execute(act.id, "alice").status == "RUNNING"
    services.recovery.start_verification(act.id)
    assert services.recovery.shutdown(timeout=10) == 0  # nothing left running
    assert services.recovery.get_action(act.id).status == "SUCCEEDED"
    assert services.recovery._threads == set()


def test_crash_in_background_verification_is_logged_not_lost(
    gateway: FakeGateway, services: Services, caplog
) -> None:
    act = approved_pod_action(services, gateway)
    services.recovery.execute(act.id, "alice")
    services.recovery.verify = lambda _id: (_ for _ in ()).throw(RuntimeError("boom"))  # type: ignore[method-assign]
    with caplog.at_level("ERROR"):
        services.recovery.start_verification(act.id)
        services.recovery.shutdown(timeout=10)
    assert "background verification crashed" in caplog.text
    assert services.recovery._threads == set()
