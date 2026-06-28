# Doctrine

*A doctrine earns the name by surviving review: a shipper, an inspector, and a cynic each
try to burn it, and what none of them can burn is what remains — fitting, for a project
whose method is to review a thing until only the true part is left.*

---

## The Kernel — the injectable unit

> This is the operative core of the doctrine, kept terse on purpose: it is what gets
> reincorporated into the agent contracts (`agents/*.md`), each agent carrying only the
> slice that bears on its job. Everything below the Kernel is the human-facing canon — the
> *why*, kept out of the agent payload. The contracts quote from here; this is the source.

**The human's attention is the real safety mechanism. Your job is to extend it, not fake it.**

1. **Honesty over objections.** Calibrate severity to the artifact's real purpose. Never
   block on a finding that exists only because of your own prior demand; after round 1, only
   genuine defects block. A gate that manufactures work is worse than none.
2. **Don't act blind.** Never fix what you can't see. Surface errors — no fire-and-forget
   without error egress; make a failure observable before you change it.
3. **Respect the bounds.** Use the named constants (never hardcoded literals); honor the
   round / timeout / cooldown ordering.
4. **Know when to stop and ask.** Every path ends at `agent:ready` or `needs-human`.
   Escalate honestly rather than fake-green — you are fast, and you can be confidently wrong.

---

## The thesis

The system survives because a human is awake, reading source. Not because the architecture
holds the line — the architecture is what strands the work, swallows the errors, and
guillotines runs at the timeout. The architecture creates the conditions; a person holds
the line.

That sentence belongs at the top, because every principle below exists for a single
purpose: **to make that person's attention reach further — never to pretend it can be
replaced.** A doctrine for autonomous systems that forgets the irreducible human is just
the story the machine tells about itself.

What follows is four principles. Two are backed by automated gates in this repository — and
a gate catches the coarsest violation, not the spirit, so "backed by a gate" is not the
same as "enforced." One cannot be backed by anything at all — and that is the most important
one.

---

## 1. Guard the reviewer's honesty above all else.

A gate that manufactures work is worse than no gate. A reviewer that demands a
Content-Security-Policy on a static single-file demo, then blocks the pull request because
that very policy lacks a sub-directive, generates its own blocking condition and grinds
real work to `needs-human` in an infinite regress.

This is the one failure mode no architecture can recover from. A supervisor can restart a
stranded run; nothing can exit a loop whose exit condition the reviewer keeps inventing.
Calibrate severity to the artifact's true purpose. Never raise a blocker that exists only
because of your own prior demand. After the first round, only genuine defects block. This
is the same ethic the parent project points outward — `mirror` rewrites a person's story
without fabricating it — turned inward at the machine. Honesty in assessment is what
decides whether the loop converges or eats itself.

*Partially enforced* by the converge-reviewer contract (`agents/converge-reviewer.md`); the
rest is judgment, and there is no test for judgment.

## 2. You may not fix what you cannot see.

Shipping a fix for a hypothesized cause while the real failure is invisible — an exception
swallowed by a fire-and-forget boundary whose done-callback logs where no one is watching —
cannot work; it is a guess wearing a deploy's clothes. Autonomy is supervision at a
distance, and supervision is impossible without observation, so error *egress* must be
designed with the same care as the happy path.

*Gate-assisted* by **`tests/unit/test_doctrine_error_egress.py`**: every `asyncio.create_task`
in production code must register error egress (`add_done_callback`) or be a lifecycle task
owned on `self` and cancelled on shutdown. A background task whose exception vanishes is,
operationally, a task that never fails — and never runs. The word is *gate-assisted*, not
*enforced*, deliberately: the gate proves egress is registered, not that the callback (or a
self-owned loop) actually surfaces the error. See "Known gaps in the gates."

*The caveat at the heart of the whole document:* this mechanism flags a missing callback; it
cannot make anyone read the logs it enables. The observability requirement can exist and
still be violated. Enforcement needs a human to notice when enforcement is missing. The
gate buys attention reach, not attention itself.

## 3. Bound everything — and then order the bounds.

Three converge rounds. One reconverge. Redispatch caps, run deadlines, harness cooldowns,
stale-draft thresholds. Autonomy without bounds is an expensive way to loop forever. But
bounds *interact*: the run deadline must sit below the backend safety timeout and below the
stale-draft threshold, or the supervisor reaps a run that is still legitimately working.
That relationship is part of the design and must be stated, not re-derived by whoever next
edits a number.

