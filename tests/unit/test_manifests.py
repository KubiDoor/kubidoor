"""Static guarantees about the shipped Kubernetes manifests (least-privilege RBAC etc.)."""

from pathlib import Path

import yaml

K8S = Path("deploy/kubernetes")
WRITE_VERBS = {
    "create",
    "update",
    "patch",
    "delete",
    "deletecollection",
    "*",
    "escalate",
    "bind",
    "impersonate",
}


def docs(name: str) -> list[dict]:
    return [d for d in yaml.safe_load_all((K8S / name).read_text()) if d]


def rules(name: str, kind: str) -> list[dict]:
    return [r for d in docs(name) if d["kind"] == kind for r in d["rules"]]


def test_monitoring_role_is_strictly_read_only() -> None:
    rs = rules("rbac.yaml", "ClusterRole")
    assert rs
    for rule in rs:
        assert set(rule["verbs"]) <= {"get", "list", "watch"}, rule
        assert "*" not in rule["resources"] and "*" not in rule["apiGroups"]


def test_no_manifest_grants_secrets_rbac_or_cluster_admin() -> None:
    forbidden_resources = {
        "secrets",
        "roles",
        "rolebindings",
        "clusterroles",
        "clusterrolebindings",
        "pods/exec",
        "serviceaccounts/token",
        "nodes/proxy",
    }
    for f in K8S.glob("*.yaml"):
        for d in yaml.safe_load_all(f.read_text()):
            if not d:
                continue
            for rule in d.get("rules", []):
                assert not forbidden_resources & set(rule["resources"]), (f, rule)
            if d["kind"] in ("ClusterRoleBinding", "RoleBinding"):
                assert d["roleRef"]["name"] != "cluster-admin", f


def test_monitoring_has_no_write_access_to_nodes_pvs_or_pvcs() -> None:
    for rule in rules("rbac.yaml", "ClusterRole"):
        assert not (set(rule["verbs"]) & WRITE_VERBS)


def test_recovery_role_is_opt_in_namespaced_and_minimal() -> None:
    assert "rbac-recovery.yaml" not in (K8S / "kustomization.yaml").read_text().replace(
        "# rbac-recovery.yaml", ""
    )
    kinds = {d["kind"] for d in docs("rbac-recovery.yaml")}
    assert kinds == {"Role", "RoleBinding"}  # namespaced only, never a ClusterRole
    granted = {
        (r["apiGroups"][0], res, v)
        for r in rules("rbac-recovery.yaml", "Role")
        for res in r["resources"]
        for v in r["verbs"]
    }
    assert granted == {
        ("apps", "deployments", "get"),
        ("apps", "deployments", "patch"),
        ("", "pods", "get"),
        ("", "pods", "list"),
        ("", "pods", "delete"),
    }


