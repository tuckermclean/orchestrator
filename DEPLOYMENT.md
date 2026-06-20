# DEPLOYMENT.md — Kubernetes Deployment Specification

**Version**: 1.0
**Date**: 2026-06-20
**Status**: Draft
**Depends on**: `ARCHITECTURE.md` 1.0, `API.md` 1.0, `STATE_MACHINE.md` 1.0

---

## §1 Overview

The orchestrator ships as a single multi-architecture container image. One image, one
process, one Kubernetes `Deployment`. The container hosts the `OrchestratorService`
(control plane and webhook ingress), the `Engine`, all port implementations, the reconcile
scheduler, and the PWA static assets. Kubernetes handles high availability through replica
count — there is no separate web-tier or worker-tier container.

This topology is warranted by the architecture's durability model. Entity state lives
exclusively in forge labels (`ARCHITECTURE.md §4.1`). The process holds no durable
in-process state: if it crashes mid-operation, every entity remains in its last-written
label state and the reconciler recovers stranded entities on the next cron tick
(`STATE_MACHINE.md §1`, `ARCHITECTURE.md §6`). Replicas are therefore stateless with
respect to the domain problem; the only shared dependencies at scale are the dedup LRU
and the `SwarmLimits` semaphores (see `§4`).

For the logical topology that this deployment realizes, see `ARCHITECTURE.md §5`.

---

## §2 Container Image

### §2.1 Build

The image is built for two architectures: `linux/amd64` (production Kubernetes nodes) and
`linux/arm64` (Apple Silicon development machines). Use Docker Buildx or any
OCI-compliant multi-platform builder. The build target for each architecture is identical;
the builder selects the appropriate platform layer automatically.

The base image depends on the runtime chosen by the implementation:

- **Python runtime**: a minimal Python image such as `python:3.12-slim`. Strip
  development dependencies from the final image stage — only the application wheel and
  its runtime dependencies belong in the shipped layer.
- **Rust runtime**: a minimal base such as `gcr.io/distroless/cc`. The compiled binary
  is the only artifact; no interpreter is required.

This spec does not mandate a runtime. The choice is deferred to the implementation.
Whatever the base, the image must run as a non-root user and must not include a shell or
package manager in the final layer.

**Specialist agent pack acquisition** — The specialist agent pack is **baked into the
image at build time**. This is a required step in the Dockerfile; it must not be deferred
to container startup (`AGENT_PACK.md §3`). The pack is fetched from the repo and ref
specified in `AgentPackConfig` (`API.md §2`) and flattened into `dest_dir` (default
`.agents/`):

```dockerfile
# Build args — supply via --build-arg or docker buildx bake
ARG AGENT_PACK_REPO_URL="https://github.com/msitarzewski/agency-agents"
ARG AGENT_PACK_PINNED_REF="d6553e261e595c651064f899a6c33dd5aa71c9e3"
ARG AGENT_PACK_DEST_DIR=".agents"

# In the builder stage, after code checkout:
RUN git clone --no-tags --depth 1 ${AGENT_PACK_REPO_URL} /tmp/agency-agents \
 && git -C /tmp/agency-agents fetch --depth 1 origin ${AGENT_PACK_PINNED_REF} \
 && git -C /tmp/agency-agents checkout ${AGENT_PACK_PINNED_REF} \
 && mkdir -p /app/${AGENT_PACK_DEST_DIR} \
 && find /tmp/agency-agents -mindepth 2 -name "*.md" -exec cp {} /app/${AGENT_PACK_DEST_DIR}/ \; \
 && rm -rf /tmp/agency-agents
```

The flattening step (`find -mindepth 2`) copies every `*.md` file from the pack's category
subdirs into a single flat directory. After this step, specialists are addressable at
`/app/.agents/<AgentRef>` where `AgentRef` is the flat basename
(e.g. `engineering-security-engineer.md`).

To update the specialist pack SHA, change `AGENT_PACK_PINNED_REF` and rebuild. Review the
diff at `https://github.com/msitarzewski/agency-agents/compare/<old>...<new>` before
bumping (`AGENT_PACK.md §5.1`). A SHA change requires a full image rebuild and a new
deployment rollout.

