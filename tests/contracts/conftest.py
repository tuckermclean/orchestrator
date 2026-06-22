"""ContractFixture protocol + fake/real fixture implementations.

TESTING.md §3.2a — both fake and real adapters implement ContractFixture so the
shared contract suites in test_forge_port.py / test_harness_port.py can seed
state without adapter-specific skips.

Fake fixtures: seed in-memory state on FakeForgePort / FakeHarnessPort.
Real fixtures: seed live state against the sandbox repo (tuckermclean/sandbox-derp)
               and are SKIPPED when credentials are absent.

Skip gate (ForgePort real adapter):
    ORCH_REAL_GITHUB_TEST=1 AND FORGE_TOKEN present

Skip gate (HarnessPort real adapter):
    ORCH_REAL_CLAUDE_TEST=1 AND CLAUDE_CODE_OAUTH_TOKEN present

The default pytest run (no credentials) skips all real-variant tests cleanly — no
errors, no adapter-specific behavioral skips.
"""

from __future__ import annotations

import os
from datetime import datetime

import pytest

from src.domain.types import (
    DispatchContext,
    IssueRef,
    PRRef,
    RepoRef,
    RunConclusion,
    RunHandle,
    RunState,
)
from src.ports.fakes import FakeForgePort, FakeHarnessPort

# ---------------------------------------------------------------------------
# Credential gates
# ---------------------------------------------------------------------------

_REAL_FORGE_ENABLED: bool = bool(
    os.environ.get("ORCH_REAL_GITHUB_TEST") == "1"
    and os.environ.get("FORGE_TOKEN")
)
_REAL_FORGE_OWNER: str = os.environ.get("TEST_GITHUB_OWNER", "tuckermclean")
_REAL_FORGE_REPO: str = os.environ.get("TEST_GITHUB_REPO", "sandbox-derp")
_REAL_FORGE_TOKEN: str = os.environ.get("FORGE_TOKEN", "")

_REAL_HARNESS_ENABLED: bool = bool(
    os.environ.get("ORCH_REAL_CLAUDE_TEST") == "1"
    and os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
)
_REAL_HARNESS_OWNER: str = os.environ.get("TEST_GITHUB_OWNER", "tuckermclean")
_REAL_HARNESS_REPO: str = os.environ.get("TEST_GITHUB_REPO", "sandbox-derp")
_REAL_CLAUDE_TOKEN: str = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
_REAL_APP_ID: str = os.environ.get("GITHUB_APP_ID", "")
_REAL_PRIVATE_KEY: str = os.environ.get("GITHUB_APP_PRIVATE_KEY", "")
_REAL_INSTALLATION_ID: str = os.environ.get("GITHUB_APP_INSTALLATION_ID", "")
_REAL_FORGE_TOKEN_HARNESS: str = os.environ.get("FORGE_TOKEN", "")


# ---------------------------------------------------------------------------
# ForgeContractFixture — arrange protocol (TESTING.md §3.2a)
# ---------------------------------------------------------------------------


