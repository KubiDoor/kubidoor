"""Parse core/v1 Events and select the ones relevant to a resource."""

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from kubidoor.core.clock import ensure_utc
from kubidoor.kubernetes.models import EventInfo

_EPOCH = datetime.min.replace(tzinfo=UTC)


def parse_event(ev: Any) -> EventInfo:
    obj = ev.involved_object
    last = ensure_utc(ev.last_timestamp or ev.event_time or ev.first_timestamp)
    return EventInfo(
        namespace=obj.namespace,
        kind=obj.kind or "",
        name=obj.name or "",
        uid=obj.uid,
        type=ev.type or "Normal",
        reason=ev.reason or "",
        message=ev.message or "",
        count=int(ev.count or 1),
        first_at=ensure_utc(ev.first_timestamp),
        last_at=last,
    )


def events_for(
    events: Iterable[EventInfo], kind: str, namespace: str | None, name: str, limit: int = 10
) -> list[EventInfo]:
    """Most recent events about one object, newest first."""
    matching = [
        e
        for e in events
        if e.kind == kind and e.name == name and (e.namespace or None) == namespace
    ]
    matching.sort(key=lambda e: e.last_at or _EPOCH, reverse=True)
    return matching[:limit]
