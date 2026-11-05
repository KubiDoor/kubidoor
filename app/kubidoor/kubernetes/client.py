"""Kubernetes integration layer.

``KubeGateway`` is the narrow interface the rest of the application depends on (and what tests
fake). ``KubeClient`` implements it with the official Python client.

Safety properties enforced here:
* No Secret is ever read; there is no code path that touches the Secrets API.
* The only writes are a Deployment rollout-restart annotation patch and a UID-guarded Pod delete.
* Every Kubernetes/transport failure is translated into an explicit ``KubernetesError``
  subclass, so callers can distinguish "API unreachable" from "resource healthy".
"""

import json
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from typing import Any, Protocol

import urllib3
from kubernetes import client, config
from kubernetes.client import ApiException
from kubernetes.config.config_exception import ConfigException

from kubidoor.core.config import KubernetesSettings
from kubidoor.core.exceptions import (
    KubernetesConflictError,
    KubernetesError,
    KubernetesNotFoundError,
    KubernetesPermissionError,
    KubernetesUnavailableError,
    MetricsUnavailableError,
)
from kubidoor.kubernetes.deployments import parse_deployment
from kubidoor.kubernetes.events import parse_event
from kubidoor.kubernetes.metrics import parse_node_metrics, parse_pod_metrics
from kubidoor.kubernetes.models import (
    DeploymentInfo,
    EventInfo,
    NodeInfo,
    PodInfo,
    PVCInfo,
    ResourceUsage,
)
from kubidoor.kubernetes.nodes import parse_node, parse_pvc
from kubidoor.kubernetes.pods import parse_pod
from kubidoor.logging.setup import get_logger, redact

logger = get_logger("kubernetes")

RESTART_ANNOTATION = "kubectl.kubernetes.io/restartedAt"
PAGE_SIZE = 500
_RECONNECT_BACKOFF_SECONDS = 5.0


class KubeGateway(Protocol):
    def ping(self) -> str: ...
    def list_nodes(self) -> list[NodeInfo]: ...
    def list_pods(self, namespaces: Sequence[str] | None = None) -> list[PodInfo]: ...
    def list_deployments(self, namespaces: Sequence[str] | None = None) -> list[DeploymentInfo]: ...
    def list_pvcs(self, namespaces: Sequence[str] | None = None) -> list[PVCInfo]: ...
    def list_events(self, namespaces: Sequence[str] | None = None) -> list[EventInfo]: ...
    def get_node(self, name: str) -> NodeInfo | None: ...
    def get_pod(self, namespace: str, name: str) -> PodInfo | None: ...
    def get_deployment(self, namespace: str, name: str) -> DeploymentInfo | None: ...
    def get_pvc(self, namespace: str, name: str) -> PVCInfo | None: ...
    def list_events_for(self, kind: str, namespace: str | None, name: str) -> list[EventInfo]: ...
    def node_usage(self) -> dict[str, ResourceUsage]: ...
    def pod_usage(
        self, namespace: str | None = None
    ) -> dict[tuple[str, str], dict[str, ResourceUsage]]: ...
    def restart_deployment(
        self, namespace: str, name: str, uid: str, restarted_at: str
    ) -> None: ...
    def delete_pod(self, namespace: str, name: str, uid: str) -> None: ...


def translate_api_exception(exc: ApiException) -> KubernetesError:
    """Map an ApiException to a KubernetesError with a short, credential-free message."""
    detail = ""
    try:
        body = json.loads(exc.body or "{}")
        detail = str(body.get("message", ""))
    except (ValueError, TypeError, AttributeError):
        pass
    message = redact(f"Kubernetes API error {exc.status} {exc.reason}: {detail}"[:300])
    if exc.status in (401, 403):
        return KubernetesPermissionError(message)
    if exc.status == 404:
        return KubernetesNotFoundError(message)
    if exc.status == 409:
        return KubernetesConflictError(message)
    if exc.status in (429, 500, 502, 503, 504):
        return KubernetesUnavailableError(message)
    return KubernetesError(message)


