#!/usr/bin/env bash
# Stops the stack and removes ONLY demo resources. Pass --delete-cluster to also delete the
# dedicated 'kubidoor' kind cluster. Never touches any other cluster or namespace.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_demo_common.sh"
need docker

docker compose -f "$ROOT/docker-compose.yml" down || true
rm -f "$ROOT/demo/kubeconfig.internal"

if command -v kind >/dev/null && command -v kubectl >/dev/null && kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
  if assert_demo_namespace; then
    k delete namespace "$DEMO_NS" --ignore-not-found
  fi
  if [ "${1:-}" = "--delete-cluster" ]; then
    kind delete cluster --name "$CLUSTER_NAME"
  fi
fi
echo "demo cleaned up"
