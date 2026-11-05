"""The REAL KubeClient against a local fake Kubernetes API server (HTTP + real kubeconfig).

Covers what the in-memory FakeGateway cannot: URL/paging behaviour, response deserialisation,
error translation, the UID-guarded delete, the restart patch body, and optional metrics.
"""

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from kubidoor.core.config import KubernetesSettings
from kubidoor.core.exceptions import (
    KubernetesConflictError,
    KubernetesPermissionError,
    KubernetesUnavailableError,
    MetricsUnavailableError,
)
from kubidoor.kubernetes.client import RESTART_ANNOTATION, KubeClient

pytestmark = pytest.mark.integration

TS = "2026-11-05T10:00:00Z"


def pod_json(name: str, ns: str = "demo", uid: str | None = None) -> dict[str, Any]:
    return {
        "metadata": {
            "name": name,
            "namespace": ns,
            "uid": uid or f"uid-{name}",
            "creationTimestamp": TS,
            "ownerReferences": [
                {
                    "apiVersion": "apps/v1",
                    "kind": "ReplicaSet",
                    "name": "rs",
                    "uid": "rs-1",
                    "controller": True,
                }
            ],
        },
        "spec": {"nodeName": "n1", "containers": [{"name": "app", "image": "nginx:1"}]},
        "status": {
            "phase": "Running",
            "conditions": [{"type": "Ready", "status": "True"}],
            "containerStatuses": [
                {
                    "name": "app",
                    "image": "nginx:1",
                    "imageID": "x",
                    "ready": True,
                    "restartCount": 0,
                    "state": {"running": {"startedAt": TS}},
                }
            ],
        },
    }


def deployment_json(name: str = "web", ns: str = "demo", uid: str = "dep-uid") -> dict[str, Any]:
    return {
        "metadata": {
            "name": name,
            "namespace": ns,
            "uid": uid,
            "generation": 2,
            "creationTimestamp": TS,
        },
        "spec": {
            "replicas": 2,
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {"labels": {"app": name}},
                "spec": {"containers": [{"name": "c", "image": "i"}]},
            },
        },
        "status": {
            "observedGeneration": 2,
            "replicas": 2,
            "readyReplicas": 2,
            "availableReplicas": 2,
            "updatedReplicas": 2,
        },
    }


