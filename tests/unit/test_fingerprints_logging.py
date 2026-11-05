"""Fingerprint determinism and log redaction."""

import json
import logging

from kubidoor.core.types import ResourceType
from kubidoor.incidents.fingerprints import compute_fingerprint
from kubidoor.logging.setup import JsonFormatter, log_context, redact


def test_fingerprint_is_deterministic_and_hex() -> None:
    a = compute_fingerprint(ResourceType.POD, "demo", "p", "CrashLoopBackOff", "app")
    b = compute_fingerprint(ResourceType.POD, "demo", "p", "CrashLoopBackOff", "app")
    assert a == b and len(a) == 64 and int(a, 16) >= 0


def test_fingerprint_distinguishes_identity_fields() -> None:
    base = ("demo", "p", "CrashLoopBackOff", "app")
    fp = compute_fingerprint(ResourceType.POD, *base)
    assert fp != compute_fingerprint(ResourceType.POD, "other", *base[1:])
    assert fp != compute_fingerprint(ResourceType.POD, "demo", "q", *base[2:])
    assert fp != compute_fingerprint(ResourceType.POD, "demo", "p", "OOMKilled", "app")
    assert fp != compute_fingerprint(ResourceType.POD, "demo", "p", "CrashLoopBackOff", "sidecar")
    assert fp != compute_fingerprint(ResourceType.DEPLOYMENT, *base)


def test_fingerprint_field_boundaries_cannot_collide() -> None:
    a = compute_fingerprint(ResourceType.POD, "ab", "c", "X")
    b = compute_fingerprint(ResourceType.POD, "a", "bc", "X")
    assert a != b


def test_redaction_masks_credentials() -> None:
    assert "s3cr3t" not in redact("password=s3cr3t")
    assert "abcdef123456" not in redact("Authorization: Bearer abcdef123456")
    assert "tok123" not in redact('{"token": "tok123"}')
    assert "pw" not in redact("postgresql://user:pw@db:5432/x")
    assert "MIIBVQ" not in redact(
        "-----BEGIN RSA PRIVATE KEY-----\nMIIBVQ\n-----END RSA PRIVATE KEY-----"
    )
    assert redact("pod web-1 crashed") == "pod web-1 crashed"


def test_json_formatter_emits_required_fields_and_redacts() -> None:
    rec = logging.LogRecord(
        "kubidoor.recovery", logging.INFO, "f", 1, "api_key=zzz999 done", None, None
    )
    for k, v in log_context(
        incident_id="i1",
        action_id="a1",
        resource_type="Pod",
        resource_name="p",
        namespace="demo",
        bogus="x",
    ).items():
        setattr(rec, k, v)
    out = json.loads(JsonFormatter().format(rec))
    assert out["component"] == "recovery" and out["level"] == "INFO"
    assert out["incident_id"] == "i1" and out["action_id"] == "a1" and out["namespace"] == "demo"
    assert "zzz999" not in out["message"] and "timestamp" in out and "bogus" not in out
