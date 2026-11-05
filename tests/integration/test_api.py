"""REST API: every endpoint against real services with a fake cluster."""

import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from kubidoor.container import Services
from kubidoor.core.exceptions import KubernetesUnavailableError
from kubidoor.database.base import Base
from kubidoor.main import create_app
from tests.fakes import FakeGateway, container, crashloop_pod, deployment, node, pod, pvc

pytestmark = pytest.mark.integration

ACTOR = {"X-KubiDoor-Actor": "alice"}


@pytest.fixture
def svc(make_services, tmp_path) -> Services:  # type: ignore[no-untyped-def]
    s = make_services(database_url=f"sqlite:///{tmp_path / 'api.db'}")
    Base.metadata.create_all(s.db_engine)
    return s


@pytest.fixture
def api(svc: Services) -> Iterator[TestClient]:
    with TestClient(create_app(svc.settings, svc)) as c:
        yield c


def seed_crash(svc: Services, gw: FakeGateway) -> str:
    gw.pods = [crashloop_pod()]
    svc.monitoring.run_cycle()
    rows, _ = svc.incidents.list_incidents(
        __import__("kubidoor.incidents.models", fromlist=["x"]).IncidentFilters(), 5, 0
    )
    return rows[0].id


def wait_for(api: TestClient, action_id: str, status: str, timeout: float = 10) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = api.get(f"/api/v1/recovery/actions/{action_id}").json()
        if body["status"] == status:
            return body
        time.sleep(0.02)
    raise AssertionError(f"action never reached {status}: {body}")


# ------------------------------------------------------------------------- health
def test_health_and_ready(api: TestClient, gateway: FakeGateway) -> None:
    assert api.get("/health").json() == {"status": "ok"}
    r = api.get("/ready")
    assert r.status_code == 200
    assert r.json()["checks"] == {"database": True, "kubernetes": True}
    assert r.json()["kubernetes_version"] == "v1.31.0"


def test_ready_is_503_when_kubernetes_unreachable(api: TestClient, gateway: FakeGateway) -> None:
    gateway.fail["ping"] = KubernetesUnavailableError("connection refused")
    r = api.get("/ready")
    assert r.status_code == 503
    assert r.json()["status"] == "not_ready" and r.json()["checks"]["kubernetes"] is False
    assert api.get("/health").status_code == 200  # liveness is independent


def test_metrics_endpoint_exposes_kubidoor_series(
    api: TestClient, svc: Services, gateway: FakeGateway
) -> None:
    seed_crash(svc, gateway)
    r = api.get("/metrics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    for name in (
        "kubidoor_monitoring_cycles_total",
        "kubidoor_monitoring_errors_total",
        "kubidoor_monitoring_cycle_duration_seconds",
        "kubidoor_active_incidents",
        "kubidoor_incidents_detected_total",
        "kubidoor_recovery_actions_total",
        "kubidoor_recovery_failures_total",
        "kubidoor_recovery_duration_seconds",
    ):
        assert name in r.text, name
    assert 'kubidoor_active_incidents{severity="HIGH"} 1.0' in r.text


def test_metrics_labels_are_low_cardinality(
    api: TestClient, svc: Services, gateway: FakeGateway
) -> None:
    iid = seed_crash(svc, gateway)
    text = api.get("/metrics").text
    for forbidden in (iid, "uid-demo-crash-1", "crash-1"):
        assert forbidden not in text


# ------------------------------------------------------------------------ cluster
def test_cluster_overview(api: TestClient, svc: Services, gateway: FakeGateway) -> None:
    seed_crash(svc, gateway)  # an incident exists; then describe the cluster for the overview
    gateway.nodes = [node("n1"), node("n2", ready="False")]
    gateway.pods = [pod("a"), crashloop_pod("b")]
    gateway.deployments = [deployment("ok"), deployment("bad", replicas=3, ready=1)]
    body = api.get("/api/v1/cluster/overview").json()
    assert body["kubernetes_version"] == "v1.31.0"
    assert body["nodes"]["data"] == {"total": 2, "ready": 1, "not_ready": 1}
    assert body["pods"]["data"]["total"] == 2 and body["pods"]["data"]["running"] == 2
    assert body["deployments"]["data"] == {"total": 2, "fully_available": 1, "degraded": 1}
    assert body["incidents"]["OPEN"] >= 1


