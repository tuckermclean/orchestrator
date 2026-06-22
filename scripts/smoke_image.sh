#!/usr/bin/env bash
# smoke_image.sh — Smoke-boot the built control-plane image in both backend modes.
#
# Usage:
#   scripts/smoke_image.sh <image-tag>
#
# Assertions:
#   subprocess mode  — /healthz → 200; / → 200 and serves HTML (not a 404/JSON error)
#   k8s mode         — /healthz → 200 (kubernetes package present; no ImportError on boot)
#
# Exits non-zero on any assertion failure; dumps docker logs before exiting.
set -euo pipefail

IMAGE="${1:?Usage: $0 <image-tag>}"
HEALTHZ_TIMEOUT=60   # seconds to poll /healthz before declaring failure

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

container_cleanup() {
    local cid="$1"
    if [ -n "$cid" ]; then
        docker rm -f "$cid" >/dev/null 2>&1 || true
    fi
}

dump_logs_and_fail() {
    local cid="$1"
    local msg="$2"
    echo "::error::SMOKE BOOT FAILED: ${msg}" >&2
    echo "--- docker logs for ${cid} ---" >&2
    docker logs "$cid" >&2 || true
    container_cleanup "$cid"
    exit 1
}

wait_for_healthz() {
    local cid="$1"
    local port="$2"
    local deadline=$(( $(date +%s) + HEALTHZ_TIMEOUT ))
    echo "  Polling http://localhost:${port}/healthz (timeout ${HEALTHZ_TIMEOUT}s)..."
    while true; do
        if curl -sf "http://localhost:${port}/healthz" >/dev/null 2>&1; then
            echo "  /healthz is UP"
            return 0
        fi
        if [ "$(date +%s)" -ge "$deadline" ]; then
            return 1
        fi
        sleep 2
    done
}

# ---------------------------------------------------------------------------
# Dummy kubeconfig for k8s mode — lets the kubernetes client parse and load
# config without reaching a real cluster.  Actual API calls will fail, but
# boot (import + client construction) must succeed.
# ---------------------------------------------------------------------------

DUMMY_KUBECONFIG="$(mktemp)"
cat > "$DUMMY_KUBECONFIG" <<'EOF'
apiVersion: v1
kind: Config
clusters:
- cluster:
    server: https://localhost:6443
  name: smoke-cluster
contexts:
- context:
    cluster: smoke-cluster
    user: smoke-user
  name: smoke-context
current-context: smoke-context
users:
- name: smoke-user
  user:
    token: dummy-token
EOF

cleanup_kubeconfig() {
    rm -f "$DUMMY_KUBECONFIG"
}
trap cleanup_kubeconfig EXIT

# ---------------------------------------------------------------------------
# Mode 1: subprocess (default)
# ---------------------------------------------------------------------------

echo "=== Smoke test: subprocess mode ==="

PORT_SUBPROCESS=18080
CID_SUBPROCESS=""
CID_SUBPROCESS="$(docker run -d \
    --name "smoke-subprocess-$$" \
    -p "${PORT_SUBPROCESS}:8080" \
    -e FORGE_TOKEN=dummy \
    "$IMAGE")"

echo "  Container: ${CID_SUBPROCESS}"

if ! wait_for_healthz "$CID_SUBPROCESS" "$PORT_SUBPROCESS"; then
    dump_logs_and_fail "$CID_SUBPROCESS" "/healthz did not return 200 within ${HEALTHZ_TIMEOUT}s (subprocess mode)"
fi

# Assert /healthz → 200
HTTP_STATUS="$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:${PORT_SUBPROCESS}/healthz")"
if [ "$HTTP_STATUS" != "200" ]; then
    dump_logs_and_fail "$CID_SUBPROCESS" "/healthz returned ${HTTP_STATUS}, expected 200 (subprocess mode)"
fi
echo "  PASS: /healthz → 200"

# Assert / → 200 and serves HTML (not a 404 or {"detail":"Not Found"})
ROOT_STATUS="$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:${PORT_SUBPROCESS}/")"
if [ "$ROOT_STATUS" != "200" ]; then
    dump_logs_and_fail "$CID_SUBPROCESS" "PWA not served at /: HTTP ${ROOT_STATUS} (expected 200) (subprocess mode)"
fi
ROOT_BODY="$(curl -s "http://localhost:${PORT_SUBPROCESS}/")"
if ! echo "$ROOT_BODY" | grep -qi "<html"; then
    dump_logs_and_fail "$CID_SUBPROCESS" "PWA not served at /: response body is not HTML (got: ${ROOT_BODY:0:200}) (subprocess mode)"
fi
echo "  PASS: / → 200 and serves HTML (PWA is present)"

container_cleanup "$CID_SUBPROCESS"
CID_SUBPROCESS=""
echo "  Subprocess mode: ALL ASSERTIONS PASSED"

# ---------------------------------------------------------------------------
# Mode 2: k8s backend
# ---------------------------------------------------------------------------

echo ""
echo "=== Smoke test: k8s mode ==="

PORT_K8S=18081
CID_K8S=""
CID_K8S="$(docker run -d \
    --name "smoke-k8s-$$" \
    -p "${PORT_K8S}:8080" \
    -e FORGE_TOKEN=dummy \
    -e HARNESS_EXECUTION_BACKEND=k8s \
    -v "${DUMMY_KUBECONFIG}:/tmp/smoke-kubeconfig:ro" \
    -e KUBECONFIG=/tmp/smoke-kubeconfig \
    "$IMAGE")"

echo "  Container: ${CID_K8S}"

if ! wait_for_healthz "$CID_K8S" "$PORT_K8S"; then
    dump_logs_and_fail "$CID_K8S" "/healthz did not return 200 within ${HEALTHZ_TIMEOUT}s (k8s mode) — possible kubernetes ImportError or boot crash"
fi

# Assert /healthz → 200
HTTP_STATUS="$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:${PORT_K8S}/healthz")"
if [ "$HTTP_STATUS" != "200" ]; then
    dump_logs_and_fail "$CID_K8S" "/healthz returned ${HTTP_STATUS}, expected 200 (k8s mode)"
fi
echo "  PASS: /healthz → 200 (kubernetes client constructed without ImportError)"

container_cleanup "$CID_K8S"
CID_K8S=""
echo "  k8s mode: ALL ASSERTIONS PASSED"

echo ""
echo "=== ALL SMOKE TESTS PASSED ==="
