# AGENT_PACK.md — Specialist Agent Provenance and Selection

**Version**: 1.0
**Date**: 2026-06-20
**Status**: Draft
**Depends on**: `API.md` 1.0, `THREAT_MODEL.md` 1.0, `DEPLOYMENT.md` 1.0

---

## §1 Two-Tier Agent Architecture

The orchestrator uses **two distinct tiers of agents**. Confusing them is the single most
common misunderstanding; state it plainly here:

| Tier | Where defined | Who authors | How referenced | Examples |
|---|---|---|---|---|
| **Orchestration agents** | `agents/*.md` in this repo | Us (in this spec) | By contract file path | `agents/converge-reviewer.md`, `agents/orchestrator.md` |
| **Specialist pack** | External versioned repo (see §2) | The pack maintainer | By `AgentRef` (flat filename) | `engineering-security-engineer.md`, `engineering-code-reviewer.md` |

**We do not author specialist agents.** The specialist content is an external dependency, pinned and
fetched exactly like a dependency in a package manager. The orchestration agents call into the
specialist pack; they do not contain specialist logic themselves.

The local `~/.claude/agents/` directory on an operator's workstation and the specialists used in CI
**are the same versioned content** — both come from the same upstream pack repo at the same pinned
SHA. The local pack is a developer convenience; CI fetches the pack fresh at build time.

---

## §2 The Specialist Pack

### §2.1 Upstream source

The canonical specialist agent pack is maintained at:

```
https://github.com/msitarzewski/agency-agents
```

It organizes specialists into category subdirectories (e.g. `engineering/`, `design/`, `testing/`,
`product/`, `specialized/`, …). Every specialist is a single Markdown file with YAML frontmatter
(`name`, `description`, `color`, `emoji`) followed by a system-prompt body. The orchestrator treats
these files as opaque prompt templates — it does not parse their content, only references them.

### §2.2 `AgentPackConfig`

The source and version of the specialist pack, stored in `Config` (`API.md §8.2`):

```
AgentPackConfig {
  repo_url:  string   # Git-clonable HTTPS URL of the pack repo
  pinned_ref: string  # Full SHA (preferred) or tag to checkout after cloning
  dest_dir:  string = ".agents"   # directory relative to workspace root where pack is flattened
}
```

**Defaults** (used when no override is provided):

```
AgentPackConfig {
  repo_url:   "https://github.com/msitarzewski/agency-agents"
  pinned_ref: "d6553e261e595c651064f899a6c33dd5aa71c9e3"
  dest_dir:   ".agents"
}
```

Operators may override `repo_url` to point at a private fork under their own org — this is the
recommended posture for full supply-chain control (see §5.1). Any fork must maintain the same
flat-file layout in the `dest_dir` after acquisition.

### §2.3 `AgentRef`

The canonical name for a specialist agent within the flattened pack:

```
AgentRef  — a basename string (e.g. "engineering-security-engineer.md")
```

An `AgentRef` is a **flat filename** — just the basename, no directory path prefix. After
acquisition (§3), every specialist is addressable as `<dest_dir>/<AgentRef>`.

`AgentRef` values are defined by the pack upstream. The routing table (`API.md §2`
`SPECIALIST_ROUTING`) maps diff-path patterns to `AgentRef` values.

---

## §3 Pack Acquisition — Bake at Build

The specialist pack is **baked into the container image at build time**. It is not fetched at
container startup, and it is not mounted from a volume. This is the default and recommended
mode — it minimizes the runtime supply-chain surface and makes the image fully reproducible.

### §3.1 Acquisition procedure

The Dockerfile build stage clones the pack at the pinned ref and flattens it:

```dockerfile
# In the builder stage:
RUN git clone --no-tags --depth 1 ${AGENT_PACK_REPO_URL} /tmp/agency-agents \
 && git -C /tmp/agency-agents fetch --depth 1 origin ${AGENT_PACK_PINNED_REF} \
 && git -C /tmp/agency-agents checkout ${AGENT_PACK_PINNED_REF} \
 && mkdir -p /app/${AGENT_PACK_DEST_DIR} \
 && find /tmp/agency-agents -mindepth 2 -name "*.md" -exec cp {} /app/${AGENT_PACK_DEST_DIR}/ \; \
 && rm -rf /tmp/agency-agents
```

`AGENT_PACK_REPO_URL`, `AGENT_PACK_PINNED_REF`, and `AGENT_PACK_DEST_DIR` are Docker build args,
passed from `AgentPackConfig`. The flattened pack ends up at `/app/.agents/*.md` inside the image.