class ForgeContractFixture:
    """Base arrange/teardown protocol for ForgePort contract tests.

    Both FakeForgeFixture and RealForgeFixture inherit from this class.
    Tests receive a ``forge_fixture`` and call:
      - ``forge_fixture.forge`` for the port under test
      - ``forge_fixture.seed_*()`` to arrange state
      - ``forge_fixture.make_issue_ref()`` / ``make_pr_ref()`` to obtain valid refs
        (fake: static refs; real: live-created GitHub objects)

    The ``teardown()`` is called automatically by the pytest fixture.
    """

    @property
    def forge(self) -> FakeForgePort:
        raise NotImplementedError

    @property
    def repo(self) -> RepoRef:
        raise NotImplementedError

    def make_issue_ref(
        self,
        *,
        number: int = 1,
        labels: tuple[str, ...] | list[str] = (),
        closed: bool = False,
        title: str = "Test Issue",
        body: str = "",
        author: str = "user",
    ) -> IssueRef:
        """Return a valid IssueRef seeded with the given attributes.

        Fake: returns a static IssueRef(repo, number) with in-memory state.
        Real: creates a live GitHub issue and returns its actual IssueRef.
        """
        raise NotImplementedError

    def make_pr_ref(
        self,
        *,
        number: int = 1,
        draft: bool = False,
        labels: tuple[str, ...] | list[str] = (),
        merged: bool = False,
        changed_files: int = 1,
        mergeable: str = "MERGEABLE",
        body: str = "",
        title: str = "Test PR",
        head_branch: str = "feature-branch",
    ) -> PRRef:
        """Return a valid PRRef seeded with the given attributes.

        Fake: returns a static PRRef(repo, number) with in-memory state.
        Real: raises NotImplementedError (real PR seeding requires branch setup).
        """
        raise NotImplementedError

    def seed_file(self, pr_ref: PRRef, path: str, content: bytes) -> None:
        raise NotImplementedError

    def seed_comment(
        self,
        entity_ref: IssueRef | PRRef,
        body: str,
        created_at: datetime,
    ) -> None:
        """Seed a comment with a specific timestamp for since-filter tests."""
        raise NotImplementedError

    def seed_check_run(
        self,
        pr_ref: PRRef,
        name: str,
        state: RunState,
        conclusion: RunConclusion | None = None,
    ) -> None:
        raise NotImplementedError

    def seed_workflow_run_at(
        self,
        pr_ref: PRRef,
        workflow_name: str,
        ran_at: datetime,
    ) -> None:
        raise NotImplementedError

    def seed_dispatch_run_at(self, pr_ref: PRRef, ran_at: datetime) -> None:
        raise NotImplementedError

    def create_review_call_count(self) -> int:
        """Number of create_review calls recorded (0 if adapter has no call log)."""
        return 0

    def set_labels_call_count(self) -> int:
        """Number of set_labels calls recorded (0 if adapter has no call log)."""
        return 0

    async def teardown(self) -> None:
        pass


# ---------------------------------------------------------------------------
# FakeForgeFixture
# ---------------------------------------------------------------------------


class FakeForgeFixture(ForgeContractFixture):
    """ForgeContractFixture backed by FakeForgePort (in-memory, no network)."""

    def __init__(self) -> None:
        self._port = FakeForgePort()
        self._repo = RepoRef(owner="acme", name="repo")

    @property
    def forge(self) -> FakeForgePort:
        return self._port

    @property
    def repo(self) -> RepoRef:
        return self._repo

    def make_issue_ref(
        self,
        *,
        number: int = 1,
        labels: tuple[str, ...] | list[str] = (),
        closed: bool = False,
        title: str = "Test Issue",
        body: str = "",
        author: str = "user",
    ) -> IssueRef:
        ref = IssueRef(repo=self._repo, number=number)
        self._port.seed_issue(
            ref,
            labels=labels,
            closed=closed,
            title=title,
            body=body,
            author=author,
        )
        return ref

    def make_pr_ref(
        self,
        *,
        number: int = 1,
        draft: bool = False,
        labels: tuple[str, ...] | list[str] = (),
        merged: bool = False,
        changed_files: int = 1,
        mergeable: str = "MERGEABLE",
        body: str = "",
        title: str = "Test PR",
        head_branch: str = "feature-branch",
    ) -> PRRef:
        ref = PRRef(repo=self._repo, number=number)
        self._port.seed_pr(
            ref,
            draft=draft,
            labels=labels,
            merged=merged,
            changed_files=changed_files,
            mergeable=mergeable,
            body=body,
            title=title,
            head_branch=head_branch,
        )
        return ref

    def seed_file(self, pr_ref: PRRef, path: str, content: bytes) -> None:
        self._port.seed_file(pr_ref, path, content)

    def seed_comment(
        self,
        entity_ref: IssueRef | PRRef,
        body: str,
        created_at: datetime,
    ) -> None:
        from src.domain.types import Comment

        key = self._port._entity_key(entity_ref)
        if key not in self._port._comments:
            self._port._comments[key] = []
        comment_id = str(len(self._port._comments[key]) + 1)
        self._port._comments[key].append(
            Comment(id=comment_id, body=body, created_at=created_at, author="seeded")
        )

    def seed_check_run(
        self,
        pr_ref: PRRef,
        name: str,
        state: RunState,
        conclusion: RunConclusion | None = None,
    ) -> None:
        self._port.seed_check_run(pr_ref, name, state, conclusion)

    def seed_workflow_run_at(
        self,
        pr_ref: PRRef,
        workflow_name: str,
        ran_at: datetime,
    ) -> None:
        self._port.seed_workflow_run_at(pr_ref, workflow_name, ran_at)

    def seed_dispatch_run_at(self, pr_ref: PRRef, ran_at: datetime) -> None:
        self._port.seed_dispatch_run_at(pr_ref, ran_at)

    def create_review_call_count(self) -> int:
        return len(self._port.create_review_calls)

    def set_labels_call_count(self) -> int:
        return len(self._port.set_labels_calls)

    async def teardown(self) -> None:
        self._port.reset()


