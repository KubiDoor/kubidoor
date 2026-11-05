#!/usr/bin/env bash
# Creates the kind cluster, applies the demo scenarios and starts KubiDoor + Prometheus + Grafana.
# Docker Compose does not provide Kubernetes: the cluster is created with kind.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_demo_common.sh"
need docker; need kind; need kubectl

if kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
  echo "kind cluster '$CLUSTER_NAME' already exists; reusing it"
else
  kind create cluster --config "$ROOT/demo/kind-config.yaml"
fi

"$ROOT/scripts/demo-scenarios.sh" apply

# A kubeconfig whose server address is reachable from inside the compose network (kind docker network).
kind get kubeconfig --internal --name "$CLUSTER_NAME" > "$ROOT/demo/kubeconfig.internal"
chmod 600 "$ROOT/demo/kubeconfig.internal"

# Read-only monitoring works with the kind admin credentials used here. To also try recovery against
# the demo namespace, set KUBIDOOR_RECOVERY__ENABLED=true (see docs/recovery-policy.md).
docker compose -f "$ROOT/docker-compose.yml" up -d --build

cat <<MSG

KubiDoor API : http://localhost:8000/docs
Prometheus   : http://localhost:9090
Grafana      : http://localhost:3000  (admin / \$GRAFANA_ADMIN_PASSWORD, default admin)
Watch incidents: curl -s localhost:8000/api/v1/incidents | python3 -m json.tool
MSG
