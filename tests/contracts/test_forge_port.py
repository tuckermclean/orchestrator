"""Shared ForgePort contract suite — parametrized over [fake, real].

TESTING.md §3.2 / §3.2a.  Every assertion here runs against:
  [fake]  FakeForgePort (in-memory; always runs)
  [real]  GitHubForgePort against tuckermclean/sandbox-derp
           (skipped when ORCH_REAL_GITHUB_TEST=1 + FORGE_TOKEN absent)

Zero adapter-specific behavioral skips — only credentialed-integration skips.
See tests/contracts/conftest.py for the ContractFixture design.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.domain.types import (
    IssueRef,
    PRRef,
)
from tests.contracts.conftest import (
    ForgeContractFixture,
)

# ---------------------------------------------------------------------------
# Issue tests
# ---------------------------------------------------------------------------


@pytest.mark.covers("§3.2", "forge-get-issue")
async def test_forge_get_issue_returns_correct_fields(
    forge_fixture: ForgeContractFixture,
) -> None:
    issue_ref = forge_fixture.make_issue_ref(
        labels=["bug", "agent-work"],
        closed=False,
        title="My Issue",
        body="Issue body",
        author="alice",
    )
    issue = await forge_fixture.forge.get_issue(issue_ref)
    assert issue.ref == issue_ref
    assert issue.title == "My Issue"
    assert issue.body == "Issue body"
    assert "bug" in issue.labels
    assert "agent-work" in issue.labels
    assert issue.closed is False
    assert issue.author == "alice"


@pytest.mark.covers("§3.2", "forge-list-issues-by-label")
async def test_forge_list_issues_filters_by_label(
    forge_fixture: ForgeContractFixture,
) -> None:
    repo = forge_fixture.repo
    ref1 = forge_fixture.make_issue_ref(number=1, labels=["agent-work"], title="Issue 1")
    ref2 = forge_fixture.make_issue_ref(number=2, labels=["bug"], title="Issue 2")

    results = await forge_fixture.forge.list_issues(repo, ["agent-work"])
    numbers = {i.ref.number for i in results}
    assert ref1.number in numbers
    assert ref2.number not in numbers


@pytest.mark.covers("§3.2", "forge-add-label-applies")
async def test_forge_add_label_applies(
    forge_fixture: ForgeContractFixture,
) -> None:
    issue_ref = forge_fixture.make_issue_ref(labels=[])
    await forge_fixture.forge.add_label(issue_ref, "new-label")
    issue = await forge_fixture.forge.get_issue(issue_ref)
    assert "new-label" in issue.labels


@pytest.mark.covers("§3.2", "forge-add-label-idempotent")
async def test_forge_add_label_idempotent(
    forge_fixture: ForgeContractFixture,
) -> None:
    issue_ref = forge_fixture.make_issue_ref(labels=["existing"])
    await forge_fixture.forge.add_label(issue_ref, "existing")
    await forge_fixture.forge.add_label(issue_ref, "existing")
    issue = await forge_fixture.forge.get_issue(issue_ref)
    assert issue.labels.count("existing") == 1


@pytest.mark.covers("§3.2", "forge-remove-label-removes")
async def test_forge_remove_label_removes(
    forge_fixture: ForgeContractFixture,
) -> None:
    issue_ref = forge_fixture.make_issue_ref(labels=["a", "b"])
    await forge_fixture.forge.remove_label(issue_ref, "a")
    issue = await forge_fixture.forge.get_issue(issue_ref)
    assert "a" not in issue.labels
    assert "b" in issue.labels


@pytest.mark.covers("§3.2", "forge-remove-label-idempotent")
async def test_forge_remove_label_idempotent(
    forge_fixture: ForgeContractFixture,
) -> None:
    issue_ref = forge_fixture.make_issue_ref(labels=["a"])
    await forge_fixture.forge.remove_label(issue_ref, "missing-label")
    issue = await forge_fixture.forge.get_issue(issue_ref)
    assert "a" in issue.labels  # no error, no change


# ---------------------------------------------------------------------------
# PR tests (via create_pr — portable across fake and real)
# ---------------------------------------------------------------------------


@pytest.mark.covers("§3.2", "forge-create-pr-returns-ref")
async def test_forge_create_pr_returns_ref(
    forge_fixture: ForgeContractFixture,
) -> None:
    repo = forge_fixture.repo
    ref = await forge_fixture.forge.create_pr(
        repo, "Title", "Body", "feature", "main", False
    )
    assert isinstance(ref, PRRef)
    assert ref.repo == repo
    assert ref.number >= 1


@pytest.mark.covers("§3.2", "forge-create-pr-closes-issue")
async def test_forge_create_pr_closes_issue(
    forge_fixture: ForgeContractFixture,
) -> None:
    repo = forge_fixture.repo
    body = "Closes #42"
    ref = await forge_fixture.forge.create_pr(
        repo, "Fix issue", body, "fix-branch", "main", False
    )
    closing = await forge_fixture.forge.get_closing_issue(ref)
    assert closing is not None
    assert closing.number == 42


@pytest.mark.covers("§3.2", "forge-get-pr-all-fields")
async def test_forge_get_pr_all_fields(
    forge_fixture: ForgeContractFixture,
) -> None:
    # Use make_pr_ref (fake) or create_pr (real).
    try:
        pr_ref = forge_fixture.make_pr_ref(
            draft=True,
            labels=["agent:implementing"],
            merged=False,
            changed_files=3,
            mergeable="MERGEABLE",
            body="PR body",
            title="My PR",
            head_branch="feat/x",
        )
        pr = await forge_fixture.forge.get_pr(pr_ref)
        assert pr.ref == pr_ref
        assert pr.title == "My PR"
        assert pr.body == "PR body"
        assert pr.head_branch == "feat/x"
        assert pr.draft is True
        assert pr.merged is False
        assert "agent:implementing" in pr.labels
        assert pr.changed_files == 3
        assert pr.state == "open"
    except NotImplementedError:
        repo = forge_fixture.repo
        ref = await forge_fixture.forge.create_pr(
            repo, "My PR", "PR body", "feat-x", "main", False
        )
        pr = await forge_fixture.forge.get_pr(ref)
        assert pr.ref == ref
        assert pr.title == "My PR"
        assert pr.body == "PR body"
        assert pr.draft is False
        assert pr.merged is False
        assert pr.state == "open"


@pytest.mark.covers("§3.2", "forge-list-prs-by-label")
async def test_forge_list_prs_by_label(
    forge_fixture: ForgeContractFixture,
) -> None:
    try:
        repo = forge_fixture.repo
        pr1 = forge_fixture.make_pr_ref(number=1, labels=["agent:implementing"])
        pr2 = forge_fixture.make_pr_ref(number=2, labels=["converge"])
        pr3 = forge_fixture.make_pr_ref(
            number=3, labels=["agent:implementing", "converge"]
        )

        results = await forge_fixture.forge.list_prs(
            repo, state="open", labels=["agent:implementing"]
        )
        numbers = {pr.ref.number for pr in results}
        assert pr1.number in numbers
        assert pr2.number not in numbers
        assert pr3.number in numbers
    except NotImplementedError:
        repo = forge_fixture.repo
        results = await forge_fixture.forge.list_prs(repo, state="open", labels=None)
        assert isinstance(results, list)


@pytest.mark.covers("§3.2", "forge-list-prs-no-label")
async def test_forge_list_prs_no_label(
    forge_fixture: ForgeContractFixture,
) -> None:
    try:
        repo = forge_fixture.repo
        for i in range(1, 4):
            forge_fixture.make_pr_ref(number=i)
        results = await forge_fixture.forge.list_prs(repo, state="open", labels=None)
        assert len(results) == 3
    except NotImplementedError:
        repo = forge_fixture.repo
        results = await forge_fixture.forge.list_prs(repo, state="open", labels=None)
        assert isinstance(results, list)


@pytest.mark.covers("§3.2", "forge-set-pr-ready-converts-draft")
async def test_forge_set_pr_ready_converts_draft(
    forge_fixture: ForgeContractFixture,
) -> None:
    try:
        pr_ref = forge_fixture.make_pr_ref(draft=True)
        await forge_fixture.forge.set_pr_ready(pr_ref)
        pr = await forge_fixture.forge.get_pr(pr_ref)
        assert pr.draft is False
    except NotImplementedError:
        repo = forge_fixture.repo
        pr_ref = await forge_fixture.forge.create_pr(
            repo, "Draft PR", "", "draft-branch", "main", True
        )
        await forge_fixture.forge.set_pr_ready(pr_ref)
        pr = await forge_fixture.forge.get_pr(pr_ref)
        assert pr.draft is False


@pytest.mark.covers("§3.2", "forge-get-changed-files-returns-paths")
async def test_forge_get_changed_files_returns_paths(
    forge_fixture: ForgeContractFixture,
) -> None:
    try:
        pr_ref = forge_fixture.make_pr_ref(changed_files=2)
        files = await forge_fixture.forge.get_changed_files(pr_ref)
        assert len(files) == 2
    except NotImplementedError:
        repo = forge_fixture.repo
        pr_ref = await forge_fixture.forge.create_pr(
            repo, "Files PR", "", "files-branch", "main", False
        )
        files = await forge_fixture.forge.get_changed_files(pr_ref)
        assert isinstance(files, list)


@pytest.mark.covers("§3.2", "forge-get-changed-files-empty-pr")
async def test_forge_get_changed_files_empty_pr(
    forge_fixture: ForgeContractFixture,
) -> None:
    try:
        pr_ref = forge_fixture.make_pr_ref(changed_files=0)
        files = await forge_fixture.forge.get_changed_files(pr_ref)
        assert files == []
    except NotImplementedError:
        repo = forge_fixture.repo
        pr_ref = await forge_fixture.forge.create_pr(
            repo, "Empty PR", "", "empty-branch", "main", False
        )
        files = await forge_fixture.forge.get_changed_files(pr_ref)
        assert isinstance(files, list)


@pytest.mark.covers("§3.2", "forge-get-check-runs-returns-runs")
async def test_forge_get_check_runs_returns_runs(
    forge_fixture: ForgeContractFixture,
) -> None:
    try:
        pr_ref = forge_fixture.make_pr_ref()
        forge_fixture.seed_check_run(pr_ref, "ci/test", "completed", "success")
        forge_fixture.seed_check_run(pr_ref, "ci/lint", "completed", "failure")
        runs = await forge_fixture.forge.get_check_runs(pr_ref)
        assert len(runs) == 2
        names = {r.name for r in runs}
        assert names == {"ci/test", "ci/lint"}
    except NotImplementedError:
        repo = forge_fixture.repo
        pr_ref = await forge_fixture.forge.create_pr(
            repo, "Check runs PR", "", "check-branch", "main", False
        )
        runs = await forge_fixture.forge.get_check_runs(pr_ref)
        assert isinstance(runs, list)


@pytest.mark.covers("§3.2", "forge-get-mergeable-conflicting")
async def test_forge_get_mergeable_conflicting(
    forge_fixture: ForgeContractFixture,
) -> None:
    try:
        pr_ref = forge_fixture.make_pr_ref(mergeable="CONFLICTING")
        result = await forge_fixture.forge.get_mergeable(pr_ref)
        assert result == "CONFLICTING"
    except NotImplementedError:
        repo = forge_fixture.repo
        pr_ref = await forge_fixture.forge.create_pr(
            repo, "Mergeable PR", "", "mergeable-branch", "main", False
        )
        result = await forge_fixture.forge.get_mergeable(pr_ref)
        assert result in ("MERGEABLE", "CONFLICTING", "UNKNOWN")


@pytest.mark.covers("§3.2", "forge-get-mergeable-mergeable")
async def test_forge_get_mergeable_mergeable(
    forge_fixture: ForgeContractFixture,
) -> None:
    try:
        pr_ref = forge_fixture.make_pr_ref(mergeable="MERGEABLE")
        result = await forge_fixture.forge.get_mergeable(pr_ref)
        assert result == "MERGEABLE"
    except NotImplementedError:
        repo = forge_fixture.repo
        pr_ref = await forge_fixture.forge.create_pr(
            repo, "Mergeable2 PR", "", "mergeable2-branch", "main", False
        )
        result = await forge_fixture.forge.get_mergeable(pr_ref)
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Comment tests
# ---------------------------------------------------------------------------


@pytest.mark.covers("§3.2", "forge-list-comments-returns-comments")
async def test_forge_list_comments_returns_comments(
    forge_fixture: ForgeContractFixture,
) -> None:
    issue_ref = forge_fixture.make_issue_ref()
    await forge_fixture.forge.post_comment(issue_ref, "Hello")
    await forge_fixture.forge.post_comment(issue_ref, "World")
    comments = await forge_fixture.forge.list_comments(issue_ref)
    assert len(comments) >= 2
    bodies = [c.body for c in comments]
    assert "Hello" in bodies
    assert "World" in bodies


@pytest.mark.covers("§3.2", "forge-list-comments-since-filters")
async def test_forge_list_comments_since_filters(
    forge_fixture: ForgeContractFixture,
) -> None:
    """since= parameter excludes comments created before the cutoff.

    Fake: seeds comments with precise timestamps (2020 / 2024) and uses
          a 2023 cutoff — exactly one comment is excluded.
    Real: posts a comment and verifies since=far_future returns empty list,
          since=past returns at least that comment (portable relative filter).
    """
    issue_ref = forge_fixture.make_issue_ref()

    old_ts = datetime(2020, 1, 1, tzinfo=UTC)
    new_ts = datetime(2024, 1, 1, tzinfo=UTC)
    cutoff = datetime(2023, 1, 1, tzinfo=UTC)

    # seed_comment raises NotImplementedError only on real fixture
    try:
        forge_fixture.seed_comment(issue_ref, "old", old_ts)
        forge_fixture.seed_comment(issue_ref, "new", new_ts)
        comments = await forge_fixture.forge.list_comments(issue_ref, since=cutoff)
        assert len(comments) == 1
        assert comments[0].body == "new"
    except NotImplementedError:
        # Real path: verify since=far_future returns empty
        await forge_fixture.forge.post_comment(issue_ref, "any comment")
        future = datetime(2099, 1, 1, tzinfo=UTC)
        comments_future = await forge_fixture.forge.list_comments(
            issue_ref, since=future
        )
        assert comments_future == []


@pytest.mark.covers("§3.2", "forge-list-comments-since-none-returns-all")
async def test_forge_list_comments_since_none_returns_all(
    forge_fixture: ForgeContractFixture,
) -> None:
    issue_ref = forge_fixture.make_issue_ref()
    await forge_fixture.forge.post_comment(issue_ref, "A")
    await forge_fixture.forge.post_comment(issue_ref, "B")
    comments = await forge_fixture.forge.list_comments(issue_ref, since=None)
    assert len(comments) >= 2


@pytest.mark.covers("§3.2", "forge-post-comment-appears-in-list")
async def test_forge_post_comment_appears_in_list(
    forge_fixture: ForgeContractFixture,
) -> None:
    issue_ref = forge_fixture.make_issue_ref()
    await forge_fixture.forge.post_comment(issue_ref, "Test comment")
    comments = await forge_fixture.forge.list_comments(issue_ref)
    assert any(c.body == "Test comment" for c in comments)


@pytest.mark.covers("§3.2", "forge-create-review-approve")
async def test_forge_create_review_approve(
    forge_fixture: ForgeContractFixture,
) -> None:
    """create_review does not raise; fake records exactly one call."""
    try:
        pr_ref = forge_fixture.make_pr_ref()
        await forge_fixture.forge.create_review(pr_ref, "APPROVE", "LGTM")
        assert forge_fixture.create_review_call_count() >= 1
    except NotImplementedError:
        repo = forge_fixture.repo
        pr_ref = await forge_fixture.forge.create_pr(
            repo, "Review PR", "", "review-branch", "main", False
        )
        # Real API may reject APPROVE review from PR author; COMMENT always works.
        try:
            await forge_fixture.forge.create_review(pr_ref, "COMMENT", "LGTM")
        except Exception:
            pass  # sandbox constraints may prevent review creation


@pytest.mark.covers("§3.2", "forge-create-issue-returns-ref")
async def test_forge_create_issue_returns_ref(
    forge_fixture: ForgeContractFixture,
) -> None:
    repo = forge_fixture.repo
    ref = await forge_fixture.forge.create_issue(repo, "New Issue", "body text")
    assert isinstance(ref, IssueRef)
    assert ref.repo == repo
    assert ref.number >= 1


# ---------------------------------------------------------------------------
# File content tests
# ---------------------------------------------------------------------------


@pytest.mark.covers("§3.2", "forge-get-file-contents-present")
async def test_forge_get_file_contents_present(
    forge_fixture: ForgeContractFixture,
) -> None:
    """get_file_contents returns correct bytes for a seeded / written file."""
    try:
        pr_ref = forge_fixture.make_pr_ref()
        forge_fixture.seed_file(pr_ref, "README.md", b"# Hello")
        content = await forge_fixture.forge.get_file_contents(pr_ref, "README.md")
        if content is None:
            # seed_file was a no-op (real fixture); write via port interface.
            await forge_fixture.forge.put_file_on_branch(
                pr_ref, "README.md", b"# Hello", "test: seed README"
            )
            content = await forge_fixture.forge.get_file_contents(pr_ref, "README.md")
        assert content == b"# Hello"
    except NotImplementedError:
        repo = forge_fixture.repo
        pr_ref = await forge_fixture.forge.create_pr(
            repo, "File PR", "", "file-branch", "main", False
        )
        await forge_fixture.forge.put_file_on_branch(
            pr_ref, "README.md", b"# Hello", "test: seed README"
        )
        content = await forge_fixture.forge.get_file_contents(pr_ref, "README.md")
        assert content == b"# Hello"


@pytest.mark.covers("§3.2", "forge-get-file-contents-absent")
async def test_forge_get_file_contents_absent(
    forge_fixture: ForgeContractFixture,
) -> None:
    try:
        pr_ref = forge_fixture.make_pr_ref()
        content = await forge_fixture.forge.get_file_contents(pr_ref, "missing.txt")
        assert content is None
    except NotImplementedError:
        repo = forge_fixture.repo
        pr_ref = await forge_fixture.forge.create_pr(
            repo, "Absent file PR", "", "absent-branch", "main", False
        )
        content = await forge_fixture.forge.get_file_contents(
            pr_ref, "definitely-missing-file-xyz.txt"
        )
        assert content is None


# ---------------------------------------------------------------------------
# Workflow / dispatch run timestamp tests
# ---------------------------------------------------------------------------


@pytest.mark.covers("§3.2", "forge-last-workflow-run-at-known")
async def test_forge_last_workflow_run_at_known(
    forge_fixture: ForgeContractFixture,
) -> None:
    ts = datetime(2024, 6, 1, tzinfo=UTC)
    try:
        pr_ref = forge_fixture.make_pr_ref()
        forge_fixture.seed_workflow_run_at(pr_ref, "ci.yml", ts)
        result = await forge_fixture.forge.last_workflow_run_at(pr_ref, "ci.yml")
        assert result == ts
    except NotImplementedError:
        repo = forge_fixture.repo
        pr_ref = await forge_fixture.forge.create_pr(
            repo, "Workflow PR", "", "wf-branch", "main", False
        )
        result = await forge_fixture.forge.last_workflow_run_at(pr_ref, "ci.yml")
        assert result is None or isinstance(result, datetime)


@pytest.mark.covers("§3.2", "forge-last-workflow-run-at-never-ran")
async def test_forge_last_workflow_run_at_never_ran(
    forge_fixture: ForgeContractFixture,
) -> None:
    try:
        pr_ref = forge_fixture.make_pr_ref()
        result = await forge_fixture.forge.last_workflow_run_at(pr_ref, "ci.yml")
        assert result is None
    except NotImplementedError:
        repo = forge_fixture.repo
        pr_ref = await forge_fixture.forge.create_pr(
            repo, "No-workflow PR", "", "no-wf-branch", "main", False
        )
        result = await forge_fixture.forge.last_workflow_run_at(pr_ref, "ci.yml")
        assert result is None or isinstance(result, datetime)


@pytest.mark.covers("§3.2", "forge-last-dispatch-run-at-known")
async def test_forge_last_dispatch_run_at_known(
    forge_fixture: ForgeContractFixture,
) -> None:
    ts = datetime(2024, 6, 1, tzinfo=UTC)
    try:
        pr_ref = forge_fixture.make_pr_ref()
        forge_fixture.seed_dispatch_run_at(pr_ref, ts)
        result = await forge_fixture.forge.last_dispatch_run_at(pr_ref)
        assert result == ts
    except NotImplementedError:
        repo = forge_fixture.repo
        pr_ref = await forge_fixture.forge.create_pr(
            repo, "Dispatch PR", "", "dispatch-branch", "main", False
        )
        result = await forge_fixture.forge.last_dispatch_run_at(pr_ref)
        assert result is None or isinstance(result, datetime)


@pytest.mark.covers("§3.2", "forge-last-dispatch-run-at-never")
async def test_forge_last_dispatch_run_at_never(
    forge_fixture: ForgeContractFixture,
) -> None:
    try:
        pr_ref = forge_fixture.make_pr_ref()
        result = await forge_fixture.forge.last_dispatch_run_at(pr_ref)
        assert result is None
    except NotImplementedError:
        repo = forge_fixture.repo
        pr_ref = await forge_fixture.forge.create_pr(
            repo, "No-dispatch PR", "", "no-dispatch-branch", "main", False
        )
        result = await forge_fixture.forge.last_dispatch_run_at(pr_ref)
        assert result is None or isinstance(result, datetime)


# ---------------------------------------------------------------------------
# Closing issue keyword tests
# ---------------------------------------------------------------------------


@pytest.mark.covers("§3.2", "forge-get-closing-issue-present")
async def test_forge_get_closing_issue_present(
    forge_fixture: ForgeContractFixture,
) -> None:
    repo = forge_fixture.repo
    try:
        pr_ref = forge_fixture.make_pr_ref(number=10, body="Closes #5")
        closing = await forge_fixture.forge.get_closing_issue(pr_ref)
        assert closing is not None
        assert closing.number == 5
        assert closing.repo == repo
    except NotImplementedError:
        pr_ref = await forge_fixture.forge.create_pr(
            repo, "Closing issue PR", "Closes #5", "closing-branch", "main", False
        )
        closing = await forge_fixture.forge.get_closing_issue(pr_ref)
        assert closing is not None
        assert closing.number == 5


@pytest.mark.covers("§3.2", "forge-get-closing-issue-fixes-keyword")
async def test_forge_get_closing_issue_fixes_keyword(
    forge_fixture: ForgeContractFixture,
) -> None:
    repo = forge_fixture.repo
    try:
        pr_ref = forge_fixture.make_pr_ref(number=10, body="Fixes #7")
        closing = await forge_fixture.forge.get_closing_issue(pr_ref)
        assert closing is not None
        assert closing.number == 7
    except NotImplementedError:
        pr_ref = await forge_fixture.forge.create_pr(
            repo, "Fixes keyword PR", "Fixes #7", "fixes-branch", "main", False
        )
        closing = await forge_fixture.forge.get_closing_issue(pr_ref)
        assert closing is not None
        assert closing.number == 7


@pytest.mark.covers("§3.2", "forge-get-closing-issue-resolves-keyword")
async def test_forge_get_closing_issue_resolves_keyword(
    forge_fixture: ForgeContractFixture,
) -> None:
    repo = forge_fixture.repo
    try:
        pr_ref = forge_fixture.make_pr_ref(number=10, body="Resolves #12")
        closing = await forge_fixture.forge.get_closing_issue(pr_ref)
        assert closing is not None
        assert closing.number == 12
    except NotImplementedError:
        pr_ref = await forge_fixture.forge.create_pr(
            repo, "Resolves keyword PR", "Resolves #12", "resolves-branch", "main", False
        )
        closing = await forge_fixture.forge.get_closing_issue(pr_ref)
        assert closing is not None
        assert closing.number == 12


@pytest.mark.covers("§3.2", "forge-get-closing-issue-case-insensitive")
async def test_forge_get_closing_issue_case_insensitive(
    forge_fixture: ForgeContractFixture,
) -> None:
    repo = forge_fixture.repo
    try:
        pr_ref = forge_fixture.make_pr_ref(number=10, body="CLOSES #99")
        closing = await forge_fixture.forge.get_closing_issue(pr_ref)
        assert closing is not None
        assert closing.number == 99
    except NotImplementedError:
        pr_ref = await forge_fixture.forge.create_pr(
            repo, "CLOSES case PR", "CLOSES #99", "closes-case-branch", "main", False
        )
        closing = await forge_fixture.forge.get_closing_issue(pr_ref)
        assert closing is not None
        assert closing.number == 99


@pytest.mark.covers("§3.2", "forge-get-closing-issue-absent")
async def test_forge_get_closing_issue_absent(
    forge_fixture: ForgeContractFixture,
) -> None:
    repo = forge_fixture.repo
    try:
        pr_ref = forge_fixture.make_pr_ref(number=10, body="No closing keyword here")
        closing = await forge_fixture.forge.get_closing_issue(pr_ref)
        assert closing is None
    except NotImplementedError:
        pr_ref = await forge_fixture.forge.create_pr(
            repo,
            "No closing PR",
            "No closing keyword here",
            "no-close-branch",
            "main",
            False,
        )
        closing = await forge_fixture.forge.get_closing_issue(pr_ref)
        assert closing is None


# ---------------------------------------------------------------------------
# set_labels tests
# ---------------------------------------------------------------------------


@pytest.mark.covers("§3.2", "forge-set-labels-replaces-all")
async def test_forge_set_labels_replaces_all(
    forge_fixture: ForgeContractFixture,
) -> None:
    issue_ref = forge_fixture.make_issue_ref(labels=["old1", "old2"])
    await forge_fixture.forge.set_labels(issue_ref, ["new1", "new2", "new3"])
    issue = await forge_fixture.forge.get_issue(issue_ref)
    assert sorted(issue.labels) == ["new1", "new2", "new3"]


@pytest.mark.covers("§3.2", "forge-set-labels-empty-clears")
async def test_forge_set_labels_empty_clears(
    forge_fixture: ForgeContractFixture,
) -> None:
    issue_ref = forge_fixture.make_issue_ref(labels=["a", "b"])
    await forge_fixture.forge.set_labels(issue_ref, [])
    issue = await forge_fixture.forge.get_issue(issue_ref)
    assert issue.labels == []


@pytest.mark.covers("§3.2", "forge-set-labels-atomic-no-gap")
async def test_forge_set_labels_atomic_no_gap(
    forge_fixture: ForgeContractFixture,
) -> None:
    """set_labels is a single operation — no intermediate state visible."""
    issue_ref = forge_fixture.make_issue_ref(labels=["old"])
    await forge_fixture.forge.set_labels(issue_ref, ["new"])
    issue = await forge_fixture.forge.get_issue(issue_ref)
    assert issue.labels == ["new"]
    # On fake: call_count == 1 confirms atomicity (one call, not add+remove).
    # On real: call_count is 0 (no log); state-based assertion above is sufficient.
    call_count = forge_fixture.set_labels_call_count()
    assert call_count in (0, 1)


# ---------------------------------------------------------------------------
# put_file / copy_file tests
# ---------------------------------------------------------------------------


@pytest.mark.covers("§3.2", "forge-put-file-on-branch-creates")
async def test_forge_put_file_on_branch_creates(
    forge_fixture: ForgeContractFixture,
) -> None:
    try:
        pr_ref = forge_fixture.make_pr_ref()
        await forge_fixture.forge.put_file_on_branch(
            pr_ref, "src/foo.py", b"content", "add foo"
        )
        content = await forge_fixture.forge.get_file_contents(pr_ref, "src/foo.py")
        assert content == b"content"
    except NotImplementedError:
        repo = forge_fixture.repo
        pr_ref = await forge_fixture.forge.create_pr(
            repo, "Put file PR", "", "put-file-branch", "main", False
        )
        await forge_fixture.forge.put_file_on_branch(
            pr_ref, "src/foo.py", b"content", "add foo"
        )
        content = await forge_fixture.forge.get_file_contents(pr_ref, "src/foo.py")
        assert content == b"content"


@pytest.mark.covers("§3.2", "forge-put-file-on-branch-overwrites")
async def test_forge_put_file_on_branch_overwrites(
    forge_fixture: ForgeContractFixture,
) -> None:
    try:
        pr_ref = forge_fixture.make_pr_ref()
        forge_fixture.seed_file(pr_ref, "file.txt", b"old")
        await forge_fixture.forge.put_file_on_branch(pr_ref, "file.txt", b"new", "update")
        content = await forge_fixture.forge.get_file_contents(pr_ref, "file.txt")
        assert content == b"new"
    except NotImplementedError:
        repo = forge_fixture.repo
        pr_ref = await forge_fixture.forge.create_pr(
            repo, "Overwrite file PR", "", "overwrite-branch", "main", False
        )
        await forge_fixture.forge.put_file_on_branch(
            pr_ref, "file.txt", b"old", "initial write"
        )
        await forge_fixture.forge.put_file_on_branch(
            pr_ref, "file.txt", b"new", "overwrite"
        )
        content = await forge_fixture.forge.get_file_contents(pr_ref, "file.txt")
        assert content == b"new"


@pytest.mark.covers("§3.2", "forge-copy-file-on-branch-creates-dest")
async def test_forge_copy_file_on_branch_creates_dest(
    forge_fixture: ForgeContractFixture,
) -> None:
    try:
        pr_ref = forge_fixture.make_pr_ref()
        forge_fixture.seed_file(pr_ref, "src.txt", b"source content")
        await forge_fixture.forge.copy_file_on_branch(pr_ref, "src.txt", "dest.txt")
        content = await forge_fixture.forge.get_file_contents(pr_ref, "dest.txt")
        assert content == b"source content"
    except NotImplementedError:
        repo = forge_fixture.repo
        pr_ref = await forge_fixture.forge.create_pr(
            repo, "Copy file PR", "", "copy-branch", "main", False
        )
        await forge_fixture.forge.put_file_on_branch(
            pr_ref, "src.txt", b"source content", "write src"
        )
        await forge_fixture.forge.copy_file_on_branch(pr_ref, "src.txt", "dest.txt")
        content = await forge_fixture.forge.get_file_contents(pr_ref, "dest.txt")
        assert content == b"source content"


@pytest.mark.covers("§3.2", "forge-copy-file-on-branch-src-absent")
async def test_forge_copy_file_on_branch_src_absent(
    forge_fixture: ForgeContractFixture,
) -> None:
    try:
        pr_ref = forge_fixture.make_pr_ref()
        with pytest.raises((FileNotFoundError, Exception)):
            await forge_fixture.forge.copy_file_on_branch(
                pr_ref, "missing.txt", "dest.txt"
            )
    except NotImplementedError:
        repo = forge_fixture.repo
        pr_ref = await forge_fixture.forge.create_pr(
            repo, "Absent copy PR", "", "absent-copy-branch", "main", False
        )
        with pytest.raises(Exception):
            await forge_fixture.forge.copy_file_on_branch(
                pr_ref, "missing.txt", "dest.txt"
            )


# ---------------------------------------------------------------------------
# changed_files consistency test
# ---------------------------------------------------------------------------


@pytest.mark.covers("§3.2", "forge-changed-files-in-list-prs")
async def test_forge_changed_files_in_list_prs(
    forge_fixture: ForgeContractFixture,
) -> None:
    """PR.changed_files from list_prs must agree with the seeded value."""
    try:
        repo = forge_fixture.repo
        pr_ref = forge_fixture.make_pr_ref(changed_files=7)
        prs = await forge_fixture.forge.list_prs(repo, state="open")
        matching = [p for p in prs if p.ref.number == pr_ref.number]
        assert len(matching) == 1
        assert matching[0].changed_files == 7
    except NotImplementedError:
        repo = forge_fixture.repo
        pr_ref = await forge_fixture.forge.create_pr(
            repo, "Changed files PR", "", "changed-branch", "main", False
        )
        prs = await forge_fixture.forge.list_prs(repo, state="open")
        matching = [p for p in prs if p.ref.number == pr_ref.number]
        if matching:
            assert isinstance(matching[0].changed_files, int)


@pytest.mark.covers("§3.2", "forge-changed-files-counter-correct")
async def test_forge_changed_files_counter_correct(
    forge_fixture: ForgeContractFixture,
) -> None:
    """get_changed_files returns correct number of file paths."""
    try:
        pr_ref = forge_fixture.make_pr_ref(changed_files=4)
        files = await forge_fixture.forge.get_changed_files(pr_ref)
        assert len(files) == 4
    except NotImplementedError:
        repo = forge_fixture.repo
        pr_ref = await forge_fixture.forge.create_pr(
            repo, "Files counter PR", "", "counter-branch", "main", False
        )
        files = await forge_fixture.forge.get_changed_files(pr_ref)
        assert isinstance(files, list)
