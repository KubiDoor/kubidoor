"""Parse V1Node, V1PersistentVolumeClaim into domain objects."""

from typing import Any

from kubidoor.core.clock import ensure_utc
from kubidoor.kubernetes.models import NodeInfo, PVCInfo
from kubidoor.kubernetes.pods import parse_conditions


def parse_node(node: Any) -> NodeInfo:
    meta, status = node.metadata, node.status
    return NodeInfo(
        name=meta.name,
        uid=meta.uid,
        created_at=ensure_utc(meta.creation_timestamp),
        conditions=parse_conditions(status.conditions),
        capacity={k: str(v) for k, v in (status.capacity or {}).items()},
        allocatable={k: str(v) for k, v in (status.allocatable or {}).items()},
        unschedulable=bool(node.spec.unschedulable) if node.spec else False,
    )


def parse_pvc(pvc: Any) -> PVCInfo:
    meta, spec, status = pvc.metadata, pvc.spec, pvc.status
    requests = (spec.resources.requests or {}) if spec and spec.resources else {}
    return PVCInfo(
        namespace=meta.namespace,
        name=meta.name,
        uid=meta.uid,
        phase=status.phase or "Pending",
        created_at=ensure_utc(meta.creation_timestamp),
        storage_class=spec.storage_class_name if spec else None,
        volume_name=spec.volume_name if spec else None,
        requested_storage=str(requests["storage"]) if "storage" in requests else None,
    )