def test_deployment_is_hardened_and_recovery_defaults_off() -> None:
    dep = next(d for d in docs("deployment.yaml") if d["kind"] == "Deployment")
    pod = dep["spec"]["template"]["spec"]
    assert (
        pod["securityContext"]["runAsNonRoot"] is True and pod["serviceAccountName"] == "kubidoor"
    )
    c = pod["containers"][0]
    assert c["securityContext"]["allowPrivilegeEscalation"] is False
    assert c["securityContext"]["readOnlyRootFilesystem"] is True
    assert c["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert dep["spec"]["replicas"] == 1
    cfg = docs("configmap.yaml")[0]["data"]
    assert cfg["KUBIDOOR_RECOVERY__ENABLED"] == "false"
    assert cfg["KUBIDOOR_RECOVERY__DRY_RUN"] == "true"
    assert cfg["KUBIDOOR_RECOVERY__REQUIRE_APPROVAL"] == "true"
    assert "kube-system" in cfg["KUBIDOOR_RECOVERY__BLOCKED_NAMESPACES"]


def test_configmap_values_load_into_settings(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from kubidoor.core.config import Settings

    for k, v in docs("configmap.yaml")[0]["data"].items():
        monkeypatch.setenv(k, v)
    s = Settings()
    assert s.recovery.enabled is False and s.monitoring.namespaces == []
    assert s.recovery.allowed_namespaces == ["kubidoor-demo"]


def test_dockerfile_runs_as_non_root() -> None:
    text = Path("Dockerfile").read_text()
    assert "USER 10001:10001" in text and "HEALTHCHECK" in text
    compose = yaml.safe_load(Path("docker-compose.yml").read_text())
    env = compose["services"]["kubidoor"]["environment"]
    assert env["KUBIDOOR_RECOVERY__ENABLED"] == "${KUBIDOOR_RECOVERY__ENABLED:-false}"
    assert env["KUBIDOOR_RECOVERY__DRY_RUN"] == "${KUBIDOOR_RECOVERY__DRY_RUN:-true}"
    assert (
        env["KUBIDOOR_RECOVERY__REQUIRE_APPROVAL"] == "${KUBIDOOR_RECOVERY__REQUIRE_APPROVAL:-true}"
    )


# ---------------------------------------------------------------------------- demo
DEMO = Path("demo")


def demo_docs() -> list[tuple[str, dict]]:
    out = []
    for f in sorted(DEMO.glob("scenarios/*.yaml")) + [DEMO / "namespace.yaml"]:
        out += [(f.name, d) for d in yaml.safe_load_all(f.read_text()) if d]
    return out


def test_demo_is_isolated_to_the_dedicated_namespace() -> None:
    for fname, d in demo_docs():
        if d["kind"] == "Namespace":
            assert d["metadata"]["name"] == "kubidoor-demo"
            assert d["metadata"]["labels"]["kubidoor.io/demo"] == "true"  # deletion guard label
        else:
            assert d["kind"] == "Deployment", f"{fname}: only Deployments allowed in scenarios"
            assert d["metadata"]["namespace"] == "kubidoor-demo", fname
            assert d["metadata"]["labels"]["kubidoor.io/demo"] == "true", fname


def test_demo_covers_all_required_scenarios() -> None:
    names = {d["metadata"]["name"] for _, d in demo_docs() if d["kind"] == "Deployment"}
    assert {
        "demo-crashloop",
        "demo-imagepull",
        "demo-oom",
        "demo-unavailable",
        "demo-healthy",
    } <= names


def test_demo_scenarios_are_not_privileged_and_oom_has_a_limit() -> None:
    for _, d in demo_docs():
        if d["kind"] != "Deployment":
            continue
        pod = d["spec"]["template"]["spec"]
        assert not pod.get("hostNetwork") and not pod.get("hostPID")
        for c in pod["containers"]:
            assert not c.get("securityContext", {}).get("privileged")
    oom = next(d for _, d in demo_docs() if d["metadata"].get("name") == "demo-oom")
    assert (
        oom["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["memory"] == "64Mi"
    )


def test_demo_scripts_pin_context_and_guard_deletion() -> None:
    common = Path("scripts/_demo_common.sh").read_text()
    assert 'KCTX="kind-${CLUSTER_NAME}"' in common and "--context" in common
    assert "kubidoor.io/demo" in common
    for name in ("demo-up.sh", "demo-down.sh", "demo-scenarios.sh"):
        text = Path("scripts", name).read_text()
        # no bare kubectl calls: everything goes through the context-pinned helper `k`
        for line in text.splitlines():
            stripped = line.strip()
            assert not stripped.startswith("kubectl "), f"{name}: unpinned kubectl: {stripped}"
        assert "set -euo pipefail" in text
    down = Path("scripts/demo-down.sh").read_text()
    assert "--delete-cluster" in down and 'delete cluster --name "$CLUSTER_NAME"' in down


def test_demo_compose_thresholds_are_valid_settings(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from kubidoor.core.config import Settings

    env = yaml.safe_load(Path("docker-compose.yml").read_text())["services"]["kubidoor"][
        "environment"
    ]
    for k, v in env.items():
        if k.startswith("KUBIDOOR_MONITORING__"):
            monkeypatch.setenv(k, v)
    s = Settings()
    assert (
        s.monitoring.deployment_grace_seconds == 60 and s.monitoring.pending_threshold_seconds == 60
    )
