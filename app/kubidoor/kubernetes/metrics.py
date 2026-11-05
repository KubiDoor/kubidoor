"""Quantity parsing and metrics-server payload parsing (optional integration)."""

import re
from decimal import Decimal
from typing import Any

from kubidoor.kubernetes.models import ResourceUsage

_BINARY = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50, "Ei": 2**60}
_DECIMAL = {
    "n": Decimal("1e-9"),
    "u": Decimal("1e-6"),
    "m": Decimal("1e-3"),
    "": Decimal(1),
    "k": Decimal("1e3"),
    "M": Decimal("1e6"),
    "G": Decimal("1e9"),
    "T": Decimal("1e12"),
    "P": Decimal("1e15"),
    "E": Decimal("1e18"),
}
_QUANTITY_RE = re.compile(r"^([+-]?[0-9.]+(?:[eE][+-]?[0-9]+)?)([a-zA-Z]*)$")


def parse_quantity(value: str) -> Decimal:
    """Parse a Kubernetes quantity ("250m", "64Mi", "1Gi", "2") into a Decimal base value."""
    match = _QUANTITY_RE.match(value.strip())
    if not match:
        raise ValueError(f"invalid quantity: {value!r}")
    number, suffix = Decimal(match.group(1)), match.group(2)
    if suffix in _BINARY:
        return number * _BINARY[suffix]
    if suffix in _DECIMAL:
        return number * _DECIMAL[suffix]
    raise ValueError(f"invalid quantity suffix: {value!r}")


def parse_cpu_millicores(value: str) -> float:
    return float(parse_quantity(value) * 1000)


def parse_memory_bytes(value: str) -> int:
    return int(parse_quantity(value))


def _usage(raw: dict[str, Any]) -> ResourceUsage:
    return ResourceUsage(
        cpu_millicores=parse_cpu_millicores(str(raw.get("cpu", "0"))),
        memory_bytes=parse_memory_bytes(str(raw.get("memory", "0"))),
    )


def parse_node_metrics(payload: dict[str, Any]) -> dict[str, ResourceUsage]:
    return {item["metadata"]["name"]: _usage(item["usage"]) for item in payload.get("items", [])}


def parse_pod_metrics(
    payload: dict[str, Any],
) -> dict[tuple[str, str], dict[str, ResourceUsage]]:
    out: dict[tuple[str, str], dict[str, ResourceUsage]] = {}
    for item in payload.get("items", []):
        meta = item["metadata"]
        out[(meta["namespace"], meta["name"])] = {
            c["name"]: _usage(c["usage"]) for c in item.get("containers", [])
        }
    return out
