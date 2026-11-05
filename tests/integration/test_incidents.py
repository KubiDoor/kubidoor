"""Monitoring engine + incident service against a real (in-memory) database."""

import pytest
from sqlalchemy import func, select

from kubidoor.container import Services
from kubidoor.core.exceptions import ConflictError, NotFoundError
from kubidoor.core.types import IncidentStatus, ResourceType, Severity
from kubidoor.database.models import AuditLog, Incident, MonitoringHistory
from kubidoor.database.session import session_scope
from kubidoor.incidents.models import IncidentFilters
from tests.fakes import FakeClock, FakeGateway, container, crashloop_pod, deployment, node, pod

pytestmark = pytest.mark.integration


@pytest.fixture
def services(make_services) -> Services:  # type: ignore[no-untyped-def]
    """Incident behaviour is tested with the product default: recovery disabled."""
    return make_services(enabled=False)


def incidents(svc: Services, **flt) -> list[Incident]:
    rows, _ = svc.incidents.list_incidents(IncidentFilters(**flt), 500, 0)
    return rows


def count(svc: Services) -> int:
    with session_scope(svc.session_factory) as s:
        return s.scalar(select(func.count()).select_from(Incident)) or 0


def test_detects_and_persists_incident_with_diagnosis(
    services: Services, gateway: FakeGateway
) -> None:
    gateway.pods = [crashloop_pod()]
    res = services.monitoring.run_cycle()
    assert res.status == "ok" and res.findings == 1 and len(res.sync.created) == 1
    [i] = incidents(services)
    assert (i.resource_type, i.namespace, i.resource_name) == ("Pod", "demo", "crash-1")
    assert i.severity == "HIGH" and i.status == "OPEN" and i.occurrence_count == 1
    assert i.resource_uid == "uid-demo-crash-1" and len(i.fingerprint) == 64
    assert "confidence" in i.probable_cause and "kubectl logs" in i.recommended_action
    assert i.evidence["diagnosis"]["observed_facts"]


def test_repeated_cycles_do_not_duplicate_incidents(
    services: Services, gateway: FakeGateway, clock: FakeClock
) -> None:
    gateway.pods = [crashloop_pod()]
    services.monitoring.run_cycle()
    first = incidents(services)[0].first_detected_at
    for _ in range(4):
        clock.advance(seconds=30)
        res = services.monitoring.run_cycle()
        assert res.sync.created == [] and len(res.sync.updated) == 1
    assert count(services) == 1
    [i] = incidents(services)
    assert i.occurrence_count == 5
    assert i.first_detected_at == first and i.last_detected_at > first


def test_same_finding_twice_in_one_cycle_is_one_incident(
    services: Services, gateway: FakeGateway
) -> None:
    gateway.pods = [crashloop_pod(), crashloop_pod()]
    services.monitoring.run_cycle()
    assert count(services) == 1


def test_incident_resolves_only_after_healthy_state_is_observed(
    services: Services, gateway: FakeGateway
) -> None:
    gateway.pods = [crashloop_pod()]
    services.monitoring.run_cycle()
    gateway.pods = [pod("crash-1")]  # healthy now
    res = services.monitoring.run_cycle()
    assert len(res.sync.resolved) == 1
    [i] = incidents(services)
    assert i.status == "RESOLVED" and i.resolved_at is not None and i.resolved_by == "monitor"


def test_deleted_resource_resolves_its_incident(services: Services, gateway: FakeGateway) -> None:
    gateway.pods = [crashloop_pod()]
    services.monitoring.run_cycle()
    gateway.pods = []
    services.monitoring.run_cycle()
    assert incidents(services)[0].status == "RESOLVED"


def test_kubernetes_outage_never_resolves_incidents(
    services: Services, gateway: FakeGateway
) -> None:
    gateway.pods = [crashloop_pod()]
    gateway.nodes = [node(ready="False")]
    gateway.deployments = [deployment(replicas=3, ready=0)]
    services.monitoring.run_cycle()
    before = count(services)
    assert before >= 2

    gateway.break_all("connection refused")
    res = services.monitoring.run_cycle()
    assert res.status == "failed" and set(res.errors) >= {"pods", "nodes", "deployments"}
    assert all(i.status != "RESOLVED" for i in incidents(services))
    assert count(services) == before