PWA static assets are compiled at build time and copied into the image with a `COPY`
instruction from the build stage. The process itself serves them at `/`; there is no
separate nginx sidecar. This keeps the deployment to one container and one process.

The Docker Build and Scan CI check (`BLOCKING_CI_CHECKS[3]`, `API.md §2`) must pass
before the image is eligible for push.

### §2.2 Image Provenance

Every pushed image must carry verifiable provenance. Apply the following controls at push
time:

**Signing**: sign the image with Sigstore/cosign. The cosign public key (or the keyless
Fulcio certificate) must be available to the operator's admission controller so that
unsigned images can be rejected at deploy time.

**Software Bill of Materials**: generate an SBOM in CycloneDX or SPDX format and attach
it to the image as an OCI attestation. The SBOM must enumerate all direct and transitive
dependencies included in the final image layer, including the specialist agent pack.

**Agent-pack provenance annotation**: record the pack source and pinned SHA as OCI image
annotations:

```
org.opencontainers.image.agent-pack.source  = <AGENT_PACK_REPO_URL>
org.opencontainers.image.agent-pack.ref     = <AGENT_PACK_PINNED_REF>
```

These annotations appear in `docker inspect` output and are included in the SBOM. They
provide an audit trail linking each image digest to the exact specialist-pack commit it
contains (`AGENT_PACK.md §3.2`).

**Digest pinning**: the Helm chart must reference the image by its `sha256:...` digest,
not by a mutable tag. Mutable tags are permitted as aliases for human reference but must
not be the resolution target in any Kubernetes manifest. Pinning guarantees that a
rollback redeploys the exact binary that was previously verified.

**Registry**: the operator chooses the container registry (Docker Hub, GitHub Container
Registry, a private registry). The image name convention is `orchestrator:vX.Y.Z`.

### §2.3 CI/CD for the Image

The image is built and pushed as part of the CI/CD pipeline that runs on every version
tag. The pipeline enforces the following gate ordering:

1. The full test suite (see `TESTING.md §7.2`) must be green.
2. The Helm Lint and Helm Kubeconform checks (`BLOCKING_CI_CHECKS[4,5]`, `API.md §2`)
   must pass against the chart in `charts/orchestrator/`.
3. Only after all six `BLOCKING_CI_CHECKS` pass is the image built and pushed.

The image build step is itself one of the `BLOCKING_CI_CHECKS` (`Docker Build & Scan`,
`API.md §2`). This means the full six-check gate must pass in aggregate before any
downstream deployment automation promotes the new image digest into the Helm values for
staging or production.

Push rules:

- A tag matching `vX.Y.Z` triggers image build, sign, SBOM attestation, and push.
- The `latest` tag is applied only on an explicit release action, not on every tag push.
- Intermediate CI runs on pull requests build and scan the image but do not push it.

---

## §3 Helm Chart Shape

### §3.1 Chart Structure

```
charts/orchestrator/
  Chart.yaml
  values.yaml
  templates/
    deployment.yaml
    service.yaml
    ingress.yaml
    secret.yaml          # ExternalSecret or SealedSecret compatible
    configmap.yaml
    serviceaccount.yaml
    cronjob.yaml         # rendered only when reconcile_mode=external
    pdb.yaml             # PodDisruptionBudget
    hpa.yaml             # HorizontalPodAutoscaler (optional, disabled by default)
```

`Chart.yaml` records the chart version and the `appVersion` field, which must match the
image tag. All templates are parameterized through `values.yaml`; operators supply
overrides via `-f my-values.yaml` at install or upgrade time.

### §3.2 Deployment Manifest

The `Deployment` runs one container. Key fields:

**Replicas**: default `2`. Two replicas give high availability during rolling updates and
node failures without requiring a distributed semaphore for single-cluster deployments at
modest scale. Operators running a single replica for development or cost reasons must
accept that the dedup LRU and `SwarmLimits` semaphores can be in-process (see `§4`).

**Container ports**: two ports are exposed per pod:

| Port | Default | Purpose |
|------|---------|---------|
| `http` | 8080 | Webhook ingress, control-plane API, and PWA static assets |
| `metrics` | 9090 | Prometheus metrics endpoint |