Details of the Dockerfile and build pipeline are in `DEPLOYMENT.md §2`.

### §3.2 SBOM and provenance

The pinned SHA is recorded as a build argument in the image SBOM and as an OCI image annotation:

```
org.opencontainers.image.agent-pack.source  = <repo_url>
org.opencontainers.image.agent-pack.ref     = <pinned_ref>
```

A SHA bump — updating `AgentPackConfig.pinned_ref` to a newer reviewed commit — triggers a full
image rebuild and a new SBOM entry. Do not change the pinned ref without reviewing the diff:

```
https://github.com/msitarzewski/agency-agents/compare/<old_sha>...<new_sha>
```

Refer to `DEPLOYMENT.md §2.2` for the full image provenance workflow.

### §3.3 What is NOT done

- **No runtime clone.** The image does not clone the pack at startup. No outbound network access to
  the pack repo is needed at runtime.
- **No init-container fetch.** The init-container/sidecar fetch pattern was considered and rejected
  for supply-chain surface reasons.
- **No volume mount.** Operators should not mount a local pack directory into the container; it
  bypasses the baked-in provenance and SBOM.

---

## §4 Specialist Selection — `decide_specialists`

### §4.1 Selection logic

`decide_specialists` is the **pure synchronous** function that maps a PR's diff to the set of
specialist `AgentRef`s to spawn in a converge review round. It is defined as `API.md §3.12`.

```
decide_specialists(changed_paths: list<string>, round: int) -> list<AgentRef>
```

**Algorithm:**

1. Start with the always-on base set (§4.2).
2. For each entry in `SPECIALIST_ROUTING` (`API.md §2`), test whether any path in
   `changed_paths` matches the entry's glob pattern.
3. For each matching entry, add its `AgentRef` to the result (if not already present).
4. Deduplicate (a base-set specialist that also matches a routing entry is included once).
5. Cap the result at `PARALLEL_SPECIALIST_CAP = 4` (`API.md §2` Constants). When capping,
   the base set is always retained; routing-added specialists are dropped (in definition
   order) to respect the cap.

`round` is passed but currently unused by the selection logic. It is reserved so that future
extensions can suppress certain specialist tiers in later rounds (e.g., skip performance
reviewer in R3).

### §4.2 Always-on base set

Two specialists run on **every** converge review round, unconditionally:

| Role | AgentRef |
|---|---|
| Security reviewer | `engineering-security-engineer.md` |
| Code quality reviewer | `engineering-code-reviewer.md` |

These two are required because: (a) security findings must be present in every round
regardless of diff content, and (b) code quality / missing-tests detection is the primary
automated gate for the converge loop.

The base set occupies 2 of the 4 cap slots. Routing-added specialists may fill the remaining
2 slots.

### §4.3 Routing table (see also `API.md §2 SPECIALIST_ROUTING`)

| Glob pattern(s) | Specialist `AgentRef` | When to add |
|---|---|---|
| `auth/**`, `session/**`, `crypto/**`, `**/permission*`, `**/rbac*` | `engineering-security-engineer.md` | (already in base) |
| `**/migrations/**`, `**/*.sql`, `**/schema*` | `engineering-database-optimizer.md` | DB/schema changes |
| `**/*.tsx`, `**/*.css`, `**/components/**`, `**/ui/**` | `testing-accessibility-auditor.md` | UI/frontend changes |
| `**/api/**`, `**/routes/**`, `**/handlers/**` | `testing-api-tester.md` | API endpoint changes |

The routing table is **additive only**. A specialist not in the routing table is never
automatically added; it may be spawned manually by a converge-reviewer agent in exceptional
circumstances (e.g., a spec explicitly requires a documentation reviewer for every API change).

### §4.4 Spawning — the "act as" model

Each specialist is spawned by an orchestration agent (e.g. `agents/converge-reviewer.md`)
using the generic `subagent_type: "general-purpose"` pattern:

```
Agent(
  subagent_type: "general-purpose",
  prompt: """
    Act as the agent defined in <dest_dir>/<AgentRef>. Read that file first.

    <task-specific instructions>
  """
)
```

Key constraints on spawning:

- **`subagent_type` is always `"general-purpose"`.** Specialists are not registered agent
  types. Their identity is data (a prompt file), not a runtime type.
- **Depth-1 only.** Specialists do not spawn further sub-agents. The call depth from an
  orchestration agent is: orchestration agent → specialist (depth 1). Specialists terminate
  after producing their output.