def test_overview_reports_metrics_unavailable_instead_of_inventing_numbers(
    api: TestClient, gateway: FakeGateway
) -> None:
    gateway.nodes = [node()]
    body = api.get("/api/v1/cluster/overview").json()
    rm = body["resource_metrics"]
    assert rm["available"] is False and rm["data"] is None and "not available" in rm["error"]


def test_overview_reports_real_metrics_when_available(
    api: TestClient, gateway: FakeGateway
) -> None:
    from kubidoor.kubernetes.models import ResourceUsage

    gateway.nodes = [node()]
    gateway.node_metrics = {"node-1": ResourceUsage(250, 2**30)}
    rm = api.get("/api/v1/cluster/overview").json()["resource_metrics"]
    assert rm["available"] and rm["data"] == {
        "nodes_reporting": 1,
        "cpu_millicores_total": 250,
        "memory_bytes_total": 2**30,
    }


def test_overview_is_an_error_not_a_healthy_fake_when_cluster_down(
    api: TestClient, gateway: FakeGateway
) -> None:
    gateway.fail["ping"] = KubernetesUnavailableError("connection refused")
    r = api.get("/api/v1/cluster/overview")
    assert r.status_code == 503 and r.json()["error"]["code"] == "kubernetes_unavailable"


def test_partial_failure_in_overview_marks_only_that_section(
    api: TestClient, gateway: FakeGateway
) -> None:
    gateway.nodes = [node()]
    gateway.fail["pods"] = KubernetesUnavailableError("timeout")
    body = api.get("/api/v1/cluster/overview").json()
    assert body["pods"]["available"] is False and body["pods"]["error"]
    assert body["nodes"]["available"] is True


def test_list_nodes_pods_deployments_with_filters_and_pagination(
    api: TestClient, gateway: FakeGateway
) -> None:
    gateway.nodes = [node("n1"), node("n2")]
    gateway.pods = [pod(f"p{i}") for i in range(5)] + [crashloop_pod("bad"), pod("other", "team-x")]
    gateway.deployments = [deployment("a"), deployment("b", replicas=3, ready=0)]
    assert api.get("/api/v1/nodes").json()["total"] == 2
    page = api.get("/api/v1/pods", params={"namespace": "demo", "limit": 2, "offset": 2}).json()
    assert (page["total"], len(page["items"]), page["limit"], page["offset"]) == (6, 2, 2, 2)
    assert [
        p["name"] for p in api.get("/api/v1/pods", params={"unhealthy_only": True}).json()["items"]
    ] == ["bad"]
    assert api.get("/api/v1/pods", params={"namespace": "team-x"}).json()["total"] == 1
    assert api.get("/api/v1/pods", params={"phase": "running"}).json()["total"] == 7
    assert [
        d["name"]
        for d in api.get("/api/v1/deployments", params={"degraded_only": True}).json()["items"]
    ] == ["b"]


def test_cluster_listings_never_expose_env_secrets_or_config(
    api: TestClient, gateway: FakeGateway
) -> None:
    gateway.pods = [pod("p", image_pull_secrets=("regcred",))]
    gateway.nodes = [node()]
    text = api.get("/api/v1/pods").text + api.get("/api/v1/nodes").text
    for banned in ("regcred", "env", "token", "password", "kubeconfig", "image_pull_secrets"):
        assert banned not in text.lower(), banned


# ----------------------------------------------------------------------- incidents
def test_incident_list_get_ack_resolve_flow(
    api: TestClient, svc: Services, gateway: FakeGateway
) -> None:
    iid = seed_crash(svc, gateway)
    listing = api.get("/api/v1/incidents").json()
    assert listing["total"] == 1 and listing["items"][0]["id"] == iid
    detail = api.get(f"/api/v1/incidents/{iid}").json()
    assert detail["severity"] == "HIGH" and detail["history"][0]["event_type"] == "INCIDENT_CREATED"
    assert detail["recovery_actions"][0]["status"] == "PENDING_APPROVAL"
    assert detail["evidence"]["diagnosis"]["confidence"] in ("high", "medium", "low")

    ack = api.post(f"/api/v1/incidents/{iid}/acknowledge", headers=ACTOR, json={"note": "on it"})
    assert (
        ack.status_code == 200
        and ack.json()["status"] == "ACKNOWLEDGED"
        and ack.json()["acknowledged_by"] == "alice"
    )
    res = api.post(f"/api/v1/incidents/{iid}/resolve", headers=ACTOR)
    assert res.status_code == 200 and res.json()["status"] == "RESOLVED"
    assert api.post(f"/api/v1/incidents/{iid}/acknowledge", headers=ACTOR).status_code == 409
    events = [h["event_type"] for h in api.get(f"/api/v1/incidents/{iid}").json()["history"]]
    assert "INCIDENT_ACKNOWLEDGED" in events and "INCIDENT_RESOLVED" in events