**Liveness probe**: `GET /healthz` on the `http` port, expected HTTP 200. The probe
checks only that the process is alive and the HTTP listener is accepting connections.
If the liveness probe fails, Kubernetes restarts the pod. Crash-only durability ensures
correctness after restart because entity state survives in forge labels.

**Readiness probe**: `GET /readyz` on the `http` port, expected HTTP 200. The readiness
probe checks forge connectivity, reconcile scheduler liveness, and database reachability
(see `§5.1`). A pod that fails readiness is removed from the Service endpoints and
receives no traffic until it recovers. This prevents webhook delivery to a pod that
cannot reach the forge.

**Resource defaults** (adjust based on observed load; harness API calls are the primary
operational cost, not CPU):

| | Request | Limit |
|-|---------|-------|
| CPU | 100m | 500m |
| Memory | 256Mi | 512Mi |

**Security context** (applied at the pod and container level):

```
securityContext:
  runAsNonRoot: true
  readOnlyRootFilesystem: true
  allowPrivilegeEscalation: false
  capabilities:
    drop: ["ALL"]
```

A writable volume (e.g., `emptyDir`) must be mounted at any path the process writes to
at runtime (temp files, socket files) when `readOnlyRootFilesystem: true` is set.

**Image reference**: the `image` field must use the `sha256:...` digest form, not a tag.
The Helm chart derives this from `values.yaml` field `image.digest`.

**ImagePullPolicy**: `IfNotPresent` when using digest pinning; the digest uniquely
identifies the layer set so re-pulling on every pod start is unnecessary.

### §3.3 Ingress

One `Ingress` resource exposes four path prefixes on a single hostname. All paths
terminate TLS at the Ingress controller.

| Path | Backend | Purpose |
|------|---------|---------|
| `/webhook/` | Service port 8080 | Forge webhook delivery (signature-validated) |
| `/api/` | Service port 8080 | Control-plane API (CLI and PWA data) |
| `/push/` | Service port 8080 | Web Push VAPID subscription endpoint |
| `/` | Service port 8080 | PWA static assets (SPA catch-all) |

Path precedence: more-specific prefixes (`/webhook/`, `/api/`, `/push/`) must be matched
before the catch-all `/`. Configure `pathType: Prefix` for all four entries. The SPA
catch-all at `/` must rewrite all non-asset 404 responses to `index.html` so that
client-side routing works correctly on hard refresh; configure this via an Ingress
annotation or an application-level handler depending on the Ingress class.

**TLS**: use cert-manager with a Let's Encrypt `ClusterIssuer`, or supply an operator-
provided certificate via a `tls.crt`/`tls.key` Secret reference in the `tls:` stanza.
The Ingress class (nginx, Traefik, etc.) is operator-selected; the chart exposes
`ingress.className` in `values.yaml`.

**Webhook delivery**: the forge webhook must be pointed to `https://<your-domain>/webhook/`.
The content type must be `application/json`. The webhook secret must match
`OPERATOR_SECRET_KEY` (see `§3.4`) so that the process can verify the HMAC signature on
each delivery.

### §3.4 Secret

The `orchestrator-secrets` Secret holds all sensitive values. It must never be stored in
plaintext in source control. The chart's `templates/secret.yaml` is authored as an
`ExternalSecret` (External Secrets Operator) or `SealedSecret` (Sealed Secrets) stub so
that GitOps workflows can manage it without embedding cleartext.

Operators using neither operator may create the Secret manually:

```
kubectl create secret generic orchestrator-secrets \
  --from-literal=FORGE_TOKEN=<value> \
  --from-literal=HARNESS_API_KEY=<value> \
  --from-literal=OPERATOR_SECRET_KEY=<value> \
  --from-literal=PUSH_VAPID_PRIVATE_KEY=<value> \
  -n orchestrator
```

**Secret fields**:

