"""Abstract port interfaces (Protocol classes)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Protocol, runtime_checkable

from src.domain.types import (
    PR,
    CheckRun,
    Comment,
    DispatchContext,
    Issue,
    IssueRef,
    PRRef,
    RepoRef,
    RunDetail,
    RunEvent,
    RunHandle,
    RunStatus,
    RunSummary,
)


@runtime_checkable
class ForgePort(Protocol):
    async def get_issue(self, issue_ref: IssueRef) -> Issue: ...

    async def list_issues(self, repo: RepoRef, labels: list[str]) -> list[Issue]: ...

    async def add_label(self, entity_ref: IssueRef | PRRef, label: str) -> None: ...

    async def remove_label(self, entity_ref: IssueRef | PRRef, label: str) -> None: ...

    async def set_labels(self, entity_ref: IssueRef | PRRef, labels: list[str]) -> None: ...

    async def create_pr(
        self,
        repo: RepoRef,
        title: str,
        body: str,
        head: str,
        base: str,
        draft: bool,
    ) -> PRRef: ...

    async def get_pr(self, pr_ref: PRRef) -> PR: ...

    async def list_prs(
        self,
        repo: RepoRef,
        state: str,
        labels: list[str] | None = None,
    ) -> list[PR]: ...

    async def set_pr_ready(self, pr_ref: PRRef) -> None: ...

    async def get_changed_files(self, pr_ref: PRRef) -> list[str]: ...

    async def get_check_runs(self, pr_ref: PRRef) -> list[CheckRun]: ...

    async def get_mergeable(self, pr_ref: PRRef) -> str: ...

    async def get_closing_issue(self, pr_ref: PRRef) -> IssueRef | None: ...

    async def list_comments(
        self,
        entity_ref: IssueRef | PRRef,
        since: datetime | None = None,
    ) -> list[Comment]: ...

    async def post_comment(self, entity_ref: IssueRef | PRRef, body: str) -> None: ...

    async def create_review(self, pr_ref: PRRef, event: str, body: str) -> None: ...

    async def create_issue(self, repo: RepoRef, title: str, body: str) -> IssueRef: ...

    async def get_file_contents(self, pr_ref: PRRef, path: str) -> bytes | None: ...

    async def put_file_on_branch(
        self,
        pr_ref: PRRef,
        path: str,
        content: bytes,
        commit_message: str,
    ) -> None: ...

    async def copy_file_on_branch(
        self,
        pr_ref: PRRef,
        src_path: str,
        dest_path: str,
    ) -> None: ...

    async def last_workflow_run_at(
        self,
        pr_ref: PRRef,
        workflow_name: str,
    ) -> datetime | None: ...

    async def last_dispatch_run_at(self, pr_ref: PRRef) -> datetime | None: ...


@runtime_checkable
class HarnessPort(Protocol):
    async def dispatch(self, context: DispatchContext) -> RunHandle: ...

    async def trigger_workflow(self, name: str, ref: str, inputs: dict[str, object]) -> None: ...

    async def trigger_ci(self, pr_ref: PRRef) -> None: ...

    async def get_run_status(self, handle: RunHandle) -> RunStatus: ...

    async def cancel(self, handle: RunHandle) -> None: ...


@runtime_checkable
class SessionPort(Protocol):
    async def list_runs(
        self,
        repo: RepoRef,
        since: datetime | None = None,
        status: str | None = None,
        type: str | None = None,
    ) -> list[RunSummary]: ...

    async def get_run(self, run_id: str) -> RunDetail: ...

    def stream_events(self, run_id: str) -> AsyncIterator[RunEvent]: ...

    async def cancel(self, run_id: str) -> None: ...

    async def intervene(self, run_id: str, message: str) -> None: ...


@runtime_checkable
class CounterStore(Protocol):
    """Atomic per-entity, per-channel counters (SPEC §8.2a)."""

    async def get_count(self, entity_ref: IssueRef | PRRef, channel: str) -> int: ...

    async def increment(self, entity_ref: IssueRef | PRRef, channel: str) -> int: ...

    async def reset(self, entity_ref: IssueRef | PRRef, channel: str) -> None: ...


@runtime_checkable
class ConvergeStateStore(Protocol):
    """Per-PR converge loop state (SPEC §9.4)."""

    async def get_converge_round(self, pr_ref: PRRef) -> int: ...

    async def set_converge_round(self, pr_ref: PRRef, round: int) -> None: ...

    async def get_round_started(self, pr_ref: PRRef) -> datetime | None: ...

    async def set_round_started(self, pr_ref: PRRef, started: datetime) -> None: ...

    async def clear_converge_state(self, pr_ref: PRRef) -> None: ...

    async def get_last_run_handle(self, pr_ref: PRRef) -> RunHandle | None: ...

    async def set_last_run_handle(self, pr_ref: PRRef, handle: RunHandle) -> None: ...