def test_incident_filters_via_query_params(
    api: TestClient, svc: Services, gateway: FakeGateway
) -> None:
    gateway.pods = [crashloop_pod("a", "demo"), crashloop_pod("b", "team-x")]
    gateway.nodes = [node(ready="False")]
    svc.monitoring.run_cycle()
    q = lambda **p: api.get("/api/v1/incidents", params=p).json()["total"]  # noqa: E731
    assert q() == 3 and q(severity="CRITICAL") == 1 and q(namespace="team-x") == 1
    assert q(resource_type="Node") == 1 and q(status="OPEN") == 3 and q(status="RESOLVED") == 0
    assert q(limit=1) == 3


def test_incident_not_found_and_validation_errors_share_one_format(api: TestClient) -> None:
    r = api.get("/api/v1/incidents/does-not-exist")
    assert r.status_code == 404 and set(r.json()["error"]) == {"code", "message", "details"}
    assert r.json()["error"]["code"] == "not_found"
    bad = api.get("/api/v1/incidents", params={"severity": "SCARY", "limit": 0})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "validation_error"
    assert {tuple(d["loc"]) for d in bad.json()["error"]["details"]} >= {
        ("query", "severity"),
        ("query", "limit"),
    }
    assert api.get("/api/v1/nope").json()["error"]["code"] == "http_error"


def test_invalid_actor_header_rejected(
    api: TestClient, svc: Services, gateway: FakeGateway
) -> None:
    iid = seed_crash(svc, gateway)
    r = api.post(
        f"/api/v1/incidents/{iid}/acknowledge", headers={"X-KubiDoor-Actor": "bad actor!<script>"}
    )
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_actor"


# --------------------------------------------------------------------- diagnostics
def test_diagnostics_endpoint(api: TestClient, gateway: FakeGateway) -> None:
    gateway.pods = [crashloop_pod()]
    gateway.nodes = [node(ready="False"), node("n2")]
    r = api.get("/api/v1/diagnostics/pod/demo/crash-1")
    assert r.status_code == 200
    body = r.json()
    assert body["healthy"] is False and body["resource_metrics_available"] is False
    d = body["diagnoses"][0]
    assert (
        d["problem_type"] == "CrashLoopBackOff" and d["observed_facts"] and d["recommended_actions"]
    )
    assert d["confidence"] in ("high", "medium", "low")
    node_report = api.get("/api/v1/diagnostics/node/_/node-1").json()
    assert node_report["diagnoses"][0]["problem_type"] == "NodeNotReady"
    healthy = api.get("/api/v1/diagnostics/node/_/n2").json()
    assert healthy["healthy"] is True and healthy["diagnoses"] == []


def test_diagnostics_errors(api: TestClient, gateway: FakeGateway) -> None:
    assert api.get("/api/v1/diagnostics/pod/demo/ghost").status_code == 404
    assert api.get("/api/v1/diagnostics/widget/demo/x").status_code == 404
    gateway.fail["pods"] = KubernetesUnavailableError("down")
    r = api.get("/api/v1/diagnostics/pod/demo/x")
    assert r.status_code == 503 and r.json()["error"]["code"] == "kubernetes_unavailable"


def test_diagnostics_for_deployment_and_pvc(api: TestClient, gateway: FakeGateway) -> None:
    gateway.deployments = [deployment("web", replicas=2, ready=0)]
    gateway.pods = [
        pod(
            "w1",
            containers=[container(state="waiting", reason="ImagePullBackOff", ready=False)],
            phase="Pending",
        )
    ]
    gateway.pvcs = [pvc("data", phase="Pending")]
    dep = api.get("/api/v1/diagnostics/deployment/demo/web").json()
    assert dep["diagnoses"] and dep["diagnoses"][0]["problem_type"] == "ReplicasUnavailable"
    assert (
        api.get("/api/v1/diagnostics/pvc/demo/data").json()["diagnoses"][0]["problem_type"]
        == "PVCPending"
    )