def test_partial_outage_only_affects_the_unobservable_type(
    services: Services, gateway: FakeGateway
) -> None:
    gateway.pods = [crashloop_pod()]
    gateway.nodes = [node(ready="False")]
    services.monitoring.run_cycle()
    # pods become healthy, but the *nodes* API fails this cycle (e.g. RBAC revoked)
    gateway.pods = [pod("crash-1")]
    from kubidoor.core.exceptions import KubernetesPermissionError

    gateway.fail["nodes"] = KubernetesPermissionError("forbidden")
    res = services.monitoring.run_cycle()
    assert res.status == "partial" and "nodes" in res.errors
    by_type = {i.resource_type: i.status for i in incidents(services)}
    assert by_type["Pod"] == "RESOLVED"  # observed healthy
    assert by_type["Node"] != "RESOLVED"  # not observed => untouched


def test_empty_result_from_working_api_does_resolve(
    services: Services, gateway: FakeGateway
) -> None:
    """Contrast with the outage case: an honest empty list is a real observation."""
    gateway.pods = [crashloop_pod()]
    services.monitoring.run_cycle()
    gateway.pods = []
    services.monitoring.run_cycle()
    assert incidents(services)[0].status == "RESOLVED"


def test_namespace_restriction_limits_scope(gateway: FakeGateway, make_services) -> None:
    svc = make_services(enabled=False)
    svc.settings.monitoring.namespaces = ["demo"]
    gateway.pods = [crashloop_pod("a", "demo"), crashloop_pod("b", "other")]
    svc.monitoring.run_cycle()
    assert {i.namespace for i in incidents(svc)} == {"demo"}


def test_resolved_incident_is_reopened_when_problem_returns(
    services: Services, gateway: FakeGateway
) -> None:
    gateway.pods = [crashloop_pod()]
    services.monitoring.run_cycle()
    inc_id = incidents(services)[0].id
    gateway.pods = [pod("crash-1")]
    services.monitoring.run_cycle()
    gateway.pods = [crashloop_pod()]
    res = services.monitoring.run_cycle()
    assert res.sync.reopened == [inc_id] and res.sync.created == []
    [i] = incidents(services)
    assert i.id == inc_id and i.status == "OPEN" and i.reopen_count == 1 and i.resolved_at is None
    events = [h.event_type for h in services.incidents.get_detail(inc_id)[1]]
    assert events == ["INCIDENT_CREATED", "INCIDENT_AUTO_RESOLVED", "INCIDENT_REOPENED"]


def test_pod_recreated_with_new_uid_keeps_same_incident(
    services: Services, gateway: FakeGateway
) -> None:
    gateway.pods = [crashloop_pod()]
    services.monitoring.run_cycle()
    replacement = crashloop_pod()
    from dataclasses import replace

    gateway.pods = [replace(replacement, uid="brand-new-uid")]
    services.monitoring.run_cycle()
    assert count(services) == 1
    assert incidents(services)[0].resource_uid == "brand-new-uid"


def test_acknowledge_and_manual_resolve(services: Services, gateway: FakeGateway) -> None:
    gateway.pods = [crashloop_pod()]
    services.monitoring.run_cycle()
    iid = incidents(services)[0].id
    ack = services.incidents.acknowledge(iid, "alice", "looking")
    assert ack.status == "ACKNOWLEDGED" and ack.acknowledged_by == "alice" and ack.acknowledged_at
    again = services.incidents.acknowledge(iid, "bob")  # idempotent: keeps first acknowledger
    assert again.acknowledged_by == "alice"
    res = services.incidents.resolve(iid, "alice", "fixed manually")
    assert res.status == "RESOLVED" and res.resolution_note == "fixed manually"
    with pytest.raises(ConflictError):
        services.incidents.acknowledge(iid, "alice")
    assert services.incidents.resolve(iid, "alice").status == "RESOLVED"  # idempotent
    with pytest.raises(NotFoundError):
        services.incidents.acknowledge("missing", "alice")


