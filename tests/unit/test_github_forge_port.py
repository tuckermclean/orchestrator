"""Unit tests for GitHubForgePort using httpx MockTransport.

Every adapter method is exercised with a controlled mock response.  No network
calls are made.  These tests verify that the adapter builds the correct HTTP
requests and correctly parses GitHub API responses into domain types.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from src.domain.types import (
    IssueRef,
    PRRef,
    RepoRef,
)
from src.ports.github import GitHubForgePort

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

_TOKEN = "ghp_testtoken123"
_REPO = RepoRef(owner="acme", name="repo")
_ISSUE_REF = IssueRef(repo=_REPO, number=42)
_PR_REF = PRRef(repo=_REPO, number=7)


def _mock_pr_data(
    number: int = 7,
    title: str = "Fix bug",
    body: str = "Closes #42",
    head_ref: str = "feat/fix",
    head_sha: str = "abc123sha",
    draft: bool = False,
    merged: bool = False,
    labels: list[dict[str, Any]] | None = None,
    changed_files: int = 3,
    state: str = "open",
) -> dict[str, Any]:
    return {
        "number": number,
        "title": title,
        "body": body,
        "head": {"ref": head_ref, "sha": head_sha},
        "draft": draft,
        "merged": merged,
        "labels": labels or [],
        "changed_files": changed_files,
        "state": state,
        "mergeable": True,
        "mergeable_state": "clean",
    }


def _mock_issue_data(
    number: int = 42,
    title: str = "Test Issue",
    body: str = "Issue body",
    labels: list[dict[str, Any]] | None = None,
    state: str = "open",
    author: str = "alice",
) -> dict[str, Any]:
    return {
        "number": number,
        "title": title,
        "body": body,
        "labels": labels or [],
        "state": state,
        "user": {"login": author},
    }


class _MockTransportBuilder:
    """Build an httpx.MockTransport that returns scripted responses in order."""

    def __init__(self) -> None:
        self._responses: list[httpx.Response] = []

    def add_json(
        self,
        data: Any,
        status_code: int = 200,
    ) -> _MockTransportBuilder:
        self._responses.append(
            httpx.Response(status_code, json=data)
        )
        return self

    def add_empty(self, status_code: int = 204) -> _MockTransportBuilder:
        self._responses.append(httpx.Response(status_code, content=b""))
        return self

    def add_not_found(self) -> _MockTransportBuilder:
        self._responses.append(httpx.Response(404, json={"message": "Not Found"}))
        return self

    def build(self) -> httpx.AsyncClient:
        index = {"i": 0}
        responses = self._responses

        def _handler(request: httpx.Request) -> httpx.Response:
            if index["i"] >= len(responses):
                raise RuntimeError(
                    f"Unexpected request: {request.method} {request.url}"
                )
            resp = responses[index["i"]]
            index["i"] += 1
            return resp

        transport = httpx.MockTransport(_handler)
        return httpx.AsyncClient(transport=transport, base_url="https://api.github.com")


def _port(builder: _MockTransportBuilder) -> GitHubForgePort:
    return GitHubForgePort(token=_TOKEN, client=builder.build())


# ---------------------------------------------------------------------------
# get_issue
# ---------------------------------------------------------------------------


async def test_github_get_issue_builds_correct_request() -> None:
    b = _MockTransportBuilder()
    b.add_json(_mock_issue_data())
    port = _port(b)

    issue = await port.get_issue(_ISSUE_REF)

    assert issue.ref == _ISSUE_REF
    assert issue.title == "Test Issue"
    assert issue.body == "Issue body"
    assert issue.closed is False
    assert issue.author == "alice"


async def test_github_get_issue_labels_parsed() -> None:
    b = _MockTransportBuilder()
    b.add_json(
        _mock_issue_data(labels=[{"name": "bug"}, {"name": "agent-work"}])
    )
    port = _port(b)
    issue = await port.get_issue(_ISSUE_REF)
    assert "bug" in issue.labels
    assert "agent-work" in issue.labels


async def test_github_get_issue_closed_state() -> None:
    b = _MockTransportBuilder()
    b.add_json(_mock_issue_data(state="closed"))
    port = _port(b)
    issue = await port.get_issue(_ISSUE_REF)
    assert issue.closed is True


async def test_github_get_issue_null_body_normalized() -> None:
    data = _mock_issue_data()
    data["body"] = None
    b = _MockTransportBuilder()
    b.add_json(data)
    port = _port(b)
    issue = await port.get_issue(_ISSUE_REF)
    assert issue.body == ""


# ---------------------------------------------------------------------------
# list_issues
# ---------------------------------------------------------------------------


async def test_github_list_issues_filters_prs() -> None:
    """list_issues must exclude pull_request items returned by GitHub."""
    issue_item = _mock_issue_data(number=1)
    pr_item = {**_mock_issue_data(number=2), "pull_request": {"url": "https://..."}}
    b = _MockTransportBuilder()
    b.add_json([issue_item, pr_item])  # single page
    port = _port(b)

    results = await port.list_issues(_REPO, labels=["agent-work"])
    assert len(results) == 1
    assert results[0].ref.number == 1


async def test_github_list_issues_empty_list() -> None:
    b = _MockTransportBuilder()
    b.add_json([])
    port = _port(b)
    results = await port.list_issues(_REPO, labels=["nonexistent"])
    assert results == []


# ---------------------------------------------------------------------------
# add_label
# ---------------------------------------------------------------------------


async def test_github_add_label_posts_to_labels_endpoint() -> None:
    b = _MockTransportBuilder()
    b.add_json([{"name": "bug"}, {"name": "new-label"}])
    port = _port(b)
    # Should not raise
    await port.add_label(_ISSUE_REF, "new-label")


async def test_github_add_label_on_pr() -> None:
    b = _MockTransportBuilder()
    b.add_json([{"name": "converge"}])
    port = _port(b)
    await port.add_label(_PR_REF, "converge")


# ---------------------------------------------------------------------------
# remove_label
# ---------------------------------------------------------------------------


async def test_github_remove_label_success() -> None:
    b = _MockTransportBuilder()
    b.add_empty(204)
    port = _port(b)
    await port.remove_label(_ISSUE_REF, "bug")  # should not raise


async def test_github_remove_label_idempotent_on_404() -> None:
    b = _MockTransportBuilder()
    b.add_not_found()
    port = _port(b)
    # 404 on delete is idempotent — should not raise
    await port.remove_label(_ISSUE_REF, "nonexistent")


# ---------------------------------------------------------------------------
# set_labels
# ---------------------------------------------------------------------------


async def test_github_set_labels_replaces_all() -> None:
    b = _MockTransportBuilder()
    b.add_json([{"name": "new1"}, {"name": "new2"}])
    port = _port(b)
    await port.set_labels(_ISSUE_REF, ["new1", "new2"])


async def test_github_set_labels_empty_list() -> None:
    b = _MockTransportBuilder()
    b.add_json([])
    port = _port(b)
    await port.set_labels(_ISSUE_REF, [])


# ---------------------------------------------------------------------------
# create_pr
# ---------------------------------------------------------------------------


async def test_github_create_pr_returns_pr_ref() -> None:
    b = _MockTransportBuilder()
    b.add_json({"number": 99, "title": "New PR"})
    port = _port(b)
    ref = await port.create_pr(_REPO, "New PR", "body", "feat/x", "main", False)
    assert ref.repo == _REPO
    assert ref.number == 99


# ---------------------------------------------------------------------------
# get_pr
# ---------------------------------------------------------------------------


async def test_github_get_pr_all_fields() -> None:
    b = _MockTransportBuilder()
    b.add_json(
        _mock_pr_data(
            title="My PR",
            body="Fix it",
            head_ref="feat/abc",
            draft=True,
            labels=[{"name": "agent:implementing"}],
            changed_files=5,
        )
    )
    port = _port(b)
    pr = await port.get_pr(_PR_REF)
    assert pr.title == "My PR"
    assert pr.body == "Fix it"
    assert pr.head_branch == "feat/abc"
    assert pr.draft is True
    assert "agent:implementing" in pr.labels
    assert pr.changed_files == 5
    assert pr.state == "open"


async def test_github_get_pr_null_body_normalized() -> None:
    data = _mock_pr_data()
    data["body"] = None
    b = _MockTransportBuilder()
    b.add_json(data)
    port = _port(b)
    pr = await port.get_pr(_PR_REF)
    assert pr.body == ""


# ---------------------------------------------------------------------------
# list_prs
# ---------------------------------------------------------------------------


async def test_github_list_prs_returns_list() -> None:
    b = _MockTransportBuilder()
    b.add_json(
        [
            _mock_pr_data(number=1, labels=[{"name": "converge"}]),
            _mock_pr_data(number=2, labels=[]),
        ]
    )
    port = _port(b)
    prs = await port.list_prs(_REPO, state="open")
    assert len(prs) == 2


async def test_github_list_prs_label_filter() -> None:
    b = _MockTransportBuilder()
    b.add_json(
        [
            _mock_pr_data(number=1, labels=[{"name": "converge"}]),
            _mock_pr_data(number=2, labels=[]),
        ]
    )
    port = _port(b)
    prs = await port.list_prs(_REPO, state="open", labels=["converge"])
    assert len(prs) == 1
    assert prs[0].ref.number == 1


# ---------------------------------------------------------------------------
# set_pr_ready
# ---------------------------------------------------------------------------


async def test_github_set_pr_ready_patches_draft() -> None:
    b = _MockTransportBuilder()
    b.add_json(_mock_pr_data(draft=False))
    port = _port(b)
    await port.set_pr_ready(_PR_REF)  # should not raise


# ---------------------------------------------------------------------------
# get_changed_files
# ---------------------------------------------------------------------------


async def test_github_get_changed_files_returns_paths() -> None:
    b = _MockTransportBuilder()
    b.add_json(
        [
            {"filename": "src/main.py", "status": "modified"},
            {"filename": "tests/test_main.py", "status": "added"},
        ]
    )
    port = _port(b)
    files = await port.get_changed_files(_PR_REF)
    assert "src/main.py" in files
    assert "tests/test_main.py" in files


async def test_github_get_changed_files_empty() -> None:
    b = _MockTransportBuilder()
    b.add_json([])
    port = _port(b)
    files = await port.get_changed_files(_PR_REF)
    assert files == []


# ---------------------------------------------------------------------------
# get_check_runs
# ---------------------------------------------------------------------------


async def test_github_get_check_runs_parses_status() -> None:
    b = _MockTransportBuilder()
    # _get for PR data (to extract head SHA)
    b.add_json(_mock_pr_data(head_sha="abc123"))
    # _get for check-runs (bare list form — both forms handled)
    b.add_json(
        [
            {"name": "ci/test", "status": "completed", "conclusion": "success"},
            {"name": "ci/lint", "status": "completed", "conclusion": "failure"},
        ]
    )
    port = _port(b)
    runs = await port.get_check_runs(_PR_REF)
    assert len(runs) == 2
    names = {r.name for r in runs}
    assert "ci/test" in names
    assert "ci/lint" in names
    success_run = next(r for r in runs if r.name == "ci/test")
    assert success_run.conclusion == "success"
    fail_run = next(r for r in runs if r.name == "ci/lint")
    assert fail_run.conclusion == "failure"


async def test_github_get_check_runs_empty() -> None:
    b = _MockTransportBuilder()
    b.add_json(_mock_pr_data())  # _get for PR data
    b.add_json([])              # no check runs
    port = _port(b)
    runs = await port.get_check_runs(_PR_REF)
    assert runs == []


# ---------------------------------------------------------------------------
# get_mergeable
# ---------------------------------------------------------------------------


async def test_github_get_mergeable_clean() -> None:
    b = _MockTransportBuilder()
    b.add_json(_mock_pr_data(state="open"))  # get_pr used in get_mergeable
    port = _port(b)
    result = await port.get_mergeable(_PR_REF)
    assert result == "MERGEABLE"


async def test_github_get_mergeable_conflicting() -> None:
    data = _mock_pr_data()
    data["mergeable"] = False
    data["mergeable_state"] = "dirty"
    b = _MockTransportBuilder()
    b.add_json(data)
    port = _port(b)
    result = await port.get_mergeable(_PR_REF)
    assert result == "CONFLICTING"


async def test_github_get_mergeable_unknown() -> None:
    data = _mock_pr_data()
    data["mergeable"] = None
    data["mergeable_state"] = "unknown"
    b = _MockTransportBuilder()
    b.add_json(data)
    port = _port(b)
    result = await port.get_mergeable(_PR_REF)
    assert result == "UNKNOWN"


# ---------------------------------------------------------------------------
# get_closing_issue
# ---------------------------------------------------------------------------


async def test_github_get_closing_issue_closes_keyword() -> None:
    b = _MockTransportBuilder()
    b.add_json(_mock_pr_data(body="Closes #42"))
    port = _port(b)
    closing = await port.get_closing_issue(_PR_REF)
    assert closing is not None
    assert closing.number == 42
    assert closing.repo == _REPO


async def test_github_get_closing_issue_fixes_keyword() -> None:
    b = _MockTransportBuilder()
    b.add_json(_mock_pr_data(body="Fixes #7"))
    port = _port(b)
    closing = await port.get_closing_issue(_PR_REF)
    assert closing is not None
    assert closing.number == 7


async def test_github_get_closing_issue_resolves_keyword() -> None:
    b = _MockTransportBuilder()
    b.add_json(_mock_pr_data(body="Resolves #100"))
    port = _port(b)
    closing = await port.get_closing_issue(_PR_REF)
    assert closing is not None
    assert closing.number == 100


async def test_github_get_closing_issue_case_insensitive() -> None:
    b = _MockTransportBuilder()
    b.add_json(_mock_pr_data(body="CLOSES #99"))
    port = _port(b)
    closing = await port.get_closing_issue(_PR_REF)
    assert closing is not None
    assert closing.number == 99


async def test_github_get_closing_issue_absent() -> None:
    b = _MockTransportBuilder()
    b.add_json(_mock_pr_data(body="No closing keyword"))
    port = _port(b)
    closing = await port.get_closing_issue(_PR_REF)
    assert closing is None


# ---------------------------------------------------------------------------
# list_comments
# ---------------------------------------------------------------------------


async def test_github_list_comments_returns_comments() -> None:
    b = _MockTransportBuilder()
    b.add_json(
        [
            {
                "id": 1,
                "body": "First comment",
                "created_at": "2024-01-01T00:00:00Z",
                "user": {"login": "alice"},
            },
            {
                "id": 2,
                "body": "Second comment",
                "created_at": "2024-01-02T00:00:00Z",
                "user": {"login": "bob"},
            },
        ]
    )
    port = _port(b)
    comments = await port.list_comments(_ISSUE_REF)
    assert len(comments) == 2
    assert comments[0].body == "First comment"
    assert comments[0].author == "alice"
    assert comments[1].body == "Second comment"


async def test_github_list_comments_since_filter() -> None:
    cutoff = datetime(2024, 1, 2, tzinfo=UTC)
    b = _MockTransportBuilder()
    b.add_json(
        [
            {
                "id": 1,
                "body": "Old",
                "created_at": "2024-01-01T00:00:00Z",
                "user": {"login": "alice"},
            },
            {
                "id": 2,
                "body": "New",
                "created_at": "2024-01-03T00:00:00Z",
                "user": {"login": "bob"},
            },
        ]
    )
    port = _port(b)
    comments = await port.list_comments(_ISSUE_REF, since=cutoff)
    assert len(comments) == 1
    assert comments[0].body == "New"


async def test_github_list_comments_empty() -> None:
    b = _MockTransportBuilder()
    b.add_json([])
    port = _port(b)
    comments = await port.list_comments(_ISSUE_REF)
    assert comments == []


# ---------------------------------------------------------------------------
# post_comment
# ---------------------------------------------------------------------------


async def test_github_post_comment_posts_body() -> None:
    b = _MockTransportBuilder()
    b.add_json(
        {
            "id": 10,
            "body": "Test comment",
            "created_at": "2024-01-01T00:00:00Z",
            "user": {"login": "orchestrator"},
        }
    )
    port = _port(b)
    await port.post_comment(_ISSUE_REF, "Test comment")  # should not raise


# ---------------------------------------------------------------------------
# create_review
# ---------------------------------------------------------------------------


async def test_github_create_review_approve() -> None:
    b = _MockTransportBuilder()
    b.add_json({"id": 1, "state": "APPROVED", "body": "LGTM"})
    port = _port(b)
    await port.create_review(_PR_REF, "APPROVE", "LGTM")  # should not raise


async def test_github_create_review_request_changes() -> None:
    b = _MockTransportBuilder()
    b.add_json({"id": 2, "state": "CHANGES_REQUESTED", "body": "Needs work"})
    port = _port(b)
    await port.create_review(_PR_REF, "REQUEST_CHANGES", "Needs work")


# ---------------------------------------------------------------------------
# create_issue
# ---------------------------------------------------------------------------


async def test_github_create_issue_returns_ref() -> None:
    b = _MockTransportBuilder()
    b.add_json({"number": 101, "title": "New Issue"})
    port = _port(b)
    ref = await port.create_issue(_REPO, "New Issue", "body text")
    assert ref.repo == _REPO
    assert ref.number == 101


# ---------------------------------------------------------------------------
# get_file_contents
# ---------------------------------------------------------------------------


async def test_github_get_file_contents_present() -> None:
    content_bytes = b"# Hello World"
    encoded = base64.b64encode(content_bytes).decode()
    b = _MockTransportBuilder()
    b.add_json(_mock_pr_data())  # get_pr for head branch
    b.add_json({"encoding": "base64", "content": encoded + "\n"})
    port = _port(b)
    result = await port.get_file_contents(_PR_REF, "README.md")
    assert result == content_bytes


async def test_github_get_file_contents_absent_returns_none() -> None:
    b = _MockTransportBuilder()
    b.add_json(_mock_pr_data())  # get_pr
    b.add_not_found()
    port = _port(b)
    result = await port.get_file_contents(_PR_REF, "missing.txt")
    assert result is None


# ---------------------------------------------------------------------------
# put_file_on_branch
# ---------------------------------------------------------------------------


async def test_github_put_file_on_branch_creates_new() -> None:
    b = _MockTransportBuilder()
    b.add_json(_mock_pr_data())  # get_pr
    b.add_not_found()           # file doesn't exist yet
    b.add_json({"content": {"path": "file.txt"}, "commit": {"sha": "abc"}})
    port = _port(b)
    await port.put_file_on_branch(_PR_REF, "file.txt", b"content", "add file")


async def test_github_put_file_on_branch_overwrites_existing() -> None:
    b = _MockTransportBuilder()
    b.add_json(_mock_pr_data())  # get_pr
    # Existing file returns its SHA
    b.add_json(
        {
            "encoding": "base64",
            "content": base64.b64encode(b"old").decode(),
            "sha": "existing-sha-abc",
        }
    )
    b.add_json({"content": {"path": "file.txt"}, "commit": {"sha": "newsha"}})
    port = _port(b)
    await port.put_file_on_branch(_PR_REF, "file.txt", b"new", "update file")


# ---------------------------------------------------------------------------
# copy_file_on_branch
# ---------------------------------------------------------------------------


async def test_github_copy_file_on_branch_copies_content() -> None:
    content = b"source content"
    encoded = base64.b64encode(content).decode()
    b = _MockTransportBuilder()
    # get_file_contents → get_pr + file read
    b.add_json(_mock_pr_data())  # get_pr for get_file_contents
    b.add_json({"encoding": "base64", "content": encoded})
    # put_file_on_branch → get_pr + check if dest exists + write
    b.add_json(_mock_pr_data())  # get_pr for put_file_on_branch
    b.add_not_found()            # dest doesn't exist
    b.add_json({"content": {"path": "dest.txt"}, "commit": {"sha": "abc"}})
    port = _port(b)
    await port.copy_file_on_branch(_PR_REF, "src.txt", "dest.txt")


async def test_github_copy_file_on_branch_src_absent_raises() -> None:
    b = _MockTransportBuilder()
    b.add_json(_mock_pr_data())  # get_pr
    b.add_not_found()            # src absent
    port = _port(b)
    with pytest.raises(FileNotFoundError):
        await port.copy_file_on_branch(_PR_REF, "missing.txt", "dest.txt")


# ---------------------------------------------------------------------------
# last_workflow_run_at
# ---------------------------------------------------------------------------


async def test_github_last_workflow_run_at_returns_timestamp() -> None:
    b = _MockTransportBuilder()
    b.add_json(_mock_pr_data())  # get_pr
    b.add_json(
        {
            "workflow_runs": [
                {
                    "id": 1234,
                    "created_at": "2024-06-01T12:00:00Z",
                    "status": "completed",
                }
            ],
            "total_count": 1,
        }
    )
    port = _port(b)
    result = await port.last_workflow_run_at(_PR_REF, "ci.yml")
    assert result is not None
    assert result.year == 2024
    assert result.month == 6


async def test_github_last_workflow_run_at_no_runs_returns_none() -> None:
    b = _MockTransportBuilder()
    b.add_json(_mock_pr_data())  # get_pr
    b.add_json({"workflow_runs": [], "total_count": 0})
    port = _port(b)
    result = await port.last_workflow_run_at(_PR_REF, "ci.yml")
    assert result is None


# ---------------------------------------------------------------------------
# last_dispatch_run_at (delegates to last_workflow_run_at)
# ---------------------------------------------------------------------------


async def test_github_last_dispatch_run_at_returns_timestamp() -> None:
    b = _MockTransportBuilder()
    b.add_json(_mock_pr_data())  # get_pr for last_workflow_run_at
    b.add_json(
        {
            "workflow_runs": [
                {"id": 999, "created_at": "2024-07-15T08:00:00Z"}
            ],
            "total_count": 1,
        }
    )
    port = _port(b)
    result = await port.last_dispatch_run_at(_PR_REF)
    assert result is not None
    assert result.month == 7


async def test_github_last_dispatch_run_at_never_returns_none() -> None:
    b = _MockTransportBuilder()
    b.add_json(_mock_pr_data())  # get_pr
    b.add_json({"workflow_runs": [], "total_count": 0})
    port = _port(b)
    result = await port.last_dispatch_run_at(_PR_REF)
    assert result is None
