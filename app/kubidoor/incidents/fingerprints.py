"""Deterministic incident fingerprints."""

import hashlib

from kubidoor.core.types import ResourceType


def compute_fingerprint(
    resource_type: ResourceType | str,
    namespace: str | None,
    resource_name: str,
    problem_type: str,
    container: str | None = None,
) -> str:
    """SHA-256 over the identity of a problem.

    The resource UID is intentionally *not* part of the fingerprint: when a controller recreates
    an object under the same name (StatefulSet pod, a re-created Deployment) the *same* problem
    returning must reopen the existing incident rather than open a duplicate. Container name is
    included so two containers failing in one pod are tracked separately.
    """
    parts = [str(resource_type), namespace or "", resource_name, problem_type, container or ""]
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()
