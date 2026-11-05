#!/usr/bin/env bash
# Usage: scripts/demo-scenarios.sh apply|clean|status   (idempotent, demo namespace only)
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_demo_common.sh"
need kind; need kubectl
require_demo_cluster

case "${1:-}" in
  apply)
    k apply -f "$ROOT/demo/namespace.yaml"
    k apply -f "$ROOT/demo/scenarios/"
    echo "Scenarios applied to namespace $DEMO_NS. Incidents appear after the grace thresholds (~1-5 min)."
    ;;
  clean)
    if assert_demo_namespace; then
      # Delete only the demo workloads; the namespace is removed by demo-down.sh.
      k -n "$DEMO_NS" delete -f "$ROOT/demo/scenarios/" --ignore-not-found
    fi
    ;;
  status)
    k -n "$DEMO_NS" get deploy,pods -o wide
    ;;
  *) echo "usage: $0 apply|clean|status" >&2; exit 2 ;;
esac