| Key | Contents |
|-----|----------|
| `FORGE_TOKEN` | GitHub App private key and App ID (preferred), or a Personal Access Token with repo scope. The `PortProvider` implementation reads this to construct `ForgePort` credentials (`API.md §8.2`). |
| `HARNESS_API_KEY` | API key for the harness runtime (Claude Code, Codex, or equivalent). The `PortProvider` reads this to construct `HarnessPort` credentials. Never exposed to agent environments; agents run sandboxed without forge tokens or harness keys (`ARCHITECTURE.md §8`). |
| `OPERATOR_SECRET_KEY` | HMAC secret used for two purposes: (1) verifying the forge webhook signature on every delivery to `/webhook/`; (2) signing operator control-plane API tokens. Must be long, random, and rotated on compromise. |
| `PUSH_VAPID_PRIVATE_KEY` | VAPID private key for Web Push notifications. The corresponding VAPID public key is non-sensitive and may live in the ConfigMap. |

The Secret is mounted into the container as environment variables. The application must
not log any of these values; log sanitization is a required property of any
`PortProvider` implementation.

### §3.5 ConfigMap

Non-sensitive configuration is stored in the `orchestrator-config` ConfigMap. These
values correspond directly to the `Config` and `SwarmLimits` types (`API.md §8.2`).

| Key | Default | Notes |
|-----|---------|-------|
| `RECONCILE_CRON` | `*/15 * * * *` | Must match `RECONCILER_CRON` constant (`API.md §2`). Changing this alters how quickly the reconciler detects stranded entities. |
| `DEDUP_WINDOW` | `1000` | Delivery-ID ring buffer size. Governs `Config.dedup_window`. |
| `SWARM_LIMITS_GLOBAL` | `10` | `SwarmLimits.max_concurrent_runs_global`. |
| `SWARM_LIMITS_PER_REPO` | `4` | `SwarmLimits.max_concurrent_runs_per_repo`. |
| `SWARM_LIMITS_RECONCILES` | `4` | `SwarmLimits.max_concurrent_reconciles`. |
| `DB_URL` | `sqlite:///data/orchestrator.db` | Path to the SQLite file (single-replica) or a Postgres DSN (multi-replica). See `§4.1`. |
| `RECONCILE_MODE` | `internal` | `internal` runs the reconcile cadence loop inside the process (`OrchestratorService.start()`). `external` disables the internal loop; a CronJob calls `POST /api/reconcile` on the schedule (see `§3.6`). |
| `PUSH_VAPID_PUBLIC_KEY` | — | VAPID public key for Web Push. Non-sensitive; placed here rather than in the Secret. |

**What does not belong in the ConfigMap**: per-repo `RepoConfig` settings — `enabled`,
`intake_enabled`, and `allowlist` — live in the backing database and are managed through
the control-plane API and PWA. They are not static configuration; they change at
operator request without a pod restart (`API.md §8.4` registry management methods).

### §3.6 CronJob (Conditional)

Rendered only when `RECONCILE_MODE=external`. When the internal loop is disabled, a
Kubernetes CronJob calls the reconcile API on the configured schedule.

Spec shape:

```
schedule: "*/15 * * * *"
concurrencyPolicy: Forbid
jobTemplate:
  spec:
    template:
      spec:
        restartPolicy: OnFailure
        containers:
        - name: reconcile-trigger
          image: curlimages/curl:latest
          command:
          - curl
          - -X
          - POST
          - -H
          - "Authorization: Bearer $(TOKEN)"
          - --fail
          - http://orchestrator.orchestrator.svc.cluster.local/api/reconcile
          env:
          - name: TOKEN
            valueFrom:
              secretKeyRef:
                name: orchestrator-secrets
                key: RECONCILE_TOKEN
```

`concurrencyPolicy: Forbid` prevents a slow reconcile sweep from overlapping with the
next cron tick. The internal mode is simpler to operate and is the recommended default
for most deployments. The external CronJob is preferable in environments where the
reconcile cadence must be visible in cluster audit logs or managed alongside other
scheduled jobs.

### §3.7 PodDisruptionBudget

A `PodDisruptionBudget` ensures at least one pod is available during voluntary
disruptions (node drains, rolling updates):

```
spec:
  minAvailable: 1
  selector:
    matchLabels:
      app: orchestrator
```

With the default of two replicas, the PDB allows one pod to be evicted at a time while
keeping one pod in service. This is consistent with the rolling-update strategy: one pod
is replaced before the other is drained. Operators running a single replica must
acknowledge that the PDB will block voluntary disruptions until the replica is healthy.

---

## §4 Scaling

### §4.1 Statelessness and Shared State

