# Demo scenarios

> **Docker Compose alone does not give you Kubernetes.** The demo uses [kind](https://kind.sigs.k8s.io/)
> for a real local cluster and Compose for KubiDoor, Prometheus and Grafana.
> Requirements: Docker, `kind`, `kubectl`.

```bash
make demo-up                 # kind cluster "kubidoor" + scenarios + the Compose stack
make demo-down               # remove demo workloads, stop the stack (cluster is kept)
make demo-down ARGS=--delete-cluster   # also delete the dedicated kind cluster
make e2e                     # optional end-to-end test against the demo cluster
```

Isolation guarantees (enforced by tests and the scripts):

* Everything lives in the `kubidoor-demo` namespace (label `kubidoor.io/demo=true`).
* Every `kubectl` call is pinned to the `kind-kubidoor` context: your other clusters are never touched.
* Cleanup deletes only that labelled namespace (and only the `kubidoor` kind cluster on request).
* Scripts are idempotent: running `demo-up` twice reuses the cluster and re-applies the manifests.
* Nothing needs a production cluster.

| Workload | Scenario | Expected incident | Typical time |
|---|---|---|---|
| `demo-healthy` | control | **none** | n/a |
| `demo-crashloop` | container exits 1 | `CrashLoopBackOff` (HIGH) | ~1 min |
| `demo-imagepull` | image on `registry.invalid` | `ImagePullBackOff` (HIGH) | ~1 min |
| `demo-oom` | allocates 300 MiB under a 64 MiB limit | `OOMKilled` (HIGH) | ~1-2 min |
| `demo-unavailable` | readiness probe always 404 | `ReplicasUnavailable` then `ProgressDeadlineExceeded` | ~1-2 min |

The Compose stack lowers detection thresholds for the demo (60 s grace) so you do not wait five minutes.

```bash
curl -s localhost:8000/api/v1/incidents | python3 -m json.tool
curl -s localhost:8000/api/v1/diagnostics/pod/kubidoor-demo/<pod-name> | python3 -m json.tool
```

* The OOM diagnosis shows the real 64Mi limit. kind has no metrics-server, so it states that usage metrics are
  **unavailable** instead of inventing a number.
* Open Grafana at <http://localhost:3000> (dashboard "KubiDoor Overview").

## Trying recovery (opt in)

Recovery stays off unless you opt in. To try it on the demo namespace only:

```bash
kubectl --context kind-kubidoor apply -f deploy/kubernetes/rbac-recovery.yaml   # only needed for in-cluster runs
KUBIDOOR_RECOVERY__ENABLED=true KUBIDOOR_RECOVERY__DRY_RUN=true docker compose up -d   # dry-run first
```

1. A `PENDING_APPROVAL` action appears for the crash-looping pod: `GET /api/v1/recovery/actions`.
2. `POST .../approve`, then `POST .../execute`: in dry-run you get `DRY_RUN` and the cluster is untouched.
3. Set `KUBIDOOR_RECOVERY__DRY_RUN=false`, repeat. The pod is deleted and recreated, but because
   `demo-crashloop` always crashes, verification ends `FAILED` and the incident stays open. That is correct:
   deleting a pod does not fix a broken application.

## Status of this demo

The manifests, scripts, e2e test and compose file are checked statically by the test suite. They were **not
executed end to end in the development sandbox** (no Docker daemon or kind available), so expect to adjust
timings on first run.