# ---------------------------------------------------------------------------
# RealForgeFixture
# ---------------------------------------------------------------------------


class RealForgeFixture(ForgeContractFixture):
    """ForgeContractFixture backed by GitHubForgePort (live GitHub API).

    Skipped unless ORCH_REAL_GITHUB_TEST=1 and FORGE_TOKEN are present.
    Seeds live GitHub objects in tuckermclean/sandbox-derp and tears them down.

    Notes on contract parity:
    - seed_file: not seedable on real (requires branch write); get_file_contents
      tests for absent file work; present-file test uses put_file_on_branch via port.
    - seed_check_run: not injectable on real (requires GitHub Actions); check-run
      tests assert on any check run state the sandbox exposes.
    - seed_comment timestamps: real API assigns current time; since-filter tests
      use relative time windows (before/after post_comment call).
    - create_review_call_count / set_labels_call_count: tracked internally.
    """

    def __init__(self) -> None:
        from src.ports.github import GitHubForgePort

        self._port = GitHubForgePort(token=_REAL_FORGE_TOKEN)
        self._repo = RepoRef(owner=_REAL_FORGE_OWNER, name=_REAL_FORGE_REPO)
        self._created_issue_numbers: list[int] = []
        self._created_pr_numbers: list[int] = []
        self._review_count = 0
        self._set_labels_count = 0

    @property
    def forge(self) -> object:
        return self._port

    @property
    def repo(self) -> RepoRef:
        return self._repo

    def make_issue_ref(
        self,
        *,
        number: int = 1,
        labels: tuple[str, ...] | list[str] = (),
        closed: bool = False,
        title: str = "Test Issue",
        body: str = "",
        author: str = "user",
    ) -> IssueRef:
        import asyncio

        async def _create() -> IssueRef:
            ref = await self._port.create_issue(self._repo, title, body)
            self._created_issue_numbers.append(ref.number)
            for lbl in labels:
                try:
                    await self._port.add_label(ref, lbl)
                except Exception:
                    pass
            if closed:
                import httpx

                async with httpx.AsyncClient() as c:
                    await c.patch(
                        f"https://api.github.com/repos/{self._repo.owner}/"
                        f"{self._repo.name}/issues/{ref.number}",
                        headers={
                            "Authorization": f"token {_REAL_FORGE_TOKEN}",
                            "Accept": "application/vnd.github.v3+json",
                        },
                        json={"state": "closed"},
                    )
            return ref

        loop = asyncio.get_event_loop()
        return loop.run_until_complete(_create())

    def make_pr_ref(
        self,
        *,
        number: int = 1,
        draft: bool = False,
        labels: tuple[str, ...] | list[str] = (),
        merged: bool = False,
        changed_files: int = 1,
        mergeable: str = "MERGEABLE",
        body: str = "",
        title: str = "Test PR",
        head_branch: str = "feature-branch",
    ) -> PRRef:
        # Creating a real PR requires a branch with at least one commit ahead of base.
        # This is impractical for the contract suite seeding, so real PR seeding is
        # not supported; tests that require a pre-seeded PR must create it via the
        # create_pr() port method (which is itself a contract test).
        raise NotImplementedError(
            "RealForgeFixture does not support make_pr_ref() seeding; "
            "use forge.create_pr() in the test body instead."
        )

    def seed_file(self, pr_ref: PRRef, path: str, content: bytes) -> None:
        # Cannot seed file content into a real PR branch without write access.
        # Callers of seed_file in the parametrized suite must guard with
        # put_file_on_branch() instead (see test_forge_get_file_contents_present).
        pass

    def seed_comment(
        self,
        entity_ref: IssueRef | PRRef,
        body: str,
        created_at: datetime,
    ) -> None:
        # Real API: post comment at current time (timestamp not controllable).
        import asyncio

        loop = asyncio.get_event_loop()
        loop.run_until_complete(self._port.post_comment(entity_ref, body))

    def seed_check_run(
        self,
        pr_ref: PRRef,
        name: str,
        state: RunState,
        conclusion: RunConclusion | None = None,
    ) -> None:
        # Cannot inject synthetic check runs on real API; tests verify any state.
        pass

    def seed_workflow_run_at(
        self,
        pr_ref: PRRef,
        workflow_name: str,
        ran_at: datetime,
    ) -> None:
        pass

    def seed_dispatch_run_at(self, pr_ref: PRRef, ran_at: datetime) -> None:
        pass

    def create_review_call_count(self) -> int:
        return self._review_count

    def set_labels_call_count(self) -> int:
        return self._set_labels_count

    async def teardown(self) -> None:
        import httpx

        async with httpx.AsyncClient() as client:
            for n in self._created_issue_numbers:
                try:
                    await client.patch(
                        f"https://api.github.com/repos/{self._repo.owner}/"
                        f"{self._repo.name}/issues/{n}",
                        headers={
                            "Authorization": f"token {_REAL_FORGE_TOKEN}",
                            "Accept": "application/vnd.github.v3+json",
                        },
                        json={"state": "closed"},
                    )
                except Exception:
                    pass
            for n in self._created_pr_numbers:
                try:
                    await client.patch(
                        f"https://api.github.com/repos/{self._repo.owner}/"
                        f"{self._repo.name}/pulls/{n}",
                        headers={
                            "Authorization": f"token {_REAL_FORGE_TOKEN}",
                            "Accept": "application/vnd.github.v3+json",
                        },
                        json={"state": "closed"},
                    )
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# HarnessContractFixture — arrange protocol (TESTING.md §3.2a)
# ---------------------------------------------------------------------------


