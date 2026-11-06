# KubiDoor development status

_Last updated: 2026-11-05 (FINAL)_

## Completed and verified
- **Core app** (all phases 1-5): config, JSON logging with redaction, SQLAlchemy 2 models + Alembic
  migration, Kubernetes gateway/client, monitoring engine + scheduler, incident service, rule-based
  diagnostics, recovery engine, REST API (all endpoints in the spec), `kubidoor_*` metrics,
  Prometheus config, Grafana dashboard + provisioning.
- **Phase 6 artifacts written**: Dockerfile, `.dockerignore`, docker-compose.yml, Kubernetes
  manifests + least-privilege RBAC (`deploy/kubernetes/`), kind demo (`demo/`, `scripts/`),
  Makefile, `.env.example`, `.gitignore`, LICENSE, GitHub Actions CI, optional kind e2e test.
- **Hardening found by testing**: renamed `IncidentService.list` (shadowed builtin); incident only
  becomes INVESTIGATING when a real execution starts; tracked background verification threads
  joined on shutdown; blank `KUBIDOOR_API_TOKEN` disables auth; pending-pod rule no longer claims a
  cause without evidence.

## Verified results (this sandbox, Python 3.13)
- `pytest`: **236 passed**, 3 e2e deselected (skip cleanly when forced without a cluster).
- `ruff check` / `ruff format --check`: clean. `mypy` (strict, 58 files): clean.
- Mutation checks: disabling any of {atomic claim, blocked namespaces, cooldown, enabled flag,
  approval, UID check, dry-run, rate limit, loop prevention} makes specific tests fail.
- Real `KubeClient` tested against a local fake Kubernetes HTTP API (`tests/integration/test_kube_client.py`, 14 tests: paging, error translation, UID-guarded delete, restart patch, optional metrics, no Secrets access).
- Real startup smoke test: migrations run, `/health` 200, `/ready` 503 without a cluster,
  per-stage error metrics, graceful SIGTERM shutdown. Clean non-editable install works.
- Alembic renders valid PostgreSQL DDL offline (`alembic upgrade head --sql`).

## Web dashboard (added after the first delivery)
- `app/kubidoor/ui/static/` (index.html, app.css, app.js): overview, incidents + detail drawer with live diagnosis, recovery approve/reject/run, cluster tables, light/dark, responsive. Served at `/ui/` with a strict CSP; text-only rendering.
- New endpoint `GET /api/v1/recovery/policy` (read-only). `make ui-preview` runs it on a fake cluster.
- Verified in headless Chromium: no console/CSP errors, approve then dry-run flow works, screenshots reviewed (light, dark, mobile). `tests/integration/test_ui.py` covers headers, token behaviour, no external URLs, no innerHTML.

## NOT verified here (be transparent in the final report)
- `docker build` / Compose: the Docker daemon is unreachable in this sandbox.
- kind cluster, `make demo-up`, demo scenarios, `make e2e`: kind/kubectl not installed.
  The e2e test and scripts are written and statically checked only.
- Live PostgreSQL execution; Python 3.12 (only 3.13 available locally); `shellcheck`.

## Final state
- README.md and all seven docs files are written; relative links verified (0 broken).
- Final run: ruff clean, ruff format clean, mypy strict clean (60 files), pytest 236 passed
  (3 e2e deselected), stable over 5 repeated runs, clean non-editable install OK.

## Remaining (optional / needs an environment with Docker + kind)
1. Run `make docker-build`, `make demo-up`, `make e2e` on a machine with Docker and kind; fix any
   first-run issues (never executed in the sandbox).
2. Try live PostgreSQL and Python 3.12.
3. Optional: Helm chart, Prometheus as a metrics source, separation of duties for approvals.

## Exact next step
On a Docker-capable machine: `make demo-up`, watch incidents appear (~1-2 min), then `make e2e`.
