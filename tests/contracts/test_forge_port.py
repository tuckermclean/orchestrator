"""Contract tests for ForgePort against FakeForgePort."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.domain.types import (
    IssueRef,
    PRRef,
    RepoRef,
)
from src.ports.fakes import FakeForgePort


@pytest.fixture
def forge_port() -> FakeForgePort:
    """Fresh FakeForgePort for each test."""
    return FakeForgePort()


@pytest.fixture
def repo() -> RepoRef:
    return RepoRef(owner="acme", name="repo")


@pytest.fixture
def issue_ref(repo: RepoRef) -> IssueRef:
    return IssueRef(repo=repo, number=1)


@pytest.fixture
def pr_ref(repo: RepoRef) -> PRRef:
    return PRRef(repo=repo, number=1)


# ---------------------------------------------------------------------------
# Issue tests
# ---------------------------------------------------------------------------


async def test_forge_get_issue_returns_correct_fields(
    forge_port: FakeForgePort,
    issue_ref: IssueRef,
) -> None:
    forge_port.seed_issue(
        issue_ref,
        labels=["bug", "agent-work"],
        closed=False,
        title="My Issue",
        body="Issue body",
        author="alice",
    )
    issue = await forge_port.get_issue(issue_ref)
    assert issue.ref == issue_ref
    assert issue.title == "My Issue"
    assert issue.body == "Issue body"
    assert issue.labels == ["bug", "agent-work"]
    assert issue.closed is False
    assert issue.author == "alice"


async def test_forge_list_issues_filters_by_label(
    forge_port: FakeForgePort,
    repo: RepoRef,
) -> None:
    ref1 = IssueRef(repo=repo, number=1)
    ref2 = IssueRef(repo=repo, number=2)
    forge_port.seed_issue(ref1, labels=["agent-work"])
    forge_port.seed_issue(ref2, labels=["bug"])

    results = await forge_port.list_issues(repo, ["agent-work"])
    assert len(results) == 1
    assert results[0].ref == ref1


async def test_forge_add_label_applies(
    forge_port: FakeForgePort,
    issue_ref: IssueRef,
) -> None:
    forge_port.seed_issue(issue_ref, labels=[])
    await forge_port.add_label(issue_ref, "new-label")
    issue = await forge_port.get_issue(issue_ref)
    assert "new-label" in issue.labels


async def test_forge_add_label_idempotent(
    forge_port: FakeForgePort,
    issue_ref: IssueRef,
) -> None:
    forge_port.seed_issue(issue_ref, labels=["existing"])
    await forge_port.add_label(issue_ref, "existing")
    await forge_port.add_label(issue_ref, "existing")
    issue = await forge_port.get_issue(issue_ref)
    assert issue.labels.count("existing") == 1


async def test_forge_remove_label_removes(
    forge_port: FakeForgePort,
    issue_ref: IssueRef,
) -> None:
    forge_port.seed_issue(issue_ref, labels=["a", "b"])
    await forge_port.remove_label(issue_ref, "a")
    issue = await forge_port.get_issue(issue_ref)
    assert "a" not in issue.labels
    assert "b" in issue.labels


async def test_forge_remove_label_idempotent(
    forge_port: FakeForgePort,
    issue_ref: IssueRef,
) -> None:
    forge_port.seed_issue(issue_ref, labels=["a"])
    await forge_port.remove_label(issue_ref, "missing-label")
    issue = await forge_port.get_issue(issue_ref)
    assert "a" in issue.labels  # no error, no change


async def test_forge_create_pr_returns_ref(
    forge_port: FakeForgePort,
    repo: RepoRef,
) -> None:
    ref = await forge_port.create_pr(repo, "Title", "Body", "feature", "main", False)
    assert isinstance(ref, PRRef)
    assert ref.repo == repo
    assert ref.number >= 1


async def test_forge_create_pr_closes_issue(
    forge_port: FakeForgePort,
    repo: RepoRef,
) -> None:
    body = "Closes #42"
    ref = await forge_port.create_pr(repo, "Fix issue", body, "fix-branch", "main", False)
    closing = await forge_port.get_closing_issue(ref)
    assert closing is not None
    assert closing.number == 42


async def test_forge_get_pr_all_fields(
    forge_port: FakeForgePort,
    pr_ref: PRRef,
) -> None:
    forge_port.seed_pr(
        pr_ref,
        draft=True,
        labels=["agent:implementing"],
        merged=False,
        changed_files=3,
        mergeable="MERGEABLE",
        body="PR body",
        title="My PR",
        head_branch="feat/x",
    )
    pr = await forge_port.get_pr(pr_ref)
    assert pr.ref == pr_ref
    assert pr.title == "My PR"
    assert pr.body == "PR body"
    assert pr.head_branch == "feat/x"
    assert pr.draft is True
    assert pr.merged is False
    assert "agent:implementing" in pr.labels
    assert pr.changed_files == 3
    assert pr.state == "open"


async def test_forge_list_prs_by_label(
    forge_port: FakeForgePort,
    repo: RepoRef,
) -> None:
    forge_port.seed_pr(PRRef(repo=repo, number=1), labels=["agent:implementing"])
    forge_port.seed_pr(PRRef(repo=repo, number=2), labels=["converge"])
    forge_port.seed_pr(PRRef(repo=repo, number=3), labels=["agent:implementing", "converge"])

    results = await forge_port.list_prs(repo, state="open", labels=["agent:implementing"])
    numbers = {pr.ref.number for pr in results}
    assert numbers == {1, 3}


async def test_forge_list_prs_no_label(
    forge_port: FakeForgePort,
    repo: RepoRef,
) -> None:
    for i in range(1, 4):
        forge_port.seed_pr(PRRef(repo=repo, number=i))
    results = await forge_port.list_prs(repo, state="open", labels=None)
    assert len(results) == 3


async def test_forge_set_pr_ready_converts_draft(
    forge_port: FakeForgePort,
    pr_ref: PRRef,
) -> None:
    forge_port.seed_pr(pr_ref, draft=True)
    await forge_port.set_pr_ready(pr_ref)
    pr = await forge_port.get_pr(pr_ref)
    assert pr.draft is False


async def test_forge_get_changed_files_returns_paths(
    forge_port: FakeForgePort,
    pr_ref: PRRef,
) -> None:
    forge_port.seed_pr(pr_ref, changed_files=2)
    files = await forge_port.get_changed_files(pr_ref)
    assert len(files) == 2


async def test_forge_get_changed_files_empty_pr(
    forge_port: FakeForgePort,
    pr_ref: PRRef,
) -> None:
    forge_port.seed_pr(pr_ref, changed_files=0)
    files = await forge_port.get_changed_files(pr_ref)
    assert files == []


async def test_forge_get_check_runs_returns_runs(
    forge_port: FakeForgePort,
    pr_ref: PRRef,
) -> None:
    forge_port.seed_pr(pr_ref)
    forge_port.seed_check_run(pr_ref, "ci/test", "completed", "success")
    forge_port.seed_check_run(pr_ref, "ci/lint", "completed", "failure")
    runs = await forge_port.get_check_runs(pr_ref)
    assert len(runs) == 2
    names = {r.name for r in runs}
    assert names == {"ci/test", "ci/lint"}


async def test_forge_get_mergeable_conflicting(
    forge_port: FakeForgePort,
    pr_ref: PRRef,
) -> None:
    forge_port.seed_pr(pr_ref, mergeable="CONFLICTING")
    result = await forge_port.get_mergeable(pr_ref)
    assert result == "CONFLICTING"


async def test_forge_get_mergeable_mergeable(
    forge_port: FakeForgePort,
    pr_ref: PRRef,
) -> None:
    forge_port.seed_pr(pr_ref, mergeable="MERGEABLE")
    result = await forge_port.get_mergeable(pr_ref)
    assert result == "MERGEABLE"


async def test_forge_list_comments_returns_comments(
    forge_port: FakeForgePort,
    issue_ref: IssueRef,
) -> None:
    forge_port.seed_issue(issue_ref)
    await forge_port.post_comment(issue_ref, "Hello")
    await forge_port.post_comment(issue_ref, "World")
    comments = await forge_port.list_comments(issue_ref)
    assert len(comments) == 2
    bodies = [c.body for c in comments]
    assert "Hello" in bodies
    assert "World" in bodies


async def test_forge_list_comments_since_filters(
    forge_port: FakeForgePort,
    issue_ref: IssueRef,
) -> None:

    forge_port.seed_issue(issue_ref)
    key = f"issue:{forge_port._issue_key(issue_ref)}"
    old_ts = datetime(2020, 1, 1, tzinfo=UTC)
    new_ts = datetime(2024, 1, 1, tzinfo=UTC)
    cutoff = datetime(2023, 1, 1, tzinfo=UTC)

    from src.domain.types import Comment

    forge_port._comments[key] = [
        Comment(id="1", body="old", created_at=old_ts, author="a"),
        Comment(id="2", body="new", created_at=new_ts, author="b"),
    ]

    comments = await forge_port.list_comments(issue_ref, since=cutoff)
    assert len(comments) == 1
    assert comments[0].body == "new"


async def test_forge_list_comments_since_none_returns_all(
    forge_port: FakeForgePort,
    issue_ref: IssueRef,
) -> None:
    forge_port.seed_issue(issue_ref)
    await forge_port.post_comment(issue_ref, "A")
    await forge_port.post_comment(issue_ref, "B")
    comments = await forge_port.list_comments(issue_ref, since=None)
    assert len(comments) == 2


async def test_forge_post_comment_appears_in_list(
    forge_port: FakeForgePort,
    issue_ref: IssueRef,
) -> None:
    forge_port.seed_issue(issue_ref)
    await forge_port.post_comment(issue_ref, "Test comment")
    comments = await forge_port.list_comments(issue_ref)
    assert any(c.body == "Test comment" for c in comments)


async def test_forge_create_review_approve(
    forge_port: FakeForgePort,
    pr_ref: PRRef,
) -> None:
    forge_port.seed_pr(pr_ref)
    await forge_port.create_review(pr_ref, "APPROVE", "LGTM")
    assert len(forge_port.create_review_calls) == 1
    assert forge_port.create_review_calls[0] == (pr_ref, "APPROVE", "LGTM")
    assert len(forge_port._reviews) == 1


async def test_forge_create_issue_returns_ref(
    forge_port: FakeForgePort,
    repo: RepoRef,
) -> None:
    ref = await forge_port.create_issue(repo, "New Issue", "body text")
    assert isinstance(ref, IssueRef)
    assert ref.repo == repo
    assert ref.number >= 1


async def test_forge_get_file_contents_present(
    forge_port: FakeForgePort,
    pr_ref: PRRef,
) -> None:
    forge_port.seed_pr(pr_ref)
    forge_port.seed_file(pr_ref, "README.md", b"# Hello")
    content = await forge_port.get_file_contents(pr_ref, "README.md")
    assert content == b"# Hello"


async def test_forge_get_file_contents_absent(
    forge_port: FakeForgePort,
    pr_ref: PRRef,
) -> None:
    forge_port.seed_pr(pr_ref)
    content = await forge_port.get_file_contents(pr_ref, "missing.txt")
    assert content is None


async def test_forge_last_workflow_run_at_known(
    forge_port: FakeForgePort,
    pr_ref: PRRef,
) -> None:
    ts = datetime(2024, 6, 1, tzinfo=UTC)
    forge_port.seed_pr(pr_ref)
    forge_port.seed_workflow_run_at(pr_ref, "ci.yml", ts)
    result = await forge_port.last_workflow_run_at(pr_ref, "ci.yml")
    assert result == ts


async def test_forge_last_workflow_run_at_never_ran(
    forge_port: FakeForgePort,
    pr_ref: PRRef,
) -> None:
    forge_port.seed_pr(pr_ref)
    result = await forge_port.last_workflow_run_at(pr_ref, "ci.yml")
    assert result is None


async def test_forge_last_dispatch_run_at_known(
    forge_port: FakeForgePort,
    pr_ref: PRRef,
) -> None:
    ts = datetime(2024, 6, 1, tzinfo=UTC)
    forge_port.seed_pr(pr_ref)
    forge_port.seed_dispatch_run_at(pr_ref, ts)
    result = await forge_port.last_dispatch_run_at(pr_ref)
    assert result == ts


async def test_forge_last_dispatch_run_at_never(
    forge_port: FakeForgePort,
    pr_ref: PRRef,
) -> None:
    forge_port.seed_pr(pr_ref)
    result = await forge_port.last_dispatch_run_at(pr_ref)
    assert result is None


async def test_forge_get_closing_issue_present(
    forge_port: FakeForgePort,
    repo: RepoRef,
) -> None:
    pr_ref = PRRef(repo=repo, number=10)
    forge_port.seed_pr(pr_ref, body="Closes #5")
    closing = await forge_port.get_closing_issue(pr_ref)
    assert closing is not None
    assert closing.number == 5
    assert closing.repo == repo


async def test_forge_get_closing_issue_fixes_keyword(
    forge_port: FakeForgePort,
    repo: RepoRef,
) -> None:
    pr_ref = PRRef(repo=repo, number=10)
    forge_port.seed_pr(pr_ref, body="Fixes #7")
    closing = await forge_port.get_closing_issue(pr_ref)
    assert closing is not None
    assert closing.number == 7


async def test_forge_get_closing_issue_resolves_keyword(
    forge_port: FakeForgePort,
    repo: RepoRef,
) -> None:
    pr_ref = PRRef(repo=repo, number=10)
    forge_port.seed_pr(pr_ref, body="Resolves #12")
    closing = await forge_port.get_closing_issue(pr_ref)
    assert closing is not None
    assert closing.number == 12


async def test_forge_get_closing_issue_case_insensitive(
    forge_port: FakeForgePort,
    repo: RepoRef,
) -> None:
    pr_ref = PRRef(repo=repo, number=10)
    forge_port.seed_pr(pr_ref, body="CLOSES #99")
    closing = await forge_port.get_closing_issue(pr_ref)
    assert closing is not None
    assert closing.number == 99


async def test_forge_get_closing_issue_absent(
    forge_port: FakeForgePort,
    repo: RepoRef,
) -> None:
    pr_ref = PRRef(repo=repo, number=10)
    forge_port.seed_pr(pr_ref, body="No closing keyword here")
    closing = await forge_port.get_closing_issue(pr_ref)
    assert closing is None


async def test_forge_set_labels_replaces_all(
    forge_port: FakeForgePort,
    issue_ref: IssueRef,
) -> None:
    forge_port.seed_issue(issue_ref, labels=["old1", "old2"])
    await forge_port.set_labels(issue_ref, ["new1", "new2", "new3"])
    issue = await forge_port.get_issue(issue_ref)
    assert sorted(issue.labels) == ["new1", "new2", "new3"]


async def test_forge_set_labels_empty_clears(
    forge_port: FakeForgePort,
    issue_ref: IssueRef,
) -> None:
    forge_port.seed_issue(issue_ref, labels=["a", "b"])
    await forge_port.set_labels(issue_ref, [])
    issue = await forge_port.get_issue(issue_ref)
    assert issue.labels == []


async def test_forge_set_labels_atomic_no_gap(
    forge_port: FakeForgePort,
    issue_ref: IssueRef,
) -> None:
    """set_labels is a single operation — no intermediate state."""
    forge_port.seed_issue(issue_ref, labels=["old"])
    await forge_port.set_labels(issue_ref, ["new"])
    issue = await forge_port.get_issue(issue_ref)
    assert issue.labels == ["new"]
    assert len(forge_port.set_labels_calls) == 1


async def test_forge_put_file_on_branch_creates(
    forge_port: FakeForgePort,
    pr_ref: PRRef,
) -> None:
    forge_port.seed_pr(pr_ref)
    await forge_port.put_file_on_branch(pr_ref, "src/foo.py", b"content", "add foo")
    content = await forge_port.get_file_contents(pr_ref, "src/foo.py")
    assert content == b"content"


async def test_forge_put_file_on_branch_overwrites(
    forge_port: FakeForgePort,
    pr_ref: PRRef,
) -> None:
    forge_port.seed_pr(pr_ref)
    forge_port.seed_file(pr_ref, "file.txt", b"old")
    await forge_port.put_file_on_branch(pr_ref, "file.txt", b"new", "update")
    content = await forge_port.get_file_contents(pr_ref, "file.txt")
    assert content == b"new"


async def test_forge_copy_file_on_branch_creates_dest(
    forge_port: FakeForgePort,
    pr_ref: PRRef,
) -> None:
    forge_port.seed_pr(pr_ref)
    forge_port.seed_file(pr_ref, "src.txt", b"source content")
    await forge_port.copy_file_on_branch(pr_ref, "src.txt", "dest.txt")
    content = await forge_port.get_file_contents(pr_ref, "dest.txt")
    assert content == b"source content"


async def test_forge_copy_file_on_branch_src_absent(
    forge_port: FakeForgePort,
    pr_ref: PRRef,
) -> None:
    forge_port.seed_pr(pr_ref)
    with pytest.raises(FileNotFoundError):
        await forge_port.copy_file_on_branch(pr_ref, "missing.txt", "dest.txt")


async def test_forge_changed_files_in_list_prs(
    forge_port: FakeForgePort,
    repo: RepoRef,
) -> None:
    """changed_files field is preserved in list_prs."""
    pr_ref = PRRef(repo=repo, number=1)
    forge_port.seed_pr(pr_ref, changed_files=7)
    prs = await forge_port.list_prs(repo, state="open")
    assert len(prs) == 1
    assert prs[0].changed_files == 7


async def test_forge_changed_files_counter_correct(
    forge_port: FakeForgePort,
    pr_ref: PRRef,
) -> None:
    """get_changed_files returns correct number of paths."""
    forge_port.seed_pr(pr_ref, changed_files=4)
    files = await forge_port.get_changed_files(pr_ref)
    assert len(files) == 4
