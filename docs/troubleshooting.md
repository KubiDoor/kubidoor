# Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `/ready` returns 503 with `kubernetes_error` | KubiDoor cannot load kubeconfig or reach the API. Check `KUBIDOOR_KUBERNETES__KUBECONFIG` / `CONTEXT`; in a container the kubeconfig server address must be reachable from inside (`kind get kubeconfig --internal`). `/health` stays 200: it only means the process is alive. |
| No incidents although pods are broken | Look at `/metrics`: rising `kubidoor_monitoring_errors_total{stage=...}` means that stage cannot read the cluster (RBAC `403`, timeout). Failed stages are skipped on purpose and never create or resolve incidents. Also check `KUBIDOOR_MONITORING__NAMESPACES` and the thresholds (a Pending pod only alerts after `pending_threshold_seconds`). |
| Incident appears only after minutes | Time-based thresholds (`pending`, `not_ready`, `deployment_grace`) delay alerts to avoid noise. Lower them via env (see `.env.example`) or use `GET /api/v1/diagnostics/...`, which ignores them. |
| `403 policy_violation` on a recovery request | The response lists every violated rule: recovery disabled, namespace not in `allowed_namespaces` / in `blocked_namespaces`, action not allowlisted, wrong resource type. This is the safety model working. |
| Action ended `CANCELLED` | See `error_message`: UID mismatch (pod was replaced), target gone, no controller, cooldown, rate limit, failure loop, or recovery disabled after approval. Request a new action when appropriate. |
| Action `FAILED`: "verification timed out" | The call succeeded but the workload did not become healthy within `verification_timeout_seconds`. Fix the underlying cause; restarts do not repair bad images, config or quotas. |
| Action `FAILED`: "interrupted" | KubiDoor restarted mid-action. The cluster change may have happened; inspect it manually. |
| `409 conflict` on approve/execute | Someone else already approved/executed, or the action is in another state. GET it to see the current status. |
| `401 unauthorized` | `KUBIDOOR_API_TOKEN` is set; send `Authorization: Bearer <token>`. |
| Cluster overview says `resource_metrics.available=false` | metrics-server is not installed or not reachable. Expected on kind; KubiDoor reports it instead of guessing. Disable lookups with `KUBIDOOR_KUBERNETES__METRICS_ENABLED=false`. |
| `database is locked` with SQLite | Run a single KubiDoor instance, keep the DB on a local disk, or switch to PostgreSQL. |
| Migration errors at start | The DB must be reachable and `KUBIDOOR_MIGRATIONS_DIR` must point at the `migrations/` folder (the image sets it). Set `KUBIDOOR_AUTO_MIGRATE=false` and run `make migrate` yourself if you prefer. |
| Metrics gap in Grafana | Check the Prometheus target `kubidoor:8000` is UP and the datasource uid is `prometheus` (provisioned automatically). |

Logs are JSON on stdout (`timestamp, level, component, message`, plus `incident_id`, `action_id`,
`resource_type`, `resource_name`, `namespace` when relevant). Raise detail with `KUBIDOOR_LOG_LEVEL=DEBUG`.
Secrets are redacted.
