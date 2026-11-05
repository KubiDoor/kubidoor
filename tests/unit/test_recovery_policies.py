"""Recovery policy rules (pure functions) and result verification."""

from datetime import timedelta

import pytest

from kubidoor.core.config import RecoverySettings
from kubidoor.core.exceptions import KubernetesUnavailableError
from kubidoor.core.types import ActionType, ResourceType
from kubidoor.kubernetes.models import Condition
from kubidoor.recovery import policies, verification
from kubidoor.recovery.verification import Outcome
from tests.fakes import T0, deployment, pod

R = ResourceType
A = ActionType


def cfg(**kw) -> RecoverySettings:
    return RecoverySettings(**{"enabled": True, **kw})


def test_defaults_are_safe() -> None:
    d = RecoverySettings()
    assert d.enabled is False and d.dry_run is True and d.require_approval is True
    assert d.allowed_namespaces == ["demo"] and d.blocked_namespaces == ["kube-system"]
    assert set(d.allowed_actions) == {A.DEPLOYMENT_RESTART, A.POD_DELETE}
    assert d.cooldown_seconds == 300 and d.max_actions_per_hour == 5 and d.verify_after_action


def test_everything_denied_when_globally_disabled() -> None:
    v = policies.evaluate_static(RecoverySettings(), A.POD_DELETE, R.POD, "demo")
    assert any("globally disabled" in x for x in v)


def test_allowed_request_has_no_violations() -> None:
    assert policies.evaluate_static(cfg(), A.POD_DELETE, R.POD, "demo") == []
    assert policies.evaluate_static(cfg(), A.DEPLOYMENT_RESTART, R.DEPLOYMENT, "demo") == []


def test_action_must_be_allowlisted() -> None:
    v = policies.evaluate_static(
        cfg(allowed_actions=[A.DEPLOYMENT_RESTART]), A.POD_DELETE, R.POD, "demo"
    )
    assert any("allowed_actions" in x for x in v)


@pytest.mark.parametrize(
    "action,rtype",
    [
        (A.POD_DELETE, R.DEPLOYMENT),
        (A.POD_DELETE, R.NODE),
        (A.POD_DELETE, R.PVC),
        (A.DEPLOYMENT_RESTART, R.POD),
        (A.DEPLOYMENT_RESTART, R.NODE),
        (A.DEPLOYMENT_RESTART, R.PVC),
    ],
)
def test_actions_can_never_target_inappropriate_resources(action: A, rtype: R) -> None:
    v = policies.evaluate_static(cfg(allowed_namespaces=["demo"]), action, rtype, "demo")
    assert any("not appropriate" in x for x in v)


def test_namespace_rules() -> None:
    assert any(
        "not explicitly permitted" in x
        for x in policies.evaluate_static(cfg(), A.POD_DELETE, R.POD, "prod")
    )
    assert any(
        "no namespace" in x for x in policies.evaluate_static(cfg(), A.POD_DELETE, R.POD, None)
    )
    # kube-system is blocked even if someone allowlists it as well
    both = cfg(allowed_namespaces=["kube-system"], blocked_namespaces=["kube-system"])
    assert any(
        "blocked" in x for x in policies.evaluate_static(both, A.POD_DELETE, R.POD, "kube-system")
    )
    # and blocked by default without any explicit configuration
    assert any(
        "blocked" in x
        for x in policies.evaluate_static(
            RecoverySettings(enabled=True, allowed_namespaces=["kube-system"]),
            A.POD_DELETE,
            R.POD,
            "kube-system",
        )
    )


def test_empty_allowlist_means_nothing_is_permitted() -> None:
    v = policies.evaluate_static(cfg(allowed_namespaces=[]), A.POD_DELETE, R.POD, "demo")
    assert any("not explicitly permitted" in x for x in v)


def test_approval_requirement() -> None:
    assert policies.evaluate_approval(cfg(require_approval=True), None)
    assert policies.evaluate_approval(cfg(require_approval=True), "alice") == []
    assert policies.evaluate_approval(cfg(require_approval=False), None) == []


def test_cooldown_enforced_and_expires() -> None:
    c = cfg(cooldown_seconds=300)
    last = T0 - timedelta(seconds=100)
    v = policies.evaluate_limits(c, T0, last, 1, 0)
    assert any("cooldown" in x and "200s" in x for x in v)
    assert policies.evaluate_limits(c, T0 + timedelta(seconds=201), last, 1, 0) == []
    assert policies.evaluate_limits(c, T0, None, 0, 0) == []


def test_rate_limit() -> None:
    c = cfg(max_actions_per_hour=2)
    assert policies.evaluate_limits(c, T0, None, 1, 0) == []
    assert any("rate limit" in x for x in policies.evaluate_limits(c, T0, None, 2, 0))
    assert any(
        "rate limit" in x
        for x in policies.evaluate_limits(cfg(max_actions_per_hour=0), T0, None, 0, 0)
    )


def test_failed_action_loop_prevention() -> None:
    c = cfg(max_consecutive_failures=3)
    assert policies.evaluate_limits(c, T0, None, 0, 2) == []
    assert any("consecutive failed" in x for x in policies.evaluate_limits(c, T0, None, 0, 3))