# ------------------------------------------------------------------------ recovery
def test_full_recovery_flow_over_http(api: TestClient, svc: Services, gateway: FakeGateway) -> None:
    iid = seed_crash(svc, gateway)
    [action] = api.get("/api/v1/recovery/actions", params={"incident_id": iid}).json()["items"]
    aid = action["action_id"]
    assert action["status"] == "PENDING_APPROVAL" and action["dry_run"] is False

    assert (
        api.post(f"/api/v1/recovery/actions/{aid}/execute", headers=ACTOR).status_code == 409
    )  # not approved
    approved = api.post(f"/api/v1/recovery/actions/{aid}/approve", headers=ACTOR)
    assert approved.status_code == 200 and approved.json()["approved_by"] == "alice"
    assert api.post(f"/api/v1/recovery/actions/{aid}/approve", headers=ACTOR).status_code == 409

    ex = api.post(f"/api/v1/recovery/actions/{aid}/execute", headers=ACTOR)
    assert ex.status_code == 200 and ex.json()["status"] in ("RUNNING", "SUCCEEDED")
    done = wait_for(api, aid, "SUCCEEDED")
    assert done["verified_at"] is not None and done["executed_at"] is not None
    assert api.get(f"/api/v1/incidents/{iid}").json()["status"] == "RESOLVED"
    assert api.post(f"/api/v1/recovery/actions/{aid}/execute", headers=ACTOR).status_code == 409
    assert len(gateway.writes()) == 1


def test_reject_over_http(api: TestClient, svc: Services, gateway: FakeGateway) -> None:
    iid = seed_crash(svc, gateway)
    aid = api.get("/api/v1/recovery/actions", params={"incident_id": iid}).json()["items"][0][
        "action_id"
    ]
    r = api.post(
        f"/api/v1/recovery/actions/{aid}/reject", headers=ACTOR, json={"reason": "too risky"}
    )
    assert (
        r.status_code == 200
        and r.json()["status"] == "REJECTED"
        and r.json()["error_message"] == "too risky"
    )
    assert api.get("/api/v1/recovery/actions", params={"status": "REJECTED"}).json()["total"] == 1
    assert gateway.writes() == []


def test_manual_request_creates_201_then_200_and_policy_denial_is_403(
    api: TestClient, svc: Services, gateway: FakeGateway
) -> None:
    iid = seed_crash(svc, gateway)
    api.post(
        f"/api/v1/recovery/actions/{api.get('/api/v1/recovery/actions').json()['items'][0]['action_id']}/reject",
        headers=ACTOR,
    )
    first = api.post(
        "/api/v1/recovery/actions",
        headers=ACTOR,
        json={"incident_id": iid, "action_type": "pod_delete"},
    )
    again = api.post(
        "/api/v1/recovery/actions",
        headers=ACTOR,
        json={"incident_id": iid, "action_type": "pod_delete"},
    )
    assert first.status_code == 201 and again.status_code == 200
    assert (
        first.json()["action_id"] == again.json()["action_id"]
        and first.json()["requested_by"] == "alice"
    )
    denied = api.post(
        "/api/v1/recovery/actions",
        headers=ACTOR,
        json={"incident_id": iid, "action_type": "deployment_restart"},
    )
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "policy_violation"
    assert denied.json()["error"]["details"]
    bad = api.post(
        "/api/v1/recovery/actions", headers=ACTOR, json={"incident_id": iid, "action_type": "rm_rf"}
    )
    assert bad.status_code == 422