class KubeClient:
    """KubeGateway backed by the official client. Connects lazily and re-tries after failure."""

    def __init__(self, settings: KubernetesSettings) -> None:
        self._settings = settings
        self._lock = threading.Lock()
        self._core: client.CoreV1Api | None = None
        self._apps: client.AppsV1Api | None = None
        self._custom: client.CustomObjectsApi | None = None
        self._version: client.VersionApi | None = None
        self._last_failure = 0.0
        self._last_error = ""

    # -- connection -----------------------------------------------------------------------
    def _build_api_client(self) -> client.ApiClient:
        s = self._settings
        if s.in_cluster is True:
            cfg = client.Configuration()
            config.load_incluster_config(client_configuration=cfg)
            return client.ApiClient(cfg)
        if s.in_cluster is None:
            try:
                cfg = client.Configuration()
                config.load_incluster_config(client_configuration=cfg)
                return client.ApiClient(cfg)
            except ConfigException:
                pass
        return config.new_client_from_config(config_file=s.kubeconfig, context=s.context)

    def _connect(self) -> None:
        with self._lock:
            if self._core is not None:
                return
            if (
                time.monotonic() - self._last_failure < _RECONNECT_BACKOFF_SECONDS
                and self._last_error
            ):
                raise KubernetesUnavailableError(self._last_error)
            try:
                api_client = self._build_api_client()
            except (ConfigException, OSError, ValueError) as exc:
                self._last_failure = time.monotonic()
                self._last_error = redact(f"Cannot load Kubernetes configuration: {exc}")[:300]
                raise KubernetesUnavailableError(self._last_error) from exc
            self._core = client.CoreV1Api(api_client)
            self._apps = client.AppsV1Api(api_client)
            self._custom = client.CustomObjectsApi(api_client)
            self._version = client.VersionApi(api_client)
            self._last_error = ""

    def _call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        self._connect()
        try:
            return fn(*args, _request_timeout=self._settings.request_timeout_seconds, **kwargs)
        except ApiException as exc:
            raise translate_api_exception(exc) from exc
        except (urllib3.exceptions.HTTPError, OSError) as exc:
            raise KubernetesUnavailableError(
                redact(f"Kubernetes API unreachable: {type(exc).__name__}")
            ) from exc

    def _paged(self, fn: Callable[..., Any], **kwargs: Any) -> Iterator[Any]:
        token: str | None = None
        while True:
            extra = {"_continue": token} if token else {}
            page = self._call(fn, limit=PAGE_SIZE, **kwargs, **extra)
            yield from page.items
            token = page.metadata._continue if page.metadata else None
            if not token:
                return

    def _per_namespace(
        self,
        namespaced: Callable[..., Any],
        cluster_wide: Callable[..., Any],
        namespaces: Sequence[str] | None,
    ) -> Iterator[Any]:
        if namespaces:
            for ns in namespaces:
                yield from self._paged(namespaced, namespace=ns)
        else:
            yield from self._paged(cluster_wide)

    @property
    def _c(self) -> client.CoreV1Api:
        assert self._core is not None  # set by _connect() inside _call()
        return self._core

    # -- reads ----------------------------------------------------------------------------
    def ping(self) -> str:
        self._connect()
        assert self._version is not None
        info = self._call(self._version.get_code)
        return str(info.git_version)

    def list_nodes(self) -> list[NodeInfo]:
        self._connect()
        return [parse_node(n) for n in self._paged(self._c.list_node)]

    def list_pods(self, namespaces: Sequence[str] | None = None) -> list[PodInfo]:
        self._connect()
        items = self._per_namespace(
            self._c.list_namespaced_pod, self._c.list_pod_for_all_namespaces, namespaces
        )
        return [parse_pod(p) for p in items]

    def list_deployments(self, namespaces: Sequence[str] | None = None) -> list[DeploymentInfo]:
        self._connect()
        assert self._apps is not None
        items = self._per_namespace(
            self._apps.list_namespaced_deployment,
            self._apps.list_deployment_for_all_namespaces,
            namespaces,
        )
        return [parse_deployment(d) for d in items]

    def list_pvcs(self, namespaces: Sequence[str] | None = None) -> list[PVCInfo]:
        self._connect()
        items = self._per_namespace(
            self._c.list_namespaced_persistent_volume_claim,
            self._c.list_persistent_volume_claim_for_all_namespaces,
            namespaces,
        )
        return [parse_pvc(p) for p in items]

    def list_events(self, namespaces: Sequence[str] | None = None) -> list[EventInfo]:
        self._connect()
        items = self._per_namespace(
            self._c.list_namespaced_event, self._c.list_event_for_all_namespaces, namespaces
        )
        return [parse_event(e) for e in items]

    def _get(self, fn: Callable[..., Any], *args: Any) -> Any | None:
        try:
            return self._call(fn, *args)
        except KubernetesNotFoundError:
            return None

    def get_node(self, name: str) -> NodeInfo | None:
        self._connect()
        raw = self._get(self._c.read_node, name)
        return parse_node(raw) if raw else None

    def get_pod(self, namespace: str, name: str) -> PodInfo | None:
        self._connect()
        raw = self._get(self._c.read_namespaced_pod, name, namespace)
        return parse_pod(raw) if raw else None

    def get_deployment(self, namespace: str, name: str) -> DeploymentInfo | None:
        self._connect()
        assert self._apps is not None
        raw = self._get(self._apps.read_namespaced_deployment, name, namespace)
        return parse_deployment(raw) if raw else None

    def get_pvc(self, namespace: str, name: str) -> PVCInfo | None:
        self._connect()
        raw = self._get(self._c.read_namespaced_persistent_volume_claim, name, namespace)
        return parse_pvc(raw) if raw else None

    def list_events_for(self, kind: str, namespace: str | None, name: str) -> list[EventInfo]:
        self._connect()
        selector = f"involvedObject.kind={kind},involvedObject.name={name}"
        if namespace:
            page = self._call(
                self._c.list_namespaced_event, namespace=namespace, field_selector=selector
            )
        else:
            page = self._call(self._c.list_event_for_all_namespaces, field_selector=selector)
        return [parse_event(e) for e in page.items]

    # -- optional resource metrics (metrics-server) ---------------------------------------
    def _metrics(self, call: Callable[[], Any]) -> dict[str, Any]:
        if not self._settings.metrics_enabled:
            raise MetricsUnavailableError("Resource metrics are disabled by configuration")
        self._connect()
        try:
            result = call()
        except ApiException as exc:
            err = translate_api_exception(exc)
            raise MetricsUnavailableError(
                f"metrics.k8s.io is not available ({err.message})"
            ) from exc
        except (urllib3.exceptions.HTTPError, OSError) as exc:
            raise MetricsUnavailableError("metrics.k8s.io is unreachable") from exc
        return dict(result)

    def node_usage(self) -> dict[str, ResourceUsage]:
        self._connect()
        custom = self._custom
        assert custom is not None
        payload = self._metrics(
            lambda: custom.list_cluster_custom_object(
                "metrics.k8s.io",
                "v1beta1",
                "nodes",
                _request_timeout=self._settings.request_timeout_seconds,
            )
        )
        return parse_node_metrics(payload)

    def pod_usage(
        self, namespace: str | None = None
    ) -> dict[tuple[str, str], dict[str, ResourceUsage]]:
        self._connect()
        custom = self._custom
        assert custom is not None
        timeout = self._settings.request_timeout_seconds
        if namespace:
            payload = self._metrics(
                lambda: custom.list_namespaced_custom_object(
                    "metrics.k8s.io", "v1beta1", namespace, "pods", _request_timeout=timeout
                )
            )
        else:
            payload = self._metrics(
                lambda: custom.list_cluster_custom_object(
                    "metrics.k8s.io", "v1beta1", "pods", _request_timeout=timeout
                )
            )
        return parse_pod_metrics(payload)

    # -- the only two writes --------------------------------------------------------------
    def restart_deployment(self, namespace: str, name: str, uid: str, restarted_at: str) -> None:
        """Equivalent of ``kubectl rollout restart``: bump a pod-template annotation."""
        self._connect()
        assert self._apps is not None
        current = self._call(self._apps.read_namespaced_deployment, name, namespace)
        if current.metadata.uid != uid:
            raise KubernetesConflictError(
                f"Deployment {namespace}/{name} UID changed; refusing to restart a different object"
            )
        body = {
            "spec": {"template": {"metadata": {"annotations": {RESTART_ANNOTATION: restarted_at}}}}
        }
        self._call(self._apps.patch_namespaced_deployment, name, namespace, body)

    def delete_pod(self, namespace: str, name: str, uid: str) -> None:
        """Delete exactly one Pod; the API server rejects the call if the UID does not match."""
        self._connect()
        options = client.V1DeleteOptions(preconditions=client.V1Preconditions(uid=uid))
        self._call(self._c.delete_namespaced_pod, name, namespace, body=options)