*Gate-assisted* by **`tests/unit/test_doctrine_timeout_ordering.py`**: asserts
`POLL_INTERVAL_S ≪ CI_WAIT_S < _K8S_JOB_TIMEOUT_S < STALE_DRAFT_THRESHOLD_S`. An invariant
that lives only in someone's head is one a single edit can break. The gate proves the
constants are ordered, not that every call site uses them by name — see "Known gaps."

## 4. The terminal authority is human — and this one cannot be automated.

Every path through the machine ends in one of two places: `agent:ready` (a human merges) or
`needs-human` (a human decides). The pipeline is built, end to end, to know when to stop and
ask — because its own components assert false things with total, fluent confidence: a
diagnostic agent will confabulate a statement no one made and shell into production
unprompted.

You can test that every decision path eventually emits one of those two labels. You cannot
test that the human who sees `needs-human` exercises real judgment instead of
rubber-stamping. That quality is not a CI gate; it is the irreducible thing from the thesis,
wearing a label. It is the most important principle precisely because it is the one nothing
in this repo can enforce.

*Where to start when you see it:* open the pull request's converge-review comments first —
the per-round footers say what blocked, and the adjudicator's verdict says why it gave up —
then the failing run's events and the control-plane log. The label tells you to look; those
three tell you where. "A human reading source" is the thesis; this is the first source to
read.

---

## Known gaps in the gates

The two gates in Principles 2 and 3 catch the coarsest violations and nothing subtler. Named
here so "gate-assisted" is never misread as "enforced," and so the holes stay documented
rather than discovered:

- **Egress gate proves registration, not surfacing.** It confirms an `add_done_callback`
  exists for a task; it does not read the callback. A callback that discards the exception
  (`lambda t: tasks.discard(t)`) passes. Likewise a `self`-owned lifecycle task is exempt,
  but its loop can still swallow exceptions internally — the reconciler loop did exactly
  this until it was fixed to log. The exemption is sound only if the loop surfaces its own
  errors, and nothing checks that.
- **Egress exemption is filename-keyed.** `fakes.py` is skipped by name; a future
  `*_fakes.py` would not be, and is not the same class of thing.
- **Ordering gate proves order, not usage.** It asserts the four constants are correctly
  ordered; it does not assert call sites reference the symbols rather than hardcoded
  literals. A pasted `1800` drifts silently past it.
- **Principle 1 has no gate at all.** "There is no test for judgment" is true, but the
  *specific* pathology it names — a reviewer raising the same blocker signature in
  consecutive rounds — has a detectable shape. A same-signature-blocked-twice detector would
  be a test for *that* failure mode. It does not exist yet.

These are tickets, not blockers. A first line that catches the bare `create_task` and the
drifted constant is worth keeping even while it cannot reach the spirit — provided it is
never described as reaching the spirit.

---

## Appendix A — Architecture, not commandments

These are sound *engineering*, not hard-won *conviction*. They describe how the machine is
built and are worth keeping — as design facts, not scripture.

- **Spec-as-cast.** Behavior lives in truth tables; the implementation is a replaceable cast
  (bash in `mirror`, Python here, "binding on the port"). The protection extends only to
  what the spec actually contains — a timeout-ordering invariant absent from the spec is
  exactly why Principle 3 is a test.
- **State in the world.** Labels-as-state, no separate store; a ninety-second ephemeral agent
  joins a long-lived process and survives restarts. The price — drift, races, stranding — is
  why a whole supervisor loop exists.
- **Pure decisions, effects at the edges.** The legible core; bugs are forced to the boundary
  where a flashlight reaches.
- **Three loops and a supervisor.** Dispatch, converge, reconciler. The reconciler is an
  honest admission that autonomy strands things — a penance schedule.
- **Tier the labor.** Opus judges, Sonnet labors, Haiku polishes. Economics and a cheaply
  bought adversary, not a creed.

## Appendix B — Origins

This pipeline is the scaffolding that built `mirror` — a tool to help a person present their
authentic professional self — pried loose and rebuilt as its own product. The romantic
reading: *build your automation as if it will outlive its first purpose, because the
discipline that makes automation trustworthy is the same that makes anything worth keeping.*
The honest reading: it is code reuse plus a deadline, blessed in hindsight.

Both are true. Keep the romance on a sticky note. Keep the honesty here — because a doctrine
that cannot admit its own founding myth has already broken Principle 1.
