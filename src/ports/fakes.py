"""Fake in-memory port implementations for testing and dev."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime

from src.domain.types import (
    _CLOSING_RE,
    PR,
    CheckRun,
    Comment,
    DispatchContext,
    Issue,
    IssueRef,
    PRRef,
    RepoRef,
    RunConclusion,
    RunDetail,
    RunEvent,
    RunHandle,
    RunState,
    RunStatus,
    RunSummary,
    Verdict,
)

# Converge reviewer contract path — reviewer dispatches emit verdicts (SPEC §10.2).
_CONVERGE_REVIEWER_CONTRACT = "agents/converge-reviewer.md"


class SpawnDenied(Exception):
    """Raised when a spawn is attempted for a disallowed agent ref."""


# ---------------------------------------------------------------------------
# FakeForgePort
# ---------------------------------------------------------------------------


class FakeForgePort:
    """In-memory forge port for testing."""

    def _init_state(self) -> None:
        self._issues: dict[str, Issue] = {}
        self._prs: dict[str, PR] = {}
        self._comments: dict[str, list[Comment]] = {}
        self._files: dict[str, dict[str, bytes]] = {}
        self._check_runs: dict[str, list[CheckRun]] = {}
        self._mergeables: dict[str, str] = {}
        self._workflow_run_ats: dict[str, dict[str, datetime]] = {}
        self._dispatch_run_ats: dict[str, datetime] = {}
        self._changed_files: dict[str, list[str]] = {}
        self._reviews: list[dict[str, object]] = []
        self._pr_counter: dict[str, int] = {}
        self._issue_counter: dict[str, int] = {}

        # Call logs
        self.get_issue_calls: list[IssueRef] = []
        self.list_issues_calls: list[tuple[RepoRef, list[str]]] = []
        self.add_label_calls: list[tuple[IssueRef | PRRef, str]] = []
        self.remove_label_calls: list[tuple[IssueRef | PRRef, str]] = []
        self.set_labels_calls: list[tuple[IssueRef | PRRef, list[str]]] = []
        self.create_pr_calls: list[tuple[RepoRef, str, str, str, str, bool]] = []
        self.get_pr_calls: list[PRRef] = []
        self.list_prs_calls: list[tuple[RepoRef, str, list[str] | None]] = []
        self.set_pr_ready_calls: list[PRRef] = []
        self.get_changed_files_calls: list[PRRef] = []
        self.get_check_runs_calls: list[PRRef] = []
        self.get_mergeable_calls: list[PRRef] = []
        self.get_closing_issue_calls: list[PRRef] = []
        self.list_comments_calls: list[tuple[IssueRef | PRRef, datetime | None]] = []
        self.post_comment_calls: list[tuple[IssueRef | PRRef, str]] = []
        self.create_review_calls: list[tuple[PRRef, str, str]] = []
        self.create_issue_calls: list[tuple[RepoRef, str, str]] = []
        self.get_file_contents_calls: list[tuple[PRRef, str]] = []
        self.put_file_on_branch_calls: list[tuple[PRRef, str, bytes, str]] = []
        self.copy_file_on_branch_calls: list[tuple[PRRef, str, str]] = []
        self.last_workflow_run_at_calls: list[tuple[PRRef, str]] = []
        self.last_dispatch_run_at_calls: list[PRRef] = []

    def __init__(self) -> None:
        self._init_state()

    def reset(self) -> None:
        """Clear all state and call logs."""
        self._init_state()

    # --- Seeding helpers ---

    def _repo_key(self, repo: RepoRef) -> str:
        return f"{repo.owner}/{repo.name}"

    def _issue_key(self, ref: IssueRef) -> str:
        return f"{self._repo_key(ref.repo)}#{ref.number}"

    def _pr_key(self, ref: PRRef) -> str:
        return f"{self._repo_key(ref.repo)}!{ref.number}"

    def _entity_key(self, ref: IssueRef | PRRef) -> str:
        if isinstance(ref, IssueRef):
            return f"issue:{self._issue_key(ref)}"
        return f"pr:{self._pr_key(ref)}"

    def seed_issue(
        self,
        ref: IssueRef,
        *,
        labels: tuple[str, ...] | list[str] = (),
        closed: bool = False,
        title: str = "Test Issue",
        body: str = "",
        author: str = "user",
    ) -> None:
        self._issues[self._issue_key(ref)] = Issue(
            ref=ref,
            title=title,
            body=body,
            labels=list(labels),
            closed=closed,
            author=author,
        )

    def seed_pr(
        self,
        ref: PRRef,
        *,
        draft: bool = False,
        labels: tuple[str, ...] | list[str] = (),
        merged: bool = False,
        changed_files: int = 1,
        mergeable: str = "MERGEABLE",
        body: str = "",
        title: str = "Test PR",
        head_branch: str = "feature-branch",
        state: str = "open",
    ) -> None:
        key = self._pr_key(ref)
        self._prs[key] = PR(
            ref=ref,
            title=title,
            body=body,
            head_branch=head_branch,
            draft=draft,
            merged=merged,
            labels=list(labels),
            changed_files=changed_files,
            state=state,  # type: ignore[arg-type]
        )
        self._mergeables[key] = mergeable
        if key not in self._changed_files:
            self._changed_files[key] = [f"file{i}.py" for i in range(changed_files)]

    def seed_file(self, pr_ref: PRRef, path: str, content: bytes) -> None:
        key = self._pr_key(pr_ref)
        if key not in self._files:
            self._files[key] = {}
        self._files[key][path] = content

    def seed_check_run(
        self,
        pr_ref: PRRef,
        name: str,
        state: RunState,
        conclusion: RunConclusion | None = None,
    ) -> None:
        key = self._pr_key(pr_ref)
        if key not in self._check_runs:
            self._check_runs[key] = []
        self._check_runs[key].append(CheckRun(name=name, state=state, conclusion=conclusion))

    def seed_workflow_run_at(
        self,
        pr_ref: PRRef,
        workflow_name: str,
        ran_at: datetime,
    ) -> None:
        key = self._pr_key(pr_ref)
        if key not in self._workflow_run_ats:
            self._workflow_run_ats[key] = {}
        self._workflow_run_ats[key][workflow_name] = ran_at

    def seed_dispatch_run_at(self, pr_ref: PRRef, ran_at: datetime) -> None:
        key = self._pr_key(pr_ref)
        self._dispatch_run_ats[key] = ran_at

    # --- ForgePort implementation ---

    async def get_issue(self, issue_ref: IssueRef) -> Issue:
        self.get_issue_calls.append(issue_ref)
        return self._issues[self._issue_key(issue_ref)]

    async def list_issues(self, repo: RepoRef, labels: list[str]) -> list[Issue]:
        self.list_issues_calls.append((repo, labels))
        repo_prefix = self._repo_key(repo) + "#"
        result = []
        for key, issue in self._issues.items():
            if not key.startswith(repo_prefix):
                continue
            if all(lbl in issue.labels for lbl in labels):
                result.append(issue)
        return result

    async def add_label(self, entity_ref: IssueRef | PRRef, label: str) -> None:
        self.add_label_calls.append((entity_ref, label))
        if isinstance(entity_ref, IssueRef):
            key = self._issue_key(entity_ref)
            issue = self._issues[key]
            if label not in issue.labels:
                self._issues[key] = issue.model_copy(update={"labels": [*issue.labels, label]})
        else:
            key = self._pr_key(entity_ref)
            pr = self._prs[key]
            if label not in pr.labels:
                self._prs[key] = pr.model_copy(update={"labels": [*pr.labels, label]})

    async def remove_label(self, entity_ref: IssueRef | PRRef, label: str) -> None:
        self.remove_label_calls.append((entity_ref, label))
        if isinstance(entity_ref, IssueRef):
            key = self._issue_key(entity_ref)
            if key in self._issues:
                issue = self._issues[key]
                self._issues[key] = issue.model_copy(
                    update={"labels": [lbl for lbl in issue.labels if lbl != label]}
                )
        else:
            key = self._pr_key(entity_ref)
            if key in self._prs:
                pr = self._prs[key]
                self._prs[key] = pr.model_copy(
                    update={"labels": [lbl for lbl in pr.labels if lbl != label]}
                )

    async def set_labels(self, entity_ref: IssueRef | PRRef, labels: list[str]) -> None:
        self.set_labels_calls.append((entity_ref, labels))
        if isinstance(entity_ref, IssueRef):
            key = self._issue_key(entity_ref)
            issue = self._issues[key]
            self._issues[key] = issue.model_copy(update={"labels": list(labels)})
        else:
            key = self._pr_key(entity_ref)
            pr = self._prs[key]
            self._prs[key] = pr.model_copy(update={"labels": list(labels)})

    async def create_pr(
        self,
        repo: RepoRef,
        title: str,
        body: str,
        head: str,
        base: str,
        draft: bool,
    ) -> PRRef:
        self.create_pr_calls.append((repo, title, body, head, base, draft))
        repo_key = self._repo_key(repo)
        self._pr_counter[repo_key] = self._pr_counter.get(repo_key, 0) + 1
        number = self._pr_counter[repo_key]
        ref = PRRef(repo=repo, number=number)
        self.seed_pr(ref, draft=draft, body=body, title=title, head_branch=head)
        return ref

    async def get_pr(self, pr_ref: PRRef) -> PR:
        self.get_pr_calls.append(pr_ref)
        return self._prs[self._pr_key(pr_ref)]

    async def list_prs(
        self,
        repo: RepoRef,
        state: str,
        labels: list[str] | None = None,
    ) -> list[PR]:
        self.list_prs_calls.append((repo, state, labels))
        repo_prefix = self._repo_key(repo) + "!"
        result = []
        for key, pr in self._prs.items():
            if not key.startswith(repo_prefix):
                continue
            # Filter by state
            if state == "open" and pr.state != "open":
                continue
            if state == "closed" and pr.state != "closed":
                continue
            # Filter by labels (ALL must match)
            if labels and not all(lbl in pr.labels for lbl in labels):
                continue
            result.append(pr)
        return result

    async def set_pr_ready(self, pr_ref: PRRef) -> None:
        self.set_pr_ready_calls.append(pr_ref)
        key = self._pr_key(pr_ref)
        pr = self._prs[key]
        self._prs[key] = pr.model_copy(update={"draft": False})

    async def get_changed_files(self, pr_ref: PRRef) -> list[str]:
        self.get_changed_files_calls.append(pr_ref)
        key = self._pr_key(pr_ref)
        return self._changed_files.get(key, [])

    async def get_check_runs(self, pr_ref: PRRef) -> list[CheckRun]:
        self.get_check_runs_calls.append(pr_ref)
        key = self._pr_key(pr_ref)
        return list(self._check_runs.get(key, []))

    async def get_mergeable(self, pr_ref: PRRef) -> str:
        self.get_mergeable_calls.append(pr_ref)
        key = self._pr_key(pr_ref)
        return self._mergeables.get(key, "MERGEABLE")

    async def get_closing_issue(self, pr_ref: PRRef) -> IssueRef | None:
        self.get_closing_issue_calls.append(pr_ref)
        key = self._pr_key(pr_ref)
        pr = self._prs.get(key)
        if pr is None:
            return None
        # Parse nine GitHub auto-closing keyword forms (case-insensitive)
        match = _CLOSING_RE.search(pr.body)
        if match:
            return IssueRef(repo=pr_ref.repo, number=int(match.group(1)))
        return None

    async def list_comments(
        self,
        entity_ref: IssueRef | PRRef,
        since: datetime | None = None,
    ) -> list[Comment]:
        self.list_comments_calls.append((entity_ref, since))
        key = self._entity_key(entity_ref)
        comments = sorted(
            self._comments.get(key, []),
            key=lambda c: c.created_at,
        )
        if since is not None:
            comments = [c for c in comments if c.created_at >= since]
        return comments

    async def post_comment(self, entity_ref: IssueRef | PRRef, body: str) -> None:
        self.post_comment_calls.append((entity_ref, body))
        key = self._entity_key(entity_ref)
        if key not in self._comments:
            self._comments[key] = []
        comment_id = str(len(self._comments[key]) + 1)
        self._comments[key].append(
            Comment(
                id=comment_id,
                body=body,
                created_at=datetime.now(tz=UTC),
                author="orchestrator",
            )
        )

    async def create_review(self, pr_ref: PRRef, event: str, body: str) -> None:
        self.create_review_calls.append((pr_ref, event, body))
        self._reviews.append({"pr_ref": pr_ref, "event": event, "body": body})

    async def create_issue(self, repo: RepoRef, title: str, body: str) -> IssueRef:
        self.create_issue_calls.append((repo, title, body))
        repo_key = self._repo_key(repo)
        self._issue_counter[repo_key] = self._issue_counter.get(repo_key, 0) + 1
        number = self._issue_counter[repo_key]
        ref = IssueRef(repo=repo, number=number)
        self.seed_issue(ref, title=title, body=body)
        return ref

    async def get_file_contents(self, pr_ref: PRRef, path: str) -> bytes | None:
        self.get_file_contents_calls.append((pr_ref, path))
        key = self._pr_key(pr_ref)
        return self._files.get(key, {}).get(path)

    async def put_file_on_branch(
        self,
        pr_ref: PRRef,
        path: str,
        content: bytes,
        commit_message: str,
    ) -> None:
        self.put_file_on_branch_calls.append((pr_ref, path, content, commit_message))
        key = self._pr_key(pr_ref)
        if key not in self._files:
            self._files[key] = {}
        self._files[key][path] = content

    async def copy_file_on_branch(
        self,
        pr_ref: PRRef,
        src_path: str,
        dest_path: str,
    ) -> None:
        self.copy_file_on_branch_calls.append((pr_ref, src_path, dest_path))
        key = self._pr_key(pr_ref)
        files = self._files.get(key, {})
        if src_path not in files:
            raise FileNotFoundError(f"Source path {src_path!r} not found on PR {pr_ref}")
        if key not in self._files:
            self._files[key] = {}
        self._files[key][dest_path] = files[src_path]

    async def last_workflow_run_at(
        self,
        pr_ref: PRRef,
        workflow_name: str,
    ) -> datetime | None:
        self.last_workflow_run_at_calls.append((pr_ref, workflow_name))
        key = self._pr_key(pr_ref)
        return self._workflow_run_ats.get(key, {}).get(workflow_name)

    async def last_dispatch_run_at(self, pr_ref: PRRef) -> datetime | None:
        self.last_dispatch_run_at_calls.append(pr_ref)
        key = self._pr_key(pr_ref)
        return self._dispatch_run_ats.get(key)


# ---------------------------------------------------------------------------
# FakeHarnessPort
# ---------------------------------------------------------------------------

_SCRIPTED_EVENTS: list[tuple[str, dict[str, object]]] = [
    ("queued", {}),
    ("in_progress", {"message": "Starting agent"}),
    ("tool_use", {"tool": "read_file", "input": {"path": "README.md"}}),
    ("tool_use", {"tool": "bash", "input": {"command": "ls"}}),
    ("completed", {"conclusion": "success"}),
]


class FakeHarnessPort:
    """In-memory harness port for testing."""

    def __init__(
        self,
        session: FakeSessionPort | None = None,
        forge: FakeForgePort | None = None,
    ) -> None:
        self._counter = 0
        self._runs: dict[str, RunStatus] = {}
        self._event_queues: dict[str, asyncio.Queue[RunEvent | None]] = {}
        self._last_context: DispatchContext | None = None
        self._session: FakeSessionPort | None = session
        # Kept for tests that still wire a forge (e.g. comment-footer tests).
        self._forge: FakeForgePort | None = forge
        # Per-reviewer-dispatch verdicts: stored by run_id (structured-output channel).
        self._verdict_script: list[Verdict] = []
        self._verdicts: dict[str, Verdict | None] = {}
        # When True, dispatched runs stay "in_progress" (never auto-complete) so the
        # Engine's _await_run hits its CI_WAIT_S timeout — exercises the cancel path.
        self.never_completes: bool = False
        # Number of next dispatches that will time out (never complete).  Useful for
        # tests that need the Nth dispatch (e.g. a fixer run) to time out while earlier
        # reviewer runs complete normally.  Each dispatched run decrements this by 1.
        self._timeout_next_n: int = 0
        # Post-trigger CI check overrides: when trigger_ci is called and this is non-empty,
        # the checks at the given index (call count) replace the forge's current check runs.
        self._trigger_ci_check_scripts: list[list[CheckRun]] = []

        # Call logs
        self.dispatch_calls: list[DispatchContext] = []
        self.trigger_ci_calls: list[PRRef] = []
        self.trigger_workflow_calls: list[tuple[str, str, dict[str, object]]] = []
        self.cancel_calls: list[RunHandle] = []

    def script_reviewer_verdicts(self, *verdicts: Verdict) -> None:
        """Queue verdicts emitted (one per reviewer dispatch) via the structured-output channel.

        Each reviewer-contract dispatch consumes the next queued verdict and stores it
        in the per-run verdict map (``_verdicts[run_id]``) so ``get_run_verdict`` returns
        it.  When a forge is wired, the matching footer comment is also posted so tests
        that exercise the comment-footer fallback path (SPEC §8.2 rows 2–4) still work.
        """
        self._verdict_script = list(verdicts)

    def script_fixer_timeout(self, *, after_n_dispatches: int = 0) -> None:
        """Make the next dispatch (after `after_n_dispatches` normal completions) time out.

        Use this to inject a fixer timeout while earlier reviewer dispatches complete
        normally.  `after_n_dispatches=0` makes the very next dispatch time out;
        `after_n_dispatches=1` lets one dispatch complete normally then times out the next.
        """
        # We implement this as: after the next `after_n_dispatches` dispatches, the
        # following dispatch stays in_progress.  We track via _timeout_next_n.
        self._timeout_next_n = after_n_dispatches + 1

    def script_trigger_ci_checks(self, *check_lists: list[CheckRun]) -> None:
        """Script the check runs returned by the forge after each trigger_ci call.

        Each call to trigger_ci consumes one list from this script and overwrites the
        forge's check_runs for the PR.  If the script is exhausted the check runs remain
        unchanged.  Requires a wired FakeForgePort.
        """
        if self._forge is None:
            raise ValueError("script_trigger_ci_checks requires a wired FakeForgePort")
        self._trigger_ci_check_scripts = list(check_lists)

    def _emit_verdict(self, run_id: str, context: DispatchContext) -> None:
        """If this is a reviewer dispatch, store the next queued verdict by run_id.

        Stores the verdict in ``_verdicts[run_id]`` so ``get_run_verdict`` can
        return it — this is the structured-output channel (SPEC §5, §8.2).

        When a forge is wired, the matching footer comment is also posted so tests
        that exercise the comment-footer fallback path (SPEC §8.2 rows 2–4) still work.
        Non-reviewer dispatches leave ``_verdicts[run_id]`` as ``None``.
        """
        if context.contract != _CONVERGE_REVIEWER_CONTRACT or context.pr_ref is None:
            # Non-reviewer dispatch: no verdict.
            self._verdicts[run_id] = None
            return
        if not self._verdict_script:
            # Reviewer dispatch with empty script: no verdict (simulates crash).
            self._verdicts[run_id] = None
            return
        verdict = self._verdict_script.pop(0)
        self._verdicts[run_id] = verdict
        # Also post the footer comment when a forge is wired (supports fallback tests).
        if self._forge is not None and context.pr_ref is not None:
            pr_ref = context.pr_ref
            footer = (
                f"🔴 {verdict.blockers} blockers | 🟡 {verdict.suggestions} suggestions | "
                f"💬 {len(verdict.nits)} nits"
            )
            comment_key = self._forge._entity_key(pr_ref)
            self._forge._comments.setdefault(comment_key, []).append(
                Comment(
                    id=str(len(self._forge._comments.get(comment_key, [])) + 1),
                    body=f"## Converge Review\n{footer}",
                    created_at=datetime.now(tz=UTC),
                    author="converge-reviewer",
                )
            )

    def seed_run(
        self,
        handle: RunHandle,
        *,
        state: RunState,
        conclusion: RunConclusion | None = None,
    ) -> None:
        self._runs[handle.run_id] = RunStatus(state=state, conclusion=conclusion)

    async def dispatch(self, context: DispatchContext) -> RunHandle:
        self._counter += 1
        run_id = f"fake-run-{self._counter}"
        handle = RunHandle(run_id=run_id)

        self.dispatch_calls.append(context)
        self._last_context = context

        # Reviewer dispatches store their queued verdict by run_id (structured-output channel).
        self._emit_verdict(run_id, context)

        # Determine whether this dispatch should time out (stay in_progress).
        # `never_completes` is a global flag; `_timeout_next_n` is a countdown: once
        # it reaches 1, this dispatch times out and the counter is reset.
        should_timeout = self.never_completes
        if not should_timeout and self._timeout_next_n > 0:
            self._timeout_next_n -= 1
            should_timeout = self._timeout_next_n == 0

        # Default: immediately completed/success (overridable via seed_run). When
        # timeout flag is set, the run stays in_progress to force a timeout/cancel.
        if should_timeout:
            self._runs[run_id] = RunStatus(state="in_progress")
        else:
            self._runs[run_id] = RunStatus(state="completed", conclusion="success")

        # Set up event queue for SSE streaming
        queue: asyncio.Queue[RunEvent | None] = asyncio.Queue()
        self._event_queues[run_id] = queue

        # Register with session port if wired
        if self._session is not None:
            self._session._register_run(run_id, handle)

        # Emit scripted events asynchronously (fire-and-forget)
        asyncio.create_task(self._emit_events(run_id, queue))

        return handle

    async def _emit_events(
        self,
        run_id: str,
        queue: asyncio.Queue[RunEvent | None],
    ) -> None:
        """Emit scripted events into the queue, then signal completion."""
        for event_type, data in _SCRIPTED_EVENTS:
            event = RunEvent(
                event_type=event_type,
                data=data,
                timestamp=datetime.now(tz=UTC),
            )
            await queue.put(event)
            if self._session is not None:
                self._session._append_event(run_id, event)
        # Signal end
        await queue.put(None)

    def simulate_spawn_attempt(self, agent_ref: str) -> None:
        """Raise SpawnDenied if agent_ref is not in allowed_agent_refs."""
        if self._last_context is None:
            return
        allowed = self._last_context.allowed_agent_refs
        if allowed is not None and agent_ref not in allowed:
            raise SpawnDenied(f"Agent {agent_ref!r} not in allowed_agent_refs: {allowed}")

    async def trigger_workflow(
        self,
        name: str,
        ref: str,
        inputs: dict[str, object],
    ) -> None:
        self.trigger_workflow_calls.append((name, ref, inputs))

    async def trigger_ci(self, pr_ref: PRRef) -> None:
        self.trigger_ci_calls.append(pr_ref)
        # Apply next scripted check list (if any) to the wired forge.
        if self._forge is not None and self._trigger_ci_check_scripts:
            checks = self._trigger_ci_check_scripts.pop(0)
            key = self._forge._pr_key(pr_ref)
            self._forge._check_runs[key] = list(checks)

    async def get_run_status(self, handle: RunHandle) -> RunStatus:
        return self._runs.get(
            handle.run_id,
            RunStatus(state="queued"),
        )

    async def get_run_verdict(self, handle: RunHandle) -> Verdict | None:
        """Return the verdict stored for this run (structured-output channel, SPEC §5).

        Returns None for non-reviewer dispatches or when no verdict was queued
        (simulates a reviewer crash — engine treats None as "unknown" blockers).
        """
        return self._verdicts.get(handle.run_id)

    async def cancel(self, handle: RunHandle) -> None:
        # Idempotent — any already-terminal run (any conclusion) is a no-op (SPEC §9.2)
        existing = self._runs.get(handle.run_id)
        if existing is not None and existing.state == "completed":
            return
        self._runs[handle.run_id] = RunStatus(state="completed", conclusion="cancelled")
        self.cancel_calls.append(handle)


# ---------------------------------------------------------------------------
# FakeSessionPort
# ---------------------------------------------------------------------------


class FakeSessionPort:
    """In-memory session port for testing."""

    def _init_state(self) -> None:
        self._summaries: dict[str, RunSummary] = {}
        self._events: dict[str, list[RunEvent]] = {}
        self._statuses: dict[str, str] = {}
        self._event_queues: dict[str, asyncio.Queue[RunEvent | None]] = {}

        # Call logs
        self.list_runs_calls: list[tuple[RepoRef, datetime | None, str | None, str | None]] = []
        self.get_run_calls: list[str] = []
        self.stream_events_calls: list[str] = []
        self.cancel_calls: list[str] = []
        self.intervene_calls: list[tuple[str, str]] = []

    def __init__(self) -> None:
        self._init_state()

    def reset(self) -> None:
        self._init_state()

    def seed_run_summary(
        self,
        run_id: str,
        repo: RepoRef,
        type: str,
        status: str,
        started_at: datetime,
        completed_at: datetime | None = None,
        events: list[RunEvent] | None = None,
    ) -> None:
        self._summaries[run_id] = RunSummary(
            run_id=run_id,
            repo=repo,
            type=type,
            status=status,
            started_at=started_at,
            completed_at=completed_at,
        )
        self._statuses[run_id] = status
        self._events[run_id] = events if events is not None else []

    def _register_run(self, run_id: str, handle: RunHandle) -> None:
        """Called by FakeHarnessPort when a dispatch happens."""

        repo = RepoRef(owner="demo", name="repo")
        self._summaries[run_id] = RunSummary(
            run_id=run_id,
            repo=repo,
            type="issues",
            status="queued",
            started_at=datetime.now(tz=UTC),
        )
        self._statuses[run_id] = "queued"
        self._events[run_id] = []
        queue: asyncio.Queue[RunEvent | None] = asyncio.Queue()
        self._event_queues[run_id] = queue

    def _append_event(self, run_id: str, event: RunEvent) -> None:
        """Called by FakeHarnessPort to deliver events."""
        if run_id not in self._events:
            self._events[run_id] = []
        self._events[run_id].append(event)
        # Also push to the queue if streaming
        if run_id in self._event_queues:
            self._event_queues[run_id].put_nowait(event)
            if event.event_type == "completed":
                self._event_queues[run_id].put_nowait(None)
                self._statuses[run_id] = "completed"

    async def list_runs(
        self,
        repo: RepoRef,
        since: datetime | None = None,
        status: str | None = None,
        type: str | None = None,
    ) -> list[RunSummary]:
        self.list_runs_calls.append((repo, since, status, type))
        result = []
        for summary in self._summaries.values():
            if summary.repo.owner != repo.owner or summary.repo.name != repo.name:
                continue
            if since is not None and summary.started_at < since:
                continue
            if status is not None and summary.status != status:
                continue
            if type is not None and summary.type != type:
                continue
            result.append(summary)
        return result

    async def get_run(self, run_id: str) -> RunDetail:
        self.get_run_calls.append(run_id)
        summary = self._summaries[run_id]
        events = list(self._events.get(run_id, []))
        return RunDetail(
            run_id=summary.run_id,
            repo=summary.repo,
            type=summary.type,
            status=self._statuses.get(run_id, summary.status),
            started_at=summary.started_at,
            completed_at=summary.completed_at,
            events=events,
        )

    async def stream_events(self, run_id: str) -> AsyncGenerator[RunEvent, None]:  # noqa: UP007
        # Single-consumer only: drains the shared queue including its terminating None;
        # a second concurrent subscriber on the same run_id would block.
        self.stream_events_calls.append(run_id)
        # If there's a live queue (wired from harness), drain it
        if run_id in self._event_queues:
            queue = self._event_queues[run_id]
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield event
        else:
            # Yield from stored events
            for event in self._events.get(run_id, []):
                yield event

    async def cancel(self, run_id: str) -> None:
        self.cancel_calls.append(run_id)
        self._statuses[run_id] = "cancelled"
        if run_id in self._summaries:
            summary = self._summaries[run_id]
            self._summaries[run_id] = summary.model_copy(update={"status": "cancelled"})

    async def intervene(self, run_id: str, message: str) -> None:
        self.intervene_calls.append((run_id, message))


# ---------------------------------------------------------------------------
# FakeCounterStore
# ---------------------------------------------------------------------------


class FakeCounterStore:
    """In-memory atomic per-entity, per-channel counter store (SPEC §8.2a)."""

    def _init_state(self) -> None:
        self._counts: dict[tuple[str, str], int] = {}
        self._lock = asyncio.Lock()

        # Call logs
        self.get_count_calls: list[tuple[IssueRef | PRRef, str]] = []
        self.increment_calls: list[tuple[IssueRef | PRRef, str]] = []
        self.reset_calls: list[tuple[IssueRef | PRRef, str]] = []

    def __init__(self) -> None:
        self._init_state()

    def reset_state(self) -> None:
        """Clear all counters and call logs (distinct from the async `reset` method)."""
        self._init_state()

    def _key(self, entity_ref: IssueRef | PRRef, channel: str) -> tuple[str, str]:
        if isinstance(entity_ref, IssueRef):
            ref_key = f"issue:{entity_ref.repo.owner}/{entity_ref.repo.name}#{entity_ref.number}"
        else:
            ref_key = f"pr:{entity_ref.repo.owner}/{entity_ref.repo.name}!{entity_ref.number}"
        return (ref_key, channel)

    def seed_count(self, entity_ref: IssueRef | PRRef, channel: str, value: int) -> None:
        self._counts[self._key(entity_ref, channel)] = value

    async def get_count(self, entity_ref: IssueRef | PRRef, channel: str) -> int:
        self.get_count_calls.append((entity_ref, channel))
        return self._counts.get(self._key(entity_ref, channel), 0)

    async def increment(self, entity_ref: IssueRef | PRRef, channel: str) -> int:
        self.increment_calls.append((entity_ref, channel))
        async with self._lock:
            key = self._key(entity_ref, channel)
            new_value = self._counts.get(key, 0) + 1
            self._counts[key] = new_value
            return new_value

    async def reset(self, entity_ref: IssueRef | PRRef, channel: str) -> None:
        self.reset_calls.append((entity_ref, channel))
        self._counts[self._key(entity_ref, channel)] = 0


# ---------------------------------------------------------------------------
# FakeConvergeStateStore
# ---------------------------------------------------------------------------


class FakeConvergeStateStore:
    """In-memory per-PR converge loop state store (SPEC §9.4)."""

    def _init_state(self) -> None:
        self._rounds: dict[str, int] = {}
        self._round_starts: dict[str, datetime] = {}
        self._run_handles: dict[str, RunHandle] = {}

        # Call logs
        self.get_converge_round_calls: list[PRRef] = []
        self.set_converge_round_calls: list[tuple[PRRef, int]] = []
        self.get_round_started_calls: list[PRRef] = []
        self.set_round_started_calls: list[tuple[PRRef, datetime]] = []
        self.clear_calls: list[PRRef] = []
        self.get_last_run_handle_calls: list[PRRef] = []
        self.set_last_run_handle_calls: list[tuple[PRRef, RunHandle]] = []

    def __init__(self) -> None:
        self._init_state()

    def reset(self) -> None:
        self._init_state()

    def _key(self, pr_ref: PRRef) -> str:
        return f"{pr_ref.repo.owner}/{pr_ref.repo.name}!{pr_ref.number}"

    def seed_round(self, pr_ref: PRRef, round: int) -> None:
        self._rounds[self._key(pr_ref)] = round

    def seed_round_started(self, pr_ref: PRRef, started: datetime) -> None:
        self._round_starts[self._key(pr_ref)] = started

    async def get_converge_round(self, pr_ref: PRRef) -> int:
        self.get_converge_round_calls.append(pr_ref)
        return self._rounds.get(self._key(pr_ref), 0)

    async def set_converge_round(self, pr_ref: PRRef, round: int) -> None:
        self.set_converge_round_calls.append((pr_ref, round))
        self._rounds[self._key(pr_ref)] = round

    async def get_round_started(self, pr_ref: PRRef) -> datetime | None:
        self.get_round_started_calls.append(pr_ref)
        return self._round_starts.get(self._key(pr_ref))

    async def set_round_started(self, pr_ref: PRRef, started: datetime) -> None:
        self.set_round_started_calls.append((pr_ref, started))
        self._round_starts[self._key(pr_ref)] = started

    async def clear_converge_state(self, pr_ref: PRRef) -> None:
        self.clear_calls.append(pr_ref)
        key = self._key(pr_ref)
        self._rounds.pop(key, None)
        self._round_starts.pop(key, None)
        self._run_handles.pop(key, None)

    async def get_last_run_handle(self, pr_ref: PRRef) -> RunHandle | None:
        self.get_last_run_handle_calls.append(pr_ref)
        return self._run_handles.get(self._key(pr_ref))

    async def set_last_run_handle(self, pr_ref: PRRef, handle: RunHandle) -> None:
        self.set_last_run_handle_calls.append((pr_ref, handle))
        self._run_handles[self._key(pr_ref)] = handle


# ---------------------------------------------------------------------------
# FakeLockProvider
# ---------------------------------------------------------------------------


class FakeLockProvider:
    """In-memory advisory lock provider for testing (SPEC §11.3).

    Uses ``asyncio.Lock`` per entity key — same semantics as ``AsyncioLockProvider``
    but records lock acquisition / release counts for assertion in tests.
    """

    def _entity_key(self, entity_ref: IssueRef | PRRef) -> str:
        repo = entity_ref.repo
        base = f"{repo.owner}/{repo.name}"
        if isinstance(entity_ref, IssueRef):
            return f"issue:{base}#{entity_ref.number}"
        return f"pr:{base}!{entity_ref.number}"

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._meta_lock = asyncio.Lock()
        # Call log: each entry is the entity key at the time of acquisition
        self.acquired: list[str] = []
        self.released: list[str] = []

    def reset(self) -> None:
        self._locks = {}
        self.acquired = []
        self.released = []

    def lock(self, entity_ref: IssueRef | PRRef) -> AbstractAsyncContextManager[None]:
        return self._acquire(entity_ref)

    @asynccontextmanager
    async def _acquire(self, entity_ref: IssueRef | PRRef) -> AsyncGenerator[None, None]:
        key = self._entity_key(entity_ref)
        async with self._meta_lock:
            if key not in self._locks:
                self._locks[key] = asyncio.Lock()
            entity_lock = self._locks[key]
        async with entity_lock:
            self.acquired.append(key)
            try:
                yield
            finally:
                self.released.append(key)
