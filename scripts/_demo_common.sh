# Shared by the demo scripts. Safety rules enforced here:
#   * every kubectl call is pinned to the dedicated kind context, never your current context
#   * only the namespace labelled kubidoor.io/demo=true can ever be deleted
CLUSTER_NAME="kubidoor"
KCTX="kind-${CLUSTER_NAME}"
DEMO_NS="kubidoor-demo"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

need() { command -v "$1" >/dev/null 2>&1 || { echo "error: '$1' is required but not installed" >&2; exit 1; }; }
k() { kubectl --context "$KCTX" "$@"; }

require_demo_cluster() {
  kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME" \
    || { echo "error: kind cluster '$CLUSTER_NAME' not found. Run: make demo-up" >&2; exit 1; }
}

assert_demo_namespace() {
  local label
  label="$(k get namespace "$DEMO_NS" -o jsonpath='{.metadata.labels.kubidoor\.io/demo}' 2>/dev/null || true)"
  [ "$label" = "true" ] || { echo "refusing: namespace '$DEMO_NS' is missing or not labelled kubidoor.io/demo=true" >&2; return 1; }
}