Entity state is stateless from the orchestrator's perspective: it lives entirely in forge
labels on issues and pull requests (`ARCHITECTURE.md §4.1`). Adding replicas does not
require migrating or partitioning entity state.

Two components do require shared state when running more than one replica:

**Dedup LRU** (`delivery_id` ring buffer, `Config.dedup_window`). With a single replica,
an in-process LRU (e.g., `collections.OrderedDict` / `lru::LruCache`) is sufficient.
With more than one replica, duplicate webhook deliveries processed by different pods will
not be caught by either pod's local cache. Use a shared Postgres table with a bounded
eviction policy, or a Redis key with a TTL, as the dedup store. Note that the dedup cache
is a latency optimization, not a correctness requirement: the `Engine.converge`
idempotency gate and the reconciler's per-channel guards make reprocessing safe
(`API.md §8.5`). Duplicate events cause harmless no-ops, not data corruption.

**Run index and repo registry**. The run index (`run_id`, repo, issue/PR ref, status,
timestamps) and the repo registry (`RepoConfig` per managed repo) must be in a shared
backing store. SQLite on a local file is only viable for single-replica deployments where
no concurrent writers exist. For two or more replicas, use Postgres (or equivalent). The
`DB_URL` ConfigMap key selects the store.

### §4.2 SwarmLimits and Concurrency

`SwarmLimits` semaphores bound how many harness dispatch calls run concurrently, globally
and per-repo (`API.md §8.2`). With a single replica, in-process semaphores are correct.
With more than one replica, in-process semaphores are per-pod and do not enforce the
global or per-repo caps across the cluster.

The Helm chart exposes `swarm.backendType` in `values.yaml` with two options:

- `memory` (default): in-process semaphores. Correct for single-replica deployments.
- `redis`: a Redis-backed distributed semaphore (e.g., `SET NX` with expiry, or a
  Redlock-style implementation). Required for multi-replica deployments where per-repo
  and global caps must be enforced cluster-wide.

The Redis connection string is held in the Secret (key `REDIS_URL`) when
`swarm.backendType=redis`.

### §4.3 Horizontal Pod Autoscaler (Optional)

An HPA is included in the chart but disabled by default. When enabled, it scales on
CPU utilization or on the custom Prometheus metric `orchestrator_in_flight_runs`
(see `§5.2`).

Scale ceiling: to avoid defeating the `SwarmLimits` semaphore, the maximum replica count
should not exceed:

```
ceil(SwarmLimits.max_concurrent_runs_global / SwarmLimits.max_concurrent_runs_per_repo)
```

With the default limits (global=10, per\_repo=4) this ceiling is 3 replicas. Scaling
beyond this point adds replicas that spend most of their time blocked on the distributed
semaphore. Adjust `SWARM_LIMITS_GLOBAL` and `SWARM_LIMITS_PER_REPO` before increasing
the HPA `maxReplicas`.

---

## §5 Observability

### §5.1 Health Endpoints

Both endpoints are served on the `http` port (default 8080).

**`GET /healthz` — liveness**

Returns HTTP 200 as long as the process is alive and the HTTP listener is functioning.
Does not check external dependencies. A failure here causes Kubernetes to restart the pod.

Response schema:

```json
{ "status": "ok" }
```

**`GET /readyz` — readiness**

Returns HTTP 200 only when all dependencies are healthy. Returns HTTP 503 when any check
fails. A pod that returns 503 is removed from the Service endpoints until it recovers.

Response schema:

```json
{
  "status": "ok" | "degraded" | "down",
  "checks": {
    "forge":     "ok" | "error",
    "db":        "ok" | "error",
    "scheduler": "ok" | "error"
  }
}
```

Check definitions:

| Check | Passes when |
|-------|-------------|
| `forge` | A lightweight forge API call (e.g., rate-limit check) succeeds within a short timeout. |
| `db` | The backing store (SQLite or Postgres) accepts a simple read query. |
| `scheduler` | The internal reconcile loop has ticked within `2 * RECONCILER_CRON_INTERVAL` (i.e., within 30 minutes for the default cron). When `RECONCILE_MODE=external`, this check is omitted. |

### §5.2 Metrics (Prometheus)