- **Blocking/synchronous per agent.** Each specialist run is awaited before the orchestration
  agent reads its output. The *set* of specialists in a round is dispatched in parallel (up to
  the cap), but each individual specialist call blocks until that agent finishes.
- **Cap `PARALLEL_SPECIALIST_CAP = 4` applies across the full spawned set**, including the
  base set. Exceeding the cap is a contract violation.

See `agents/converge-reviewer.md §1` for the exact invocation pattern.

---

## §5 Trust and Supply-Chain Controls

### §5.1 Pinned SHA discipline

The `pinned_ref` in `AgentPackConfig` must be a **full 40-character commit SHA**. Tags are
allowed as a convenience, but the verification step in `DEPLOYMENT.md §2` must dereference
the tag to a SHA and record the SHA in the SBOM.

To bump the pinned SHA:

1. Review the diff at `https://github.com/msitarzewski/agency-agents/compare/<old>...<new>`.
2. Confirm that no specialist definition introduces prompt-injection vectors or attempts to
   exfiltrate context.
3. Update `AgentPackConfig.pinned_ref` and regenerate the image. The SHA bump appears in the
   image changelog and SBOM.

A fork of the pack under the operator's own organization gives the operator full diff review
on every update before it reaches the running image. This is the recommended approach for
production deployments.

### §5.2 `PROTECTED_PATHS` and the pack directory

**`.agents/**` is a `PROTECTED_PATHS` entry** (`API.md §2`). Any PR that modifies, adds, or
removes a file under the pack directory (including the custom orchestration agents under
`agents/`) triggers an **E1 escalation** before any specialist runs. This closes the
agent-pack poisoning vector: a malicious PR cannot override a specialist definition to alter
review behavior.

The protected-path check is performed by `Engine.converge` before round 1 and by the security
reviewer in every round (defense in depth). See `THREAT_MODEL.md §2 T5` and `T8`.

### §5.3 Specialist-content trust boundary

Specialist prompt files are **treated as trusted system prompts** by the harness, not as
user-controlled input. This means:

- The pack directory must not be writable by the agent sandbox at runtime.
- Contributor-supplied text (issue body, PR body, comments) must never be interpolated
  directly into a specialist file path or the `AgentRef` string. `AgentRef`s come only from
  `decide_specialists` output, which derives from `SPECIALIST_ROUTING` (a hardcoded constant).
- Operators who fork the pack must apply the same review discipline to specialist content
  that they apply to the orchestration agents.

---

## §6 Updating the Pack

### §6.1 SHA bump process

1. Identify the new SHA to adopt.
2. Review the diff (see §5.1).
3. Update `AgentPackConfig.pinned_ref` in the deployment configuration.
4. Rebuild the image; the pack acquisition step (§3.1) fetches the new SHA.
5. The new SHA appears in the image SBOM and OCI annotations.
6. Deploy using the standard rolling upgrade procedure (`DEPLOYMENT.md §7`).

### §6.2 Forking the pack

An operator who maintains a private fork of the pack:

1. Mirrors the upstream pack repo into their org.
2. Reviews and merges upstream updates through their own PR/review process.
3. Sets `AgentPackConfig.repo_url` to the fork URL.
4. Continues to use a pinned SHA from the fork.

The orchestrator has no hardcoded reference to the upstream URL beyond the default value in
`AgentPackConfig`. Switching to a fork is a single config change.

---

## Cross-References

- `API.md §2` — `AgentPackConfig`, `AgentRef`, `SPECIALIST_ROUTING`, `CONVERGE_REVIEW_BASE`,
  `PARALLEL_SPECIALIST_CAP`, `PROTECTED_PATHS` (includes `.agents/**`)
- `API.md §3.12` — `decide_specialists` signature and truth table
- `API.md §4.2` — `HarnessPort`; specialist spawn note
- `API.md §8.2` — `Config.agent_pack: AgentPackConfig`
- `ARCHITECTURE.md §2` — Two-tier agent model in component map
- `THREAT_MODEL.md §2 T5` — Supply-chain threat; pack-poisoning vector; pinned-SHA mitigation
- `THREAT_MODEL.md §2 T8` — Agent-pack tampering via PR; PROTECTED_PATHS defense
- `DEPLOYMENT.md §2` — Container image build; pack acquisition step; SBOM/provenance
- `TESTING.md §2.12` — `decide_specialists` truth table test suite
- `TESTING.md §5` — Pack-acquisition contract test; `.agents/**` protected-path trust test
- `agents/converge-reviewer.md §1` — Exact invocation pattern for specialist spawning
- `agents/converge-fixer.md §2` — AgentRef-based blocker routing
