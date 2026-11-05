"""Parse kubernetes.client V1Pod objects into PodInfo."""

from typing import Any

from kubidoor.core.clock import ensure_utc
from kubidoor.kubernetes.models import Condition, ContainerInfo, PodInfo


def parse_conditions(raw: list[Any] | None) -> tuple[Condition, ...]:
    return tuple(
        Condition(
            type=c.type,
            status=c.status,
            reason=getattr(c, "reason", None),
            message=getattr(c, "message", None),
            last_transition=ensure_utc(getattr(c, "last_transition_time", None)),
            last_update=ensure_utc(
                getattr(c, "last_update_time", None) or getattr(c, "last_heartbeat_time", None)
            ),
        )
        for c in (raw or [])
    )


def _state(status_state: Any) -> tuple[str, str | None, str | None, int | None]:
    if status_state is None:
        return "unknown", None, None, None
    if status_state.running is not None:
        return "running", None, None, None
    if status_state.waiting is not None:
        w = status_state.waiting
        return "waiting", w.reason, w.message, None
    if status_state.terminated is not None:
        t = status_state.terminated
        return "terminated", t.reason, t.message, t.exit_code
    return "unknown", None, None, None


def _resources(spec_container: Any) -> tuple[dict[str, str], dict[str, str]]:
    res = getattr(spec_container, "resources", None)
    if res is None:
        return {}, {}
    return dict(res.requests or {}), dict(res.limits or {})


def _container(spec_container: Any, status: Any, *, is_init: bool) -> ContainerInfo:
    requests, limits = _resources(spec_container)
    if status is None:
        # Not yet reported by the kubelet (e.g. unscheduled Pending pod).
        return ContainerInfo(
            name=spec_container.name,
            image=spec_container.image or "",
            ready=False,
            restart_count=0,
            state="waiting",
            requests=requests,
            limits=limits,
            is_init=is_init,
        )
    state, reason, message, exit_code = _state(status.state)
    last = status.last_state.terminated if status.last_state else None
    return ContainerInfo(
        name=status.name,
        image=status.image or spec_container.image or "",
        ready=bool(status.ready),
        restart_count=int(status.restart_count or 0),
        state=state,
        state_reason=reason,
        state_message=message,
        exit_code=exit_code,
        last_reason=last.reason if last else None,
        last_exit_code=last.exit_code if last else None,
        last_finished_at=ensure_utc(last.finished_at) if last else None,
        requests=requests,
        limits=limits,
        is_init=is_init,
    )


def parse_pod(pod: Any) -> PodInfo:
    meta, spec, status = pod.metadata, pod.spec, pod.status
    statuses = {s.name: s for s in (status.container_statuses or [])}
    init_statuses = {s.name: s for s in (status.init_container_statuses or [])}
    containers = [
        _container(c, statuses.get(c.name), is_init=False) for c in (spec.containers or [])
    ] + [
        _container(c, init_statuses.get(c.name), is_init=True) for c in (spec.init_containers or [])
    ]
    owner = next((o for o in (meta.owner_references or []) if o.controller), None)
    return PodInfo(
        namespace=meta.namespace,
        name=meta.name,
        uid=meta.uid,
        phase=status.phase or "Unknown",
        node_name=spec.node_name,
        created_at=ensure_utc(meta.creation_timestamp),
        reason=status.reason,
        message=status.message,
        deletion_timestamp=ensure_utc(meta.deletion_timestamp),
        labels=dict(meta.labels or {}),
        owner_kind=owner.kind if owner else None,
        owner_name=owner.name if owner else None,
        owner_uid=owner.uid if owner else None,
        containers=tuple(containers),
        conditions=parse_conditions(status.conditions),
        node_selector=dict(spec.node_selector or {}),
        tolerations=tuple(
            {k: str(v) for k, v in (t.to_dict() if hasattr(t, "to_dict") else t).items() if v}
            for t in (spec.tolerations or [])
        ),
        image_pull_secrets=tuple(s.name for s in (spec.image_pull_secrets or [])),
    )
