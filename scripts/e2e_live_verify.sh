#!/usr/bin/env bash
# e2e_live_verify.sh — drive a real issue through the live k8s orchestrator and
# verify the run pipeline end-to-end: runs get recorded (Runs screen populates),
# statuses advance, transcript events stream, and a PR is produced.
#
# One self-contained command so verification doesn't depend on a dozen ad-hoc steps.
#
# Env (all optional):
#   NS         k8s namespace            (default: orchestrator)
#   REPO       sandbox repo owner/name  (default: tuckermclean/sandbox-derp)
#   PORT       local port-forward port  (default: 18080)
#   ISSUE_TITLE / ISSUE_BODY            (defaults below)
#   POLL_SECS  total seconds to watch   (default: 180)
#   NO_ISSUE=1 skip opening an issue; just inspect existing runs
#
# Requires: kubectl (cluster context), gh (authed), python3 with the repo's deps.
set -uo pipefail

NS="${NS:-orchestrator}"
REPO="${REPO:-tuckermclean/sandbox-derp}"
PORT="${PORT:-18080}"
POLL_SECS="${POLL_SECS:-180}"
ISSUE_TITLE="${ISSUE_TITLE:-e2e: add a HELLO.md}"
ISSUE_BODY="${ISSUE_BODY:-Please add a short, friendly HELLO.md greeting file at the repo root. Keep it brief.}"

log(){ printf '\033[36m[e2e]\033[0m %s\n' "$*"; }
err(){ printf '\033[31m[e2e]\033[0m %s\n' "$*" >&2; }

pod="$(kubectl get pods -n "$NS" -l app.kubernetes.io/name=orchestrator \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)"
[ -n "$pod" ] || { err "no control-plane pod in ns/$NS"; exit 1; }
img="$(kubectl get pod -n "$NS" "$pod" -o jsonpath='{.spec.containers[0].image}')"
log "control-plane: $pod ($img)"

# Mint an operator token locally from the cluster secret.
key="$(kubectl get secret orchestrator-secrets -n "$NS" \
  -o jsonpath='{.data.OPERATOR_SECRET_KEY}' 2>/dev/null | base64 -d)"
[ -n "$key" ] || { err "could not read OPERATOR_SECRET_KEY"; exit 1; }
TOK="$(OPERATOR_SECRET_KEY="$key" python3 -c \
  'from src.api.auth import issue_token; print(issue_token("admin"))')" \
  || { err "token mint failed (run from repo root with deps installed)"; exit 1; }

# Port-forward (background), ensure cleanup.
pkill -f "port-forward.*${PORT}:8080" 2>/dev/null
sleep 1
kubectl port-forward -n "$NS" "$pod" "${PORT}:8080" >/tmp/e2e_pf.log 2>&1 &
PF_PID=$!
trap 'kill "$PF_PID" 2>/dev/null' EXIT
sleep 4
grep -q "Forwarding" /tmp/e2e_pf.log || { err "port-forward failed: $(cat /tmp/e2e_pf.log)"; exit 1; }
log "port-forward → localhost:${PORT}"

if [ "${NO_ISSUE:-0}" != "1" ]; then
  url="$(gh issue create --repo "$REPO" --title "$ISSUE_TITLE" --body "$ISSUE_BODY")"
  log "opened issue: $url"
fi

BASE="http://localhost:${PORT}" TOK="$TOK" REPO="$REPO" POLL_SECS="$POLL_SECS" python3 - <<'PY'
import json, os, time, urllib.request
from collections import Counter
base, tok = os.environ["BASE"], os.environ["TOK"]
total = int(os.environ["POLL_SECS"])
def get(p):
    return json.load(urllib.request.urlopen(
        urllib.request.Request(base+p, headers={"Authorization":"Bearer "+tok}), timeout=10))
print(f"[e2e] watching /api/runs for {total}s ...")
deadline = time.time()+total
last=""
while time.time() < deadline:
    runs = get("/api/runs")
    runs.sort(key=lambda r: r.get("started_at",""), reverse=True)
    cols=[]
    for r in runs[:5]:
        d = get("/api/runs/"+r["run_id"]); evs=d.get("events",[])
        types=Counter(e.get("event_type") for e in evs)
        transcript=sum(v for k,v in types.items() if str(k).startswith("agent_"))
        cols.append(f"{r['run_id'][:6]}:{r['type'][:10]}/{r['status'][:4]}/ev{len(evs)}/tx{transcript}")
    line=" | ".join(cols)
    if line!=last: print("  "+line); last=line
    # stop early once a recent run has transcript events
    if any("tx" in c and not c.endswith("tx0") for c in cols):
        print("[e2e] transcript events detected ✓"); break
    time.sleep(12)
print("[e2e] status counts:", dict(Counter(r["status"] for r in get("/api/runs"))))
PY

echo "[e2e] open PRs on $REPO:"
gh pr list --repo "$REPO" --state open --json number,title,labels \
  -q '.[] | "  #\(.number) [\([.labels[].name]|join(","))] \(.title)"' | head
log "done"