def test_recovery_disabled_returns_403_over_http(
    make_services, gateway: FakeGateway, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    s = make_services(database_url=f"sqlite:///{tmp_path / 'off.db'}", enabled=False)
    Base.metadata.create_all(s.db_engine)
    iid = seed_crash(s, gateway)
    with TestClient(create_app(s.settings, s)) as c:
        r = c.post(
            "/api/v1/recovery/actions",
            headers=ACTOR,
            json={"incident_id": iid, "action_type": "pod_delete"},
        )
        assert r.status_code == 403 and "globally disabled" in r.json()["error"]["message"]
        assert c.get("/api/v1/recovery/actions").json()["total"] == 0
    assert gateway.writes() == []


def test_recovery_unknown_ids_are_404(api: TestClient) -> None:
    for path in ("approve", "reject", "execute"):
        assert api.post(f"/api/v1/recovery/actions/nope/{path}", headers=ACTOR).status_code == 404
    assert api.get("/api/v1/recovery/actions/nope").status_code == 404


# --------------------------------------------------------------------------- auth
def test_bearer_token_protects_api_but_not_probes(svc: Services, gateway: FakeGateway) -> None:
    svc.settings.api_token = SecretStr("s3cret-token")
    with TestClient(create_app(svc.settings, svc)) as c:
        assert c.get("/health").status_code == 200 and c.get("/ready").status_code == 200
        assert c.get("/metrics").status_code == 200
        assert c.get("/api/v1/incidents").status_code == 401
        assert (
            c.get("/api/v1/incidents", headers={"Authorization": "Bearer wrong"}).status_code == 401
        )
        assert (
            c.get("/api/v1/incidents", headers={"Authorization": "Basic s3cret-token"}).status_code
            == 401
        )
        ok = c.get("/api/v1/incidents", headers={"Authorization": "Bearer s3cret-token"})
        assert ok.status_code == 200
        assert "s3cret-token" not in c.get("/api/v1/incidents").text


def test_openapi_documents_all_required_endpoints(api: TestClient) -> None:
    paths = api.get("/openapi.json").json()["paths"]
    required = [
        "/health",
        "/ready",
        "/metrics",
        "/api/v1/cluster/overview",
        "/api/v1/nodes",
        "/api/v1/pods",
        "/api/v1/deployments",
        "/api/v1/incidents",
        "/api/v1/incidents/{incident_id}",
        "/api/v1/incidents/{incident_id}/acknowledge",
        "/api/v1/incidents/{incident_id}/resolve",
        "/api/v1/diagnostics/{resource_type}/{namespace}/{resource_name}",
        "/api/v1/recovery/actions",
        "/api/v1/recovery/actions/{action_id}",
        "/api/v1/recovery/actions/{action_id}/approve",
        "/api/v1/recovery/actions/{action_id}/reject",
        "/api/v1/recovery/actions/{action_id}/execute",
    ]
    for p in required:
        assert p in paths, p


# ----------------------------------------------------------------- deploy artifacts
def test_grafana_dashboard_only_queries_metrics_the_app_really_exposes(
    api: TestClient, svc: Services, gateway: FakeGateway
) -> None:
    import json
    import re
    from pathlib import Path

    seed_crash(svc, gateway)
    exposed = api.get("/metrics").text
    dash = json.loads(Path("deploy/grafana/dashboards/kubidoor-overview.json").read_text())
    exprs = [t["expr"] for p in dash["panels"] for t in p["targets"]]
    used = {m for e in exprs for m in re.findall(r"kubidoor_[a-z_]+", e)}
    assert used, "dashboard references no kubidoor metrics"
    required = {
        "kubidoor_active_incidents",
        "kubidoor_incidents_detected_total",
        "kubidoor_monitoring_errors_total",
        "kubidoor_monitoring_cycle_duration_seconds_bucket",
        "kubidoor_recovery_actions_total",
        "kubidoor_recovery_failures_total",
        "kubidoor_recovery_duration_seconds_bucket",
    }
    assert required <= used
    for metric in used:
        base = re.sub(r"_(bucket|count|sum)$", "", metric)
        assert base in exposed, f"{metric} is not exposed by /metrics"
    assert all(p["datasource"]["uid"] == "prometheus" for p in dash["panels"])


def test_prometheus_and_grafana_provisioning_are_valid_yaml() -> None:
    from pathlib import Path

    import yaml

    prom = yaml.safe_load(Path("deploy/prometheus/prometheus.yml").read_text())
    assert prom["scrape_configs"][0]["metrics_path"] == "/metrics"
    assert prom["scrape_configs"][0]["static_configs"][0]["targets"] == ["kubidoor:8000"]
    ds = yaml.safe_load(Path("deploy/grafana/provisioning/datasources/prometheus.yml").read_text())
    assert ds["datasources"][0]["uid"] == "prometheus"
    prov = yaml.safe_load(Path("deploy/grafana/provisioning/dashboards/kubidoor.yml").read_text())
    assert prov["providers"][0]["options"]["path"] == "/var/lib/grafana/dashboards"