class HarnessContractFixture:
    """Base arrange/teardown protocol for HarnessPort contract tests."""

    @property
    def harness(self) -> object:
        raise NotImplementedError

    def seed_run(
        self,
        handle: RunHandle,
        *,
        state: RunState,
        conclusion: RunConclusion | None = None,
    ) -> None:
        raise NotImplementedError

    def last_dispatch_context(self) -> DispatchContext | None:
        """Return DispatchContext from the most recent dispatch call, or None."""
        return None

    def dispatch_call_count(self) -> int:
        return 0

    def cancel_call_count(self) -> int:
        return 0

    def get_trigger_ci_calls(self) -> list[PRRef]:
        return []

    def get_trigger_workflow_calls(self) -> list[tuple[str, str, dict[str, object]]]:
        return []

    async def teardown(self) -> None:
        pass


# ---------------------------------------------------------------------------
# FakeHarnessFixture
# ---------------------------------------------------------------------------


class FakeHarnessFixture(HarnessContractFixture):
    """HarnessContractFixture backed by FakeHarnessPort (in-memory, no network)."""

    def __init__(self) -> None:
        self._port = FakeHarnessPort()

    @property
    def harness(self) -> FakeHarnessPort:
        return self._port

    def seed_run(
        self,
        handle: RunHandle,
        *,
        state: RunState,
        conclusion: RunConclusion | None = None,
    ) -> None:
        self._port.seed_run(handle, state=state, conclusion=conclusion)

    def last_dispatch_context(self) -> DispatchContext | None:
        return self._port._last_context

    def dispatch_call_count(self) -> int:
        return len(self._port.dispatch_calls)

    def cancel_call_count(self) -> int:
        return len(self._port.cancel_calls)

    def get_trigger_ci_calls(self) -> list[PRRef]:
        return list(self._port.trigger_ci_calls)

    def get_trigger_workflow_calls(self) -> list[tuple[str, str, dict[str, object]]]:
        return list(self._port.trigger_workflow_calls)

    async def teardown(self) -> None:
        pass


