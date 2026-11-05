# RBAC and security

KubiDoor follows least privilege: **monitoring is read-only**, and write access is a separate, optional,
namespaced grant.

## Monitoring (always needed): `deploy/kubernetes/rbac.yaml`

ClusterRole `kubidoor-monitor`, bound to ServiceAccount `kubidoor`:

| API group | Resources | Verbs |
|---|---|---|
| core | nodes, pods, events, persistentvolumeclaims | get, list |
| apps | deployments | get, list |
| metrics.k8s.io | nodes, pods | get, list (optional; works without metrics-server) |

* **No access to Secrets.** KubiDoor never reads Secret contents; it only reports the *names* in a Pod's
  `imagePullSecrets`.
* No write verbs, no wildcards, no `cluster-admin`.
* To restrict what is monitored, set `KUBIDOOR_MONITORING__NAMESPACES`. For hard isolation replace the
  ClusterRole/ClusterRoleBinding with a Role/RoleBinding per namespace (nodes are cluster-scoped; set
  `KUBIDOOR_MONITORING__MONITOR_NODES=false`).
* Insufficient permissions fail safely: the affected resource type is reported as a monitoring error
  (`kubidoor_monitoring_errors_total{stage=...}`) and its incidents are left untouched. KubiDoor never
  asks for or assumes broader access.

## Recovery (optional): `deploy/kubernetes/rbac-recovery.yaml`

Not part of `kustomization.yaml`. A namespaced `Role` + `RoleBinding` granting only:

| Resource | Verbs | Used for |
|---|---|---|
| apps/deployments | get, patch | rollout restart |
| pods | get, list, delete | delete one unhealthy pod, verify replacement |

Copy it into each namespace listed in `recovery.allowed_namespaces`. There is no grant on Secrets, Nodes,
PV/PVC, RBAC objects, `pods/exec`, or anything cluster-scoped. Even with this grant the application-level
policy ([recovery-policy.md](recovery-policy.md)) still applies; RBAC is the second, independent wall.

Automated tests (`tests/unit/test_manifests.py`) fail if any shipped manifest grants Secrets, RBAC
resources, `pods/exec`, wildcards or write verbs to the monitoring role, binds `cluster-admin`, adds a
ClusterRole to the recovery file, or ships recovery defaults other than off/dry-run/approval.

## Connection modes

In-cluster service account (auto), local kubeconfig, and explicit context:
`KUBIDOOR_KUBERNETES__KUBECONFIG`, `KUBIDOOR_KUBERNETES__CONTEXT`, `KUBIDOOR_KUBERNETES__IN_CLUSTER`.

## Application security

* Optional bearer token (`KUBIDOOR_API_TOKEN`) for `/api/v1/*`, compared in constant time; `/health`,
  `/ready`, `/metrics` stay open for probes and Prometheus. **Without a token the API is unauthenticated**:
  do not expose it outside a trusted network. A blank token disables auth.
* No credentials in responses: API schemas are built from parsed objects that carry no env vars or secrets.
* JSON logs redact bearer tokens, `password/token/secret/api_key=...`, URL credentials and private keys;
  database URLs are logged with the password masked.
* Container runs as non-root (uid 10001), read-only root filesystem, all capabilities dropped,
  `allowPrivilegeEscalation: false`, seccomp RuntimeDefault.
* Audit trail is append-only at the application level (ORM guard rejects update/delete). A DBA with direct
  database access can still change rows; for stronger guarantees ship `audit_logs` to external storage.
* Prometheus labels use only small fixed sets (severity, resource type, action type/status, stage); never
  names, UIDs or incident ids.
