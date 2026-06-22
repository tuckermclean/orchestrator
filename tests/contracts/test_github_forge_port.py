"""Contract tests for GitHubForgePort against the real GitHub API.

These tests run the SAME contract suite as test_forge_port.py (FakeForgePort)
against the real GitHubForgePort.  They are gated behind @pytest.mark.integration_real
and will only execute when:
  - ORCH_REAL_GITHUB_TEST=1
  - FORGE_TOKEN is set (GitHub token with repo scope)
  - TEST_GITHUB_OWNER and TEST_GITHUB_REPO point to a sandboxed test repository

Without these env vars the entire module is skipped cleanly.

These tests CANNOT run in this autonomous build environment (no live tokens),
but are structured correctly for a credentialed CI/deploy environment.
See ROADMAP.md Step 8 Definition of Done.
"""

from __future__ import annotations

import os

import pytest

# Skip the entire module unless explicitly enabled and credentialed
_ENABLED = (
    os.environ.get("ORCH_REAL_GITHUB_TEST") == "1"
    and os.environ.get("FORGE_TOKEN")
)
if not _ENABLED:
    pytest.skip(
        "Real GitHub integration tests require ORCH_REAL_GITHUB_TEST=1 and FORGE_TOKEN",
        allow_module_level=True,
    )


from src.domain.types import RepoRef  # noqa: E402
from src.ports.github import GitHubForgePort  # noqa: E402

_OWNER = os.environ.get("TEST_GITHUB_OWNER", "")
_REPO_NAME = os.environ.get("TEST_GITHUB_REPO", "")
_TOKEN = os.environ.get("FORGE_TOKEN", "")

pytestmark = pytest.mark.integration_real


@pytest.fixture
def repo() -> RepoRef:
    return RepoRef(owner=_OWNER, name=_REPO_NAME)


@pytest.fixture
def forge_port() -> GitHubForgePort:
    return GitHubForgePort(token=_TOKEN)


# ---------------------------------------------------------------------------
# Issue operations (using a real sandboxed repo issue)
# ---------------------------------------------------------------------------


async def test_github_real_create_and_get_issue(
    forge_port: GitHubForgePort,
    repo: RepoRef,
) -> None:
    ref = await forge_port.create_issue(repo, "Integration test issue", "created by test")
    issue = await forge_port.get_issue(ref)
    assert issue.title == "Integration test issue"
    assert issue.ref == ref


async def test_github_real_add_remove_label(
    forge_port: GitHubForgePort,
    repo: RepoRef,
) -> None:
    ref = await forge_port.create_issue(repo, "Label test", "testing labels")
    await forge_port.add_label(ref, "test-label")
    issue = await forge_port.get_issue(ref)
    assert "test-label" in issue.labels

    await forge_port.remove_label(ref, "test-label")
    issue2 = await forge_port.get_issue(ref)
    assert "test-label" not in issue2.labels


async def test_github_real_set_labels(
    forge_port: GitHubForgePort,
    repo: RepoRef,
) -> None:
    ref = await forge_port.create_issue(repo, "Set labels test", "testing set_labels")
    await forge_port.set_labels(ref, ["label-a", "label-b"])
    issue = await forge_port.get_issue(ref)
    assert sorted(issue.labels) == sorted(["label-a", "label-b"])


async def test_github_real_post_and_list_comment(
    forge_port: GitHubForgePort,
    repo: RepoRef,
) -> None:
    ref = await forge_port.create_issue(repo, "Comment test", "testing comments")
    await forge_port.post_comment(ref, "Integration test comment")
    comments = await forge_port.list_comments(ref)
    assert any(c.body == "Integration test comment" for c in comments)


async def test_github_real_list_issues_by_label(
    forge_port: GitHubForgePort,
    repo: RepoRef,
) -> None:
    # Create an issue with a known label and verify list_issues finds it
    ref = await forge_port.create_issue(repo, "List issues test", "testing list_issues")
    await forge_port.add_label(ref, "integration-test-label")
    issues = await forge_port.list_issues(repo, labels=["integration-test-label"])
    numbers = {i.ref.number for i in issues}
    assert ref.number in numbers