def test_pod_target_checks() -> None:
    p = pod(uid="u1")
    assert policies.evaluate_pod_target(p, "u1") == []
    assert any("UID mismatch" in x for x in policies.evaluate_pod_target(p, "other"))
    assert any("UID mismatch" in x for x in policies.evaluate_pod_target(p, None))
    assert any("no longer exists" in x for x in policies.evaluate_pod_target(None, "u1"))
    assert any(
        "terminating" in x for x in policies.evaluate_pod_target(pod(uid="u1", deletion=T0), "u1")
    )


def test_bare_pod_without_controller_is_never_deleted() -> None:
    v = policies.evaluate_pod_target(pod(uid="u1", owner_kind=None), "u1")
    assert any("no controller" in x for x in v)
    assert any(
        "no controller" in x
        for x in policies.evaluate_pod_target(pod(uid="u1", owner_kind="Node"), "u1")
    )


def test_deployment_target_checks() -> None:
    d = deployment(uid="d1")
    assert policies.evaluate_deployment_target(d, "d1") == []
    assert any("UID mismatch" in x for x in policies.evaluate_deployment_target(d, "x"))
    assert any("no longer exists" in x for x in policies.evaluate_deployment_target(None, "d1"))
    assert any(
        "zero" in x
        for x in policies.evaluate_deployment_target(deployment(uid="d1", replicas=0), "d1")
    )
    assert any(
        "paused" in x
        for x in policies.evaluate_deployment_target(deployment(uid="d1", paused=True), "d1")
    )


def test_no_action_type_can_target_forbidden_resources() -> None:
    forbidden = {R.NODE, R.PVC}
    assert forbidden.isdisjoint(policies.ACTION_TARGETS.values())
    assert {a.value for a in ActionType} == {
        "deployment_restart",
        "pod_delete",
    }  # no shell/other actions


# ------------------------------------------------------------------- verification
def test_deployment_rollout_verdicts() -> None:
    uid = "d1"
    ok = deployment(uid=uid, generation=2, observed=2)
    assert verification.judge_deployment_rollout(ok, uid, 1).verdict == "success"
    # controller hasn't seen the restart yet
    assert (
        verification.judge_deployment_rollout(
            deployment(uid=uid, generation=2, observed=1), uid, 1
        ).verdict
        == "pending"
    )
    # an API 200 with the old generation observed is NOT success
    assert (
        verification.judge_deployment_rollout(
            deployment(uid=uid, generation=1, observed=1), uid, 1
        ).verdict
        == "pending"
    )
    partial = deployment(uid=uid, generation=2, observed=2, replicas=3, ready=1, updated=2)
    assert verification.judge_deployment_rollout(partial, uid, 1).verdict == "pending"
    failed = deployment(
        uid=uid,
        generation=2,
        observed=2,
        replicas=3,
        ready=1,
        conditions=[Condition("Progressing", "False", "ProgressDeadlineExceeded", "x", T0, T0)],
    )
    assert verification.judge_deployment_rollout(failed, uid, 1).verdict == "failed"
    assert verification.judge_deployment_rollout(None, uid, 1).verdict == "failed"
    assert (
        verification.judge_deployment_rollout(deployment(uid="other", generation=2), uid, 1).verdict
        == "failed"
    )


def test_pod_replacement_verdicts() -> None:
    old = pod("a", uid="old", owner_uid="rs")
    assert verification.judge_pod_replacement([old], "old", "rs").verdict == "pending"
    assert verification.judge_pod_replacement([], "old", "rs").verdict == "pending"
    starting = pod("b", uid="new", owner_uid="rs", phase="Pending", ready=False)
    assert verification.judge_pod_replacement([starting], "old", "rs").verdict == "pending"
    healthy = pod("b", uid="new", owner_uid="rs")
    assert verification.judge_pod_replacement([healthy], "old", "rs").verdict == "success"
    # a ready pod owned by someone else is not "the replacement"
    stranger = pod("c", uid="z", owner_uid="other-rs")
    assert verification.judge_pod_replacement([stranger], "old", "rs").verdict == "pending"


class Ticker:
    def __init__(self) -> None:
        self.t = 0.0

    def mono(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


def test_wait_for_outcome_polls_until_success() -> None:
    tk, seq = (
        Ticker(),
        iter([Outcome("pending", "a"), Outcome("pending", "b"), Outcome("success", "done")]),
    )
    out = verification.wait_for_outcome(lambda: next(seq), 60, 5, monotonic=tk.mono, sleep=tk.sleep)
    assert out.verdict == "success" and tk.t == 10


def test_wait_for_outcome_times_out_as_failure() -> None:
    tk = Ticker()
    out = verification.wait_for_outcome(
        lambda: Outcome("pending", "still rolling"), 30, 10, monotonic=tk.mono, sleep=tk.sleep
    )
    assert out.verdict == "failed" and "timed out" in out.detail and "still rolling" in out.detail


def test_wait_for_outcome_survives_transient_api_errors() -> None:
    tk, calls = Ticker(), {"n": 0}

    def check() -> Outcome:
        calls["n"] += 1
        if calls["n"] < 3:
            raise KubernetesUnavailableError("api down")
        return Outcome("success", "ok")

    assert (
        verification.wait_for_outcome(check, 60, 1, monotonic=tk.mono, sleep=tk.sleep).verdict
        == "success"
    )