class FakeApiServer:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, bytes]] = []
        self.overrides: dict[str, tuple[int, dict[str, Any]]] = {}  # path prefix -> (status, body)
        self.pod_pages = [
            [pod_json("p1"), pod_json("p2")],
            [pod_json("p3")],
        ]  # served via continue tokens
        self.deployment_uid = "dep-uid"
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # silence
                pass

            def _send(self, status: int, body: dict[str, Any]) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                url = urlparse(self.path)
                outer.requests.append((self.command, self.path, body))
                for prefix, (status, payload) in outer.overrides.items():
                    if url.path.startswith(prefix):
                        return self._send(status, payload)
                qs = parse_qs(url.query)
                path = url.path
                if path.rstrip("/") == "/version":
                    return self._send(
                        200,
                        {
                            "major": "1",
                            "minor": "31",
                            "gitVersion": "v1.31.0",
                            "gitCommit": "abc",
                            "gitTreeState": "clean",
                            "buildDate": "2026-01-01T00:00:00Z",
                            "goVersion": "go1.23",
                            "compiler": "gc",
                            "platform": "linux/amd64",
                        },
                    )
                if (
                    path in ("/api/v1/pods", "/api/v1/namespaces/demo/pods")
                    and self.command == "GET"
                ):
                    page = int(qs.get("continue", ["0"])[0])
                    nxt = str(page + 1) if page + 1 < len(outer.pod_pages) else ""
                    return self._send(
                        200,
                        {
                            "kind": "PodList",
                            "apiVersion": "v1",
                            "metadata": {"continue": nxt},
                            "items": outer.pod_pages[page],
                        },
                    )
                if path == "/api/v1/nodes":
                    return self._send(
                        200,
                        {
                            "kind": "NodeList",
                            "apiVersion": "v1",
                            "metadata": {},
                            "items": [
                                {
                                    "metadata": {
                                        "name": "n1",
                                        "uid": "n-uid",
                                        "creationTimestamp": TS,
                                    },
                                    "status": {
                                        "conditions": [{"type": "Ready", "status": "True"}],
                                        "capacity": {"cpu": "4"},
                                    },
                                }
                            ],
                        },
                    )
                if path == "/apis/apps/v1/deployments":
                    return self._send(
                        200,
                        {
                            "kind": "DeploymentList",
                            "apiVersion": "apps/v1",
                            "metadata": {},
                            "items": [deployment_json()],
                        },
                    )
                if path == "/apis/apps/v1/namespaces/demo/deployments/web":
                    if self.command == "GET":
                        return self._send(200, deployment_json(uid=outer.deployment_uid))
                    if self.command == "PATCH":
                        return self._send(200, deployment_json(uid=outer.deployment_uid))
                if path == "/api/v1/events":
                    return self._send(
                        200,
                        {
                            "kind": "EventList",
                            "apiVersion": "v1",
                            "metadata": {},
                            "items": [
                                {
                                    "metadata": {"name": "e1", "namespace": "demo"},
                                    "involvedObject": {
                                        "kind": "Pod",
                                        "name": "p1",
                                        "namespace": "demo",
                                    },
                                    "type": "Warning",
                                    "reason": "BackOff",
                                    "message": "Back-off restarting",
                                    "count": 3,
                                    "lastTimestamp": TS,
                                }
                            ],
                        },
                    )
                if path == "/api/v1/namespaces/demo/pods/p1" and self.command == "DELETE":
                    opts = json.loads(body or b"{}")
                    if opts.get("preconditions", {}).get("uid") != "uid-p1":
                        return self._send(
                            409,
                            {
                                "kind": "Status",
                                "message": "Precondition failed: UID in precondition: x, UID in object meta: uid-p1",
                                "code": 409,
                            },
                        )
                    return self._send(200, {"kind": "Pod", "apiVersion": "v1", **pod_json("p1")})
                self._send(404, {"kind": "Status", "message": f"not found: {path}", "code": 404})

            do_GET = do_PATCH = do_DELETE = _handle  # noqa: N815

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def api(tmp_path: Path) -> Iterator[FakeApiServer]:
    srv = FakeApiServer()
    srv.start()
    yield srv
    srv.stop()


def kubeconfig(tmp_path: Path, port: int) -> Path:
    path = tmp_path / "kubeconfig"
    path.write_text(f"""apiVersion: v1
kind: Config
current-context: fake
clusters: [{{name: fake, cluster: {{server: "http://127.0.0.1:{port}"}}}}]
contexts: [{{name: fake, context: {{cluster: fake, user: u}}}}]
users: [{{name: u, user: {{}}}}]
""")
    return path


@pytest.fixture
def kube(api: FakeApiServer, tmp_path: Path) -> KubeClient:
    return KubeClient(
        KubernetesSettings(
            kubeconfig=str(kubeconfig(tmp_path, api.port)),
            in_cluster=False,
            request_timeout_seconds=3,
        )
    )


def test_ping_and_nodes(kube: KubeClient) -> None:
    assert kube.ping() == "v1.31.0"
    [n] = kube.list_nodes()
    assert n.name == "n1" and n.ready_status == "True" and n.capacity == {"cpu": "4"}


def test_pods_are_paged_via_continue_tokens_and_parsed(
    kube: KubeClient, api: FakeApiServer
) -> None:
    pods = kube.list_pods()
    assert [p.name for p in pods] == ["p1", "p2", "p3"]
    assert pods[0].owner_kind == "ReplicaSet" and pods[0].containers[0].state == "running"
    gets = [r for r in api.requests if r[0] == "GET" and r[1].startswith("/api/v1/pods")]
    assert len(gets) == 2 and all("limit=500" in r[1] for r in gets) and "continue=1" in gets[1][1]


def test_namespaced_listing_uses_namespaced_endpoint(kube: KubeClient, api: FakeApiServer) -> None:
    kube.list_pods(["demo"])
    assert any(r[1].startswith("/api/v1/namespaces/demo/pods") for r in api.requests)
    assert not any(r[1].startswith("/api/v1/pods") for r in api.requests)


def test_deployments_events_and_get_missing(kube: KubeClient) -> None:
    [d] = kube.list_deployments()
    assert (d.name, d.replicas, d.available_replicas, d.generation) == ("web", 2, 2, 2)
    [e] = kube.list_events()
    assert (e.kind, e.name, e.reason, e.count) == ("Pod", "p1", "BackOff", 3)
    assert kube.get_deployment("demo", "web") is not None
    assert kube.get_pod("demo", "ghost") is None  # 404 => None, not an error