Metrics are served at `/metrics` on the `metrics` port (default 9090). Configure
Prometheus to scrape this endpoint. A `ServiceMonitor` resource (if using the Prometheus
Operator) is provided as an optional chart template.

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `orchestrator_in_flight_runs` | Gauge | `repo` | Active harness runs currently holding a semaphore slot. |
| `orchestrator_escalations_total` | Counter | `repo`, `cause` | Cumulative escalations by cause code (E1–E10, `STATE_MACHINE.md §6`). The `cause` label value is the `EscalationCause` token (e.g., `protected-path`, `no-progress`). |
| `orchestrator_pipeline_health` | Gauge | `repo` | Current `pipeline_health` verdict encoded as 0=BLOCKED, 1=AT\_RISK, 2=ON\_TRACK (`API.md §3.9`, `DECISION_LOGIC.md §9`). |
| `orchestrator_intake_decisions_total` | Counter | `repo`, `decision` | Cumulative intake decisions by outcome (`admit` or `queue`, `API.md §3.11`). |
| `orchestrator_reconcile_duration_seconds` | Histogram | — | Wall time for one complete `Engine.reconcile` sweep across all repos. |
| `orchestrator_webhook_deliveries_total` | Counter | `repo`, `name`, `action`, `routed` | Cumulative webhook deliveries, labeled by event name, action, and whether they were routed to an engine method (`true`) or dropped (`false`, including dedup hits). |

### §5.3 Logs

All logs are emitted as structured JSON on stdout. The Kubernetes log collector (Fluentd,
Vector, etc.) ships them to the operator's log store. Log level is configurable via the
`LOG_LEVEL` environment variable (`debug`, `info`, `warn`, `error`); default `info`.

**Mandatory fields on every log line**:

| Field | Description |
|-------|-------------|
| `timestamp` | RFC 3339 UTC |
| `level` | `debug`, `info`, `warn`, or `error` |
| `component` | Emitting component: `webhook`, `engine`, `reconciler`, `forge_port`, `harness_port`, `control_plane` |

**Conditional fields** (included when the context is available):

| Field | When present |
|-------|-------------|
| `repo` | Any event, reconcile, or engine call scoped to a repo |
| `delivery_id` | Webhook ingress events |
| `run_id` | Harness dispatch and session calls |
| `issue_ref` | Engine calls involving an issue |
| `pr_ref` | Engine calls involving a pull request |
| `escalation_cause` | Escalation events (E1–E10) |

**Audit events**: every admit, queue, promote, dispatch, escalation, and approve action
is logged at INFO with `event_type: audit`. This produces a tamper-evident trail of all
state transitions driven by the orchestrator.

### §5.4 Distributed Tracing (Optional)

For deployments that need end-to-end trace visibility, integrate OpenTelemetry. Instrument
`OrchestratorService.handle_event` as the root span; propagate the trace context through
`Engine.dispatch`, `Engine.converge`, `Engine.reconcile`, and each port call as child
spans. Export traces to Jaeger, Grafana Tempo, or any OTLP-compatible backend.

Configure the OTLP exporter endpoint via `OTEL_EXPORTER_OTLP_ENDPOINT` in the
ConfigMap. Tracing is disabled by default when this variable is absent.

---

## §6 First-Run Setup

The following steps provision a new orchestrator deployment from scratch. Complete them in
order.

**Step 1 — Create the namespace**:

```
kubectl create namespace orchestrator
```

**Step 2 — Provision secrets**:

Create or configure the `orchestrator-secrets` Secret using the ExternalSecrets,
SealedSecrets, or manual method described in `§3.4`. At minimum, `FORGE_TOKEN`,
`HARNESS_API_KEY`, and `OPERATOR_SECRET_KEY` must be present before the deployment
starts.

**Step 3 — Install the Helm chart**:

```
helm install orchestrator ./charts/orchestrator \
  -n orchestrator \
  -f my-values.yaml \
  --set image.digest=sha256:<digest>
```

The chart defaults apply for all fields not present in `my-values.yaml`. At minimum,
`my-values.yaml` must set `ingress.hostname` and either the cert-manager issuer reference
or a TLS secret name.

**Step 4 — Configure the forge webhook**:

In the forge (GitHub org or repository settings), create a webhook pointing to
`https://<your-domain>/webhook/`. Set the content type to `application/json`. Set the
webhook secret to the same value as `OPERATOR_SECRET_KEY`. Select the event types:
Issues, Pull Request, Issue Comments, Pull Request Review Comments. The process validates
the HMAC signature on every delivery and rejects payloads that do not match.

**Step 5 — Obtain an operator token**:

```
POST https://<your-domain>/api/auth
Content-Type: application/json
{ "password": "<initial-operator-password>" }
```

The initial operator password is generated from `OPERATOR_SECRET_KEY` at first boot (see
the implementation's auth bootstrap procedure). The response carries a Bearer token for
subsequent control-plane API calls.

**Step 6 — Register the first repository**:

Via the PWA at `https://<your-domain>/` (repo management screen), or via the CLI:

```
orch repo add owner/repo-name
```

This calls `OrchestratorService.register_repo` with `enabled=true` and
`intake_enabled=true` and writes the `RepoConfig` to the backing database
(`ARCHITECTURE.md §4.2`).

**Step 7 — Verify**:

```
GET https://<your-domain>/readyz
```

Expected response: `{ "status": "ok", "checks": { "forge": "ok", "db": "ok", "scheduler": "ok" } }`.

```
GET https://<your-domain>/api/status
```

Expected response: one `HealthReport` per registered repo with `verdict: ON_TRACK` and
zero in-flight runs.

**Step 8 — Smoke test**:

Open a test issue on the registered repository without adding any labels. If
`intake_enabled=true` and the author is on the allowlist (or the allowlist is empty), the
triager agent should run and the issue should receive `agent-work`. If the allowlist
blocks the author, the issue appears in the PWA triage queue. Either outcome confirms
end-to-end connectivity between the forge webhook, the orchestrator process, and the
harness runtime.

---

## §7 Upgrade and Rollback

### Rolling Update

Kubernetes performs a rolling update automatically on `kubectl apply` or `helm upgrade`.
The `Deployment` default `strategy.type: RollingUpdate` with `maxUnavailable: 0` and
`maxSurge: 1` is compatible with the `minAvailable: 1` PodDisruptionBudget (`§3.7`): one
new pod starts and passes readiness before one old pod is terminated.

To trigger a rolling restart without changing configuration:

```
kubectl rollout restart deployment/orchestrator -n orchestrator
```

### Rollback

Roll back to the previous `Deployment` revision:

```
kubectl rollout undo deployment/orchestrator -n orchestrator
```

Because the image reference is a digest, rolling back restores the exact binary that was
previously running. No additional steps are required for entity state: state lives in
forge labels and is unaffected by the orchestrator restart.

If the new version introduced a schema migration (see below), consult the migration's
rollback notes before rolling back the deployment. A downward migration is required if the
old binary cannot read schema written by the new binary.

### Database Schema Migrations

The backing database (Postgres or SQLite) has a small schema: the repo registry, run
index, dedup LRU store, operator accounts, and push subscriptions (`ARCHITECTURE.md §4.2`).
Schema migrations run as a Kubernetes `initContainer` in the `Deployment` pod spec, before
the main container starts. The init container runs the migration tool (e.g.,
`alembic upgrade head` for Python, `sqlx migrate run` for Rust) against `DB_URL` from the
ConfigMap.

Because the init container runs on every pod start, migrations must be idempotent. Forward
migrations that add nullable columns or new tables are safe. Migrations that remove columns
or change column types require a two-phase approach: deploy the new binary in
backward-compatible mode first, then remove the old column in a subsequent migration after
all pods have rolled over.

### Zero-Downtime Guarantee

A rolling restart is safe mid-operation because entity state survives in forge labels. If
a pod is terminated while `Engine.dispatch`, `Engine.converge`, or `Engine.reconcile` is
in progress:

- Any entity that did not reach its next label state before termination remains in its
  previous label state.
- The reconciler, running on the first cron tick after restart (at most `RECONCILER_CRON`
  = 15 minutes later), detects the stranded entity via RC-1 through RC-4 and recovers it.

The maximum recovery latency after a rolling restart is one reconciler cron cycle
(15 minutes at the default schedule). No manual intervention is required.