def test_acknowledged_incident_stays_acknowledged_while_problem_persists(
    services: Services, gateway: FakeGateway
) -> None:
    gateway.pods = [crashloop_pod()]
    services.monitoring.run_cycle()
    iid = incidents(services)[0].id
    services.incidents.acknowledge(iid, "alice")
    services.monitoring.run_cycle()
    assert services.incidents.get(iid).status == "ACKNOWLEDGED"


def test_manual_resolve_of_live_problem_reopens_next_cycle(
    services: Services, gateway: FakeGateway
) -> None:
    gateway.pods = [crashloop_pod()]
    services.monitoring.run_cycle()
    iid = incidents(services)[0].id
    services.incidents.resolve(iid, "alice")
    services.monitoring.run_cycle()
    assert services.incidents.get(iid).status == "OPEN"


def test_filtering_by_severity_namespace_type_status(
    services: Services, gateway: FakeGateway
) -> None:
    gateway.pods = [crashloop_pod("a", "demo"), crashloop_pod("b", "team-x")]
    gateway.nodes = [node(ready="False")]
    services.monitoring.run_cycle()
    assert len(incidents(services)) == 3
    assert len(incidents(services, severity=Severity.CRITICAL)) == 1
    assert len(incidents(services, namespace="team-x")) == 1
    assert len(incidents(services, resource_type=ResourceType.NODE)) == 1
    assert len(incidents(services, status=IncidentStatus.OPEN)) == 3
    assert incidents(services, status=IncidentStatus.RESOLVED) == []
    rows, total = services.incidents.list_incidents(IncidentFilters(), 2, 0)
    assert len(rows) == 2 and total == 3
    rows2, _ = services.incidents.list_incidents(IncidentFilters(), 2, 2)
    assert len(rows2) == 1


def test_monitoring_history_and_metrics_recorded(services: Services, gateway: FakeGateway) -> None:
    gateway.pods = [crashloop_pod()]
    services.monitoring.run_cycle()
    gateway.break_all()
    services.monitoring.run_cycle()
    with session_scope(services.session_factory) as s:
        rows = s.scalars(select(MonitoringHistory).order_by(MonitoringHistory.id)).all()
    assert [r.status for r in rows] == ["ok", "failed"]
    assert rows[0].incidents_created == 1 and rows[1].errors
    text = services.metrics.render().decode()
    assert "kubidoor_monitoring_cycles_total 2.0" in text
    assert 'kubidoor_monitoring_errors_total{stage="pods"} 1.0' in text
    assert 'kubidoor_incidents_detected_total{resource_type="Pod",severity="HIGH"} 1.0' in text
    assert 'kubidoor_active_incidents{severity="HIGH"} 1.0' in text
    assert "kubidoor_monitoring_cycle_duration_seconds_count 2.0" in text


def test_overlapping_cycle_is_skipped(services: Services, gateway: FakeGateway) -> None:
    assert services.monitoring._lock.acquire()
    try:
        assert services.monitoring.run_cycle().skipped
    finally:
        services.monitoring._lock.release()


def test_audit_trail_for_incident_actions(services: Services, gateway: FakeGateway) -> None:
    gateway.pods = [crashloop_pod()]
    services.monitoring.run_cycle()
    iid = incidents(services)[0].id
    services.incidents.acknowledge(iid, "alice")
    services.incidents.resolve(iid, "bob")
    with session_scope(services.session_factory) as s:
        rows = s.execute(select(AuditLog.event_type, AuditLog.actor).order_by(AuditLog.id)).all()
    assert rows == [
        ("INCIDENT_CREATED", "monitor"),
        ("INCIDENT_ACKNOWLEDGED", "alice"),
        ("INCIDENT_RESOLVED", "bob"),
    ]


def test_oom_incident_includes_real_metrics_or_states_unavailable(
    services: Services, gateway: FakeGateway
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
    facts = " ".join(incidents(services)[0].evidence["diagnosis"]["observed_facts"])
    assert (
        "unavailable" in facts
    )  # FakeGateway has no metrics-server: must say so, not invent numbers