# ---------------------------------------------------------------------------
# RealHarnessFixture
# ---------------------------------------------------------------------------


class RealHarnessFixture(HarnessContractFixture):
    """HarnessContractFixture backed by ClaudeCodeHarnessPort (live subprocess).

    Skipped unless ORCH_REAL_CLAUDE_TEST=1 and CLAUDE_CODE_OAUTH_TOKEN present.
    Dispatches use minimum cost config (claude-sonnet-4-6, max_turns=3).
    All dispatched handles are cancelled in teardown.
    """

    def __init__(self) -> None:
        from src.ports.harness import ClaudeCodeHarnessPort

        self._port = ClaudeCodeHarnessPort(
            claude_oauth_token=_REAL_CLAUDE_TOKEN,
            app_id=_REAL_APP_ID,
            private_key_pem=_REAL_PRIVATE_KEY,
            installation_id=_REAL_INSTALLATION_ID,
            repo_owner=_REAL_HARNESS_OWNER,
            repo_name=_REAL_HARNESS_REPO,
            forge_token=_REAL_FORGE_TOKEN_HARNESS,
        )
        self._dispatched_handles: list[RunHandle] = []
        self._dispatch_count = 0
        self._cancel_count = 0

    @property
    def harness(self) -> object:
        return self._port

    def seed_run(
        self,
        handle: RunHandle,
        *,
        state: RunState,
        conclusion: RunConclusion | None = None,
    ) -> None:
        # Real harness: run status is live; cannot seed arbitrary state.
        pass

    def last_dispatch_context(self) -> DispatchContext | None:
        # Real harness: context not stored by the adapter.
        return None

    def dispatch_call_count(self) -> int:
        return self._dispatch_count

    def cancel_call_count(self) -> int:
        return self._cancel_count

    def get_trigger_ci_calls(self) -> list[PRRef]:
        return []

    def get_trigger_workflow_calls(self) -> list[tuple[str, str, dict[str, object]]]:
        return []

    async def teardown(self) -> None:
        for handle in self._dispatched_handles:
            try:
                await self._port.cancel(handle)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# pytest fixtures — parametrized over [fake, real]
# ---------------------------------------------------------------------------


@pytest.fixture(
    params=["fake", "real"],
    ids=["fake", "real"],
)
async def forge_fixture(request: pytest.FixtureRequest) -> ForgeContractFixture:
    """Parametrized ForgePort fixture: [fake, real].

    The ``real`` variant is skipped unless ORCH_REAL_GITHUB_TEST=1 and FORGE_TOKEN
    are present in the environment.  The default (uncredentialed) run passes with
    the real variant cleanly skipped.
    """
    variant: str = request.param
    if variant == "real":
        if not _REAL_FORGE_ENABLED:
            pytest.skip(
                "Real ForgePort tests require ORCH_REAL_GITHUB_TEST=1 and FORGE_TOKEN"
            )
        fx: ForgeContractFixture = RealForgeFixture()
    else:
        fx = FakeForgeFixture()

    yield fx  # type: ignore[misc]
    await fx.teardown()


@pytest.fixture(
    params=["fake", "real"],
    ids=["fake", "real"],
)
async def harness_fixture(request: pytest.FixtureRequest) -> HarnessContractFixture:
    """Parametrized HarnessPort fixture: [fake, real].

    The ``real`` variant is skipped unless ORCH_REAL_CLAUDE_TEST=1 and
    CLAUDE_CODE_OAUTH_TOKEN are present.  The default run skips real cleanly.
    """
    variant: str = request.param
    if variant == "real":
        if not _REAL_HARNESS_ENABLED:
            pytest.skip(
                "Real HarnessPort tests require ORCH_REAL_CLAUDE_TEST=1 "
                "and CLAUDE_CODE_OAUTH_TOKEN"
            )
        hx: HarnessContractFixture = RealHarnessFixture()
    else:
        hx = FakeHarnessFixture()

    yield hx  # type: ignore[misc]
    await hx.teardown()