def test_pod_delete_sends_uid_precondition(kube: KubeClient, api: FakeApiServer) -> None:
    kube.delete_pod("demo", "p1", "uid-p1")
    [(method, path, body)] = [r for r in api.requests if r[0] == "DELETE"]
    assert path == "/api/v1/namespaces/demo/pods/p1"
    assert json.loads(body)["preconditions"] == {"uid": "uid-p1"}


def test_pod_delete_with_wrong_uid_is_rejected_by_server_as_conflict(kube: KubeClient) -> None:
    with pytest.raises(KubernetesConflictError):
        kube.delete_pod("demo", "p1", "some-other-uid")


def test_restart_patches_only_the_restart_annotation(kube: KubeClient, api: FakeApiServer) -> None:
    kube.restart_deployment("demo", "web", "dep-uid", "2026-11-05T10:00:00+00:00")
    [(_, path, body)] = [r for r in api.requests if r[0] == "PATCH"]
    assert path == "/apis/apps/v1/namespaces/demo/deployments/web"
    assert json.loads(body) == {
        "spec": {
            "template": {
                "metadata": {"annotations": {RESTART_ANNOTATION: "2026-11-05T10:00:00+00:00"}}
            }
        }
    }


def test_restart_refuses_when_deployment_uid_changed_and_sends_no_patch(
    kube: KubeClient, api: FakeApiServer
) -> None:
    api.deployment_uid = "recreated-uid"
    with pytest.raises(KubernetesConflictError):
        kube.restart_deployment("demo", "web", "dep-uid", "now")
    assert not [r for r in api.requests if r[0] == "PATCH"]


def test_http_errors_are_translated_with_distinct_types(
    kube: KubeClient, api: FakeApiServer
) -> None:
    api.overrides["/api/v1/nodes"] = (
        403,
        {"kind": "Status", "message": "nodes is forbidden", "code": 403},
    )
    with pytest.raises(KubernetesPermissionError):
        kube.list_nodes()
    api.overrides["/api/v1/nodes"] = (503, {"kind": "Status", "message": "overloaded", "code": 503})
    with pytest.raises(KubernetesUnavailableError):
        kube.list_nodes()


def test_connection_refused_is_unavailable_not_empty(tmp_path: Path) -> None:
    dead = KubeClient(
        KubernetesSettings(
            kubeconfig=str(kubeconfig(tmp_path, 1)), in_cluster=False, request_timeout_seconds=2
        )
    )
    with pytest.raises(KubernetesUnavailableError):
        dead.list_pods()


def test_metrics_server_missing_is_reported_never_faked(
    kube: KubeClient, api: FakeApiServer
) -> None:
    with pytest.raises(MetricsUnavailableError):  # server answers 404 for metrics.k8s.io
        kube.node_usage()
    with pytest.raises(MetricsUnavailableError):
        kube.pod_usage("demo")


def test_metrics_are_parsed_when_present(kube: KubeClient, api: FakeApiServer) -> None:
    api.overrides["/apis/metrics.k8s.io/v1beta1/nodes"] = (
        200,
        {"items": [{"metadata": {"name": "n1"}, "usage": {"cpu": "250m", "memory": "512Mi"}}]},
    )
    usage = kube.node_usage()
    assert usage["n1"].cpu_millicores == 250 and usage["n1"].memory_bytes == 512 * 2**20


def test_metrics_can_be_disabled_by_configuration(api: FakeApiServer, tmp_path: Path) -> None:
    c = KubeClient(
        KubernetesSettings(
            kubeconfig=str(kubeconfig(tmp_path, api.port)), in_cluster=False, metrics_enabled=False
        )
    )
    with pytest.raises(MetricsUnavailableError, match="disabled"):
        c.node_usage()
    assert not [r for r in api.requests if "metrics.k8s.io" in r[1]]  # not even attempted


def test_client_never_touches_secrets_api(kube: KubeClient, api: FakeApiServer) -> None:
    kube.list_pods()
    kube.list_nodes()
    kube.list_deployments()
    kube.list_events()
    kube.get_pod("demo", "p1")
    assert not [r for r in api.requests if "secrets" in r[1]]
