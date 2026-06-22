"""GitHubForgePort — GitHub REST API implementation of ForgePort."""

from __future__ import annotations

import base64
from datetime import datetime
from typing import Any

import httpx

from src.domain.types import (
    _CLOSING_RE,
    PR,
    CheckRun,
    Comment,
    Issue,
    IssueRef,
    PRRef,
    RepoRef,
    RunConclusion,
    RunState,
)

# GitHub REST API base URL
_GITHUB_API = "https://api.github.com"

# Workflow name used for dispatch runs (matches the action workflow file name)
_DISPATCH_WORKFLOW_NAME = "claude-code-action"


def _repo_url(repo: RepoRef) -> str:
    return f"{_GITHUB_API}/repos/{repo.owner}/{repo.name}"


def _parse_run_state(state: str | None, status: str | None) -> RunState:
    """Map GitHub Actions run status to our RunState."""
    if status == "completed":
        return "completed"
    if status == "in_progress":
        return "in_progress"
    if status == "queued":
        return "queued"
    if state == "open":
        return "in_progress"
    return "queued"


def _parse_run_conclusion(conclusion: str | None) -> RunConclusion | None:
    """Map GitHub Actions run conclusion to our RunConclusion.

    GitHub has more conclusion values than our domain type.  Unknown values
    are mapped to "failure" (conservative — treat unknown outcomes as non-green).
    """
    if conclusion is None:
        return None
    # Direct mappings for values our domain type supports
    if conclusion in ("success", "failure", "cancelled"):
        return conclusion  # type: ignore[return-value]
    # GitHub-specific values not in our Literal — map conservatively
    # "skipped"/"neutral" → success (CI treats these as green)
    # "timed_out"/"action_required"/"stale" → failure
    if conclusion in ("skipped", "neutral"):
        return "success"
    return "failure"


class GitHubForgePort:
    """ForgePort backed by the GitHub REST API (httpx async)."""

    def __init__(self, token: str, client: httpx.AsyncClient | None = None) -> None:
        self._token = token
        # Accept an injected client (allows MockTransport in tests)
        self._client = client or httpx.AsyncClient(
            base_url=_GITHUB_API,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def _get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        resp = await self._client.get(url, headers=self._headers(), params=params)
        resp.raise_for_status()
        return resp.json()

    async def _post(self, url: str, json: dict[str, Any]) -> Any:
        resp = await self._client.post(url, headers=self._headers(), json=json)
        resp.raise_for_status()
        return resp.json()

    async def _patch(self, url: str, json: dict[str, Any]) -> Any:
        resp = await self._client.patch(url, headers=self._headers(), json=json)
        resp.raise_for_status()
        return resp.json()

    async def _put(self, url: str, json: dict[str, Any]) -> Any:
        resp = await self._client.put(url, headers=self._headers(), json=json)
        resp.raise_for_status()
        return resp.json()

    async def _delete(self, url: str) -> None:
        resp = await self._client.delete(url, headers=self._headers())
        # 404 on label delete is idempotent — treat as success
        if resp.status_code not in (200, 204, 404):
            resp.raise_for_status()

    async def _get_paginated(
        self, url: str, params: dict[str, Any] | None = None
    ) -> list[Any]:
        """Fetch all pages of a paginated GitHub endpoint."""
        page_params: dict[str, Any] = {"per_page": 100, **(params or {})}
        results: list[Any] = []
        page = 1
        while True:
            page_params["page"] = page
            resp = await self._client.get(
                url, headers=self._headers(), params=page_params
            )
            resp.raise_for_status()
            data = resp.json()
            if not data:
                break
            results.extend(data)
            # GitHub sets Link header for next page; stop when page is partial
            if len(data) < 100:
                break
            page += 1
        return results

    def _branch_from_pr(self, pr_ref: PRRef) -> str:
        """Resolve the head branch for a PR ref (requires a prior get_pr call if not cached)."""
        # The branch is not embedded in PRRef; callers that need it must pass it separately.
        # For file operations, the GitHub API uses the PR's head ref directly.
        return str(pr_ref.number)  # fallback placeholder — callers use _get_pr_head

    async def _get_pr_head(self, pr_ref: PRRef) -> str:
        """Return the head branch name for the PR."""
        pr = await self.get_pr(pr_ref)
        return pr.head_branch

    # ------------------------------------------------------------------
    # ForgePort implementation
    # ------------------------------------------------------------------

    async def get_issue(self, issue_ref: IssueRef) -> Issue:
        data = await self._get(
            f"{_GITHUB_API}/repos/{issue_ref.repo.owner}/{issue_ref.repo.name}"
            f"/issues/{issue_ref.number}"
        )
        return Issue(
            ref=issue_ref,
            title=str(data["title"]),
            body=str(data.get("body") or ""),
            labels=[lbl["name"] for lbl in data.get("labels", [])],
            closed=data["state"] == "closed",
            author=str(data["user"]["login"]),
        )

    async def list_issues(self, repo: RepoRef, labels: list[str]) -> list[Issue]:
        params: dict[str, Any] = {
            "labels": ",".join(labels),
            "state": "open",
            "per_page": 100,
        }
        data = await self._get_paginated(
            f"{_GITHUB_API}/repos/{repo.owner}/{repo.name}/issues",
            params=params,
        )
        issues = []
        for item in data:
            # GitHub returns both issues and PRs from /issues; skip PRs
            if "pull_request" in item:
                continue
            ref = IssueRef(repo=repo, number=int(item["number"]))
            issues.append(
                Issue(
                    ref=ref,
                    title=str(item["title"]),
                    body=str(item.get("body") or ""),
                    labels=[lbl["name"] for lbl in item.get("labels", [])],
                    closed=item["state"] == "closed",
                    author=str(item["user"]["login"]),
                )
            )
        return issues

    async def add_label(self, entity_ref: IssueRef | PRRef, label: str) -> None:
        number = entity_ref.number
        repo = entity_ref.repo
        # GitHub issues and PRs share the same labels endpoint
        await self._post(
            f"{_GITHUB_API}/repos/{repo.owner}/{repo.name}/issues/{number}/labels",
            json={"labels": [label]},
        )

    async def remove_label(self, entity_ref: IssueRef | PRRef, label: str) -> None:
        number = entity_ref.number
        repo = entity_ref.repo
        # Encode label name for URL; 404 is idempotent
        encoded = label.replace(" ", "%20")
        await self._delete(
            f"{_GITHUB_API}/repos/{repo.owner}/{repo.name}/issues/{number}"
            f"/labels/{encoded}"
        )

    async def set_labels(self, entity_ref: IssueRef | PRRef, labels: list[str]) -> None:
        """Atomically replace the full label set (PUT semantics)."""
        number = entity_ref.number
        repo = entity_ref.repo
        await self._put(
            f"{_GITHUB_API}/repos/{repo.owner}/{repo.name}/issues/{number}/labels",
            json={"labels": labels},
        )

    async def create_pr(
        self,
        repo: RepoRef,
        title: str,
        body: str,
        head: str,
        base: str,
        draft: bool,
    ) -> PRRef:
        data = await self._post(
            f"{_GITHUB_API}/repos/{repo.owner}/{repo.name}/pulls",
            json={"title": title, "body": body, "head": head, "base": base, "draft": draft},
        )
        return PRRef(repo=repo, number=int(data["number"]))

    async def get_pr(self, pr_ref: PRRef) -> PR:
        data = await self._get(
            f"{_GITHUB_API}/repos/{pr_ref.repo.owner}/{pr_ref.repo.name}"
            f"/pulls/{pr_ref.number}"
        )
        return PR(
            ref=pr_ref,
            title=str(data["title"]),
            body=str(data.get("body") or ""),
            head_branch=str(data["head"]["ref"]),
            draft=bool(data.get("draft", False)),
            merged=bool(data.get("merged", False)),
            labels=[lbl["name"] for lbl in data.get("labels", [])],
            changed_files=int(data.get("changed_files", 0)),
            state=str(data.get("state", "open")),  # type: ignore[arg-type]
        )

    async def list_prs(
        self,
        repo: RepoRef,
        state: str,
        labels: list[str] | None = None,
    ) -> list[PR]:
        params: dict[str, Any] = {"state": state, "per_page": 100}
        data = await self._get_paginated(
            f"{_GITHUB_API}/repos/{repo.owner}/{repo.name}/pulls",
            params=params,
        )
        prs = []
        for item in data:
            pr_labels = [lbl["name"] for lbl in item.get("labels", [])]
            # Client-side label filter (GitHub API does not filter PRs by label)
            if labels and not all(lbl in pr_labels for lbl in labels):
                continue
            ref = PRRef(repo=repo, number=int(item["number"]))
            prs.append(
                PR(
                    ref=ref,
                    title=str(item["title"]),
                    body=str(item.get("body") or ""),
                    head_branch=str(item["head"]["ref"]),
                    draft=bool(item.get("draft", False)),
                    merged=bool(item.get("merged", False)),
                    labels=pr_labels,
                    changed_files=int(item.get("changed_files", 0)),
                    state=str(item.get("state", "open")),  # type: ignore[arg-type]
                )
            )
        return prs

    async def set_pr_ready(self, pr_ref: PRRef) -> None:
        """Convert a draft PR to ready for review via GraphQL (REST has no direct endpoint)."""
        # The GitHub REST API has no "set ready for review" endpoint; use the PATCH endpoint
        # with `draft: false` if the implementation supports it, otherwise fall back to GraphQL.
        # As of 2024, GitHub REST supports PATCH /repos/{owner}/{repo}/pulls/{pull_number}
        # with draft=false to convert from draft.
        await self._patch(
            f"{_GITHUB_API}/repos/{pr_ref.repo.owner}/{pr_ref.repo.name}"
            f"/pulls/{pr_ref.number}",
            json={"draft": False},
        )

    async def get_changed_files(self, pr_ref: PRRef) -> list[str]:
        data = await self._get_paginated(
            f"{_GITHUB_API}/repos/{pr_ref.repo.owner}/{pr_ref.repo.name}"
            f"/pulls/{pr_ref.number}/files"
        )
        return [str(item["filename"]) for item in data]

    async def get_check_runs(self, pr_ref: PRRef) -> list[CheckRun]:
        """Fetch check runs for the PR's head commit.

        GitHub's check-runs endpoint returns a wrapper object
        ``{"total_count": N, "check_runs": [...]}``, not a bare list.
        """
        # Fetch the PR's HEAD commit SHA directly
        pr_data = await self._get(
            f"{_GITHUB_API}/repos/{pr_ref.repo.owner}/{pr_ref.repo.name}"
            f"/pulls/{pr_ref.number}"
        )
        head_sha = str(pr_data["head"]["sha"])
        # Use _get (not _get_paginated) because the response is a wrapper object
        response = await self._get(
            f"{_GITHUB_API}/repos/{pr_ref.repo.owner}/{pr_ref.repo.name}"
            f"/commits/{head_sha}/check-runs",
            params={"per_page": 100},
        )
        # GitHub wraps check runs in {"check_runs": [...]} — handle both forms
        if isinstance(response, dict):
            items: list[Any] = response.get("check_runs", [])
        else:
            items = list(response)  # bare list (test mock may return this)
        runs = []
        for item in items:
            state = _parse_run_state(None, str(item.get("status", "queued")))
            conclusion = _parse_run_conclusion(item.get("conclusion"))
            runs.append(
                CheckRun(
                    name=str(item["name"]),
                    state=state,
                    conclusion=conclusion,
                )
            )
        return runs

    async def get_mergeable(self, pr_ref: PRRef) -> str:
        data = await self._get(
            f"{_GITHUB_API}/repos/{pr_ref.repo.owner}/{pr_ref.repo.name}"
            f"/pulls/{pr_ref.number}"
        )
        mergeable_state = data.get("mergeable_state", "unknown")
        # Map GitHub's mergeable_state to our values
        if mergeable_state == "clean" or data.get("mergeable") is True:
            return "MERGEABLE"
        if mergeable_state == "dirty" or data.get("mergeable") is False:
            return "CONFLICTING"
        return "UNKNOWN"

    async def get_closing_issue(self, pr_ref: PRRef) -> IssueRef | None:
        pr = await self.get_pr(pr_ref)
        match = _CLOSING_RE.search(pr.body)
        if match:
            return IssueRef(repo=pr_ref.repo, number=int(match.group(1)))
        return None

    async def list_comments(
        self,
        entity_ref: IssueRef | PRRef,
        since: datetime | None = None,
    ) -> list[Comment]:
        repo = entity_ref.repo
        number = entity_ref.number
        params: dict[str, Any] = {"per_page": 100}
        if since is not None:
            params["since"] = since.isoformat()
        data = await self._get_paginated(
            f"{_GITHUB_API}/repos/{repo.owner}/{repo.name}/issues/{number}/comments",
            params=params,
        )
        comments = []
        for item in data:
            created_at_str = str(item["created_at"])
            created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
            if since is not None and created_at < since:
                continue
            comments.append(
                Comment(
                    id=str(item["id"]),
                    body=str(item.get("body") or ""),
                    created_at=created_at,
                    author=str(item["user"]["login"]),
                )
            )
        return comments

    async def post_comment(self, entity_ref: IssueRef | PRRef, body: str) -> None:
        repo = entity_ref.repo
        number = entity_ref.number
        await self._post(
            f"{_GITHUB_API}/repos/{repo.owner}/{repo.name}/issues/{number}/comments",
            json={"body": body},
        )

    async def create_review(self, pr_ref: PRRef, event: str, body: str) -> None:
        await self._post(
            f"{_GITHUB_API}/repos/{pr_ref.repo.owner}/{pr_ref.repo.name}"
            f"/pulls/{pr_ref.number}/reviews",
            json={"event": event, "body": body},
        )

    async def create_issue(self, repo: RepoRef, title: str, body: str) -> IssueRef:
        data = await self._post(
            f"{_GITHUB_API}/repos/{repo.owner}/{repo.name}/issues",
            json={"title": title, "body": body},
        )
        return IssueRef(repo=repo, number=int(data["number"]))

    async def get_file_contents(self, pr_ref: PRRef, path: str) -> bytes | None:
        """Fetch file contents from the PR head branch; returns None if absent."""
        pr = await self.get_pr(pr_ref)
        try:
            data = await self._get(
                f"{_GITHUB_API}/repos/{pr_ref.repo.owner}/{pr_ref.repo.name}"
                f"/contents/{path}",
                params={"ref": pr.head_branch},
            )
            if isinstance(data, dict) and data.get("encoding") == "base64":
                return base64.b64decode(data["content"].replace("\n", ""))
            return None
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise

    async def put_file_on_branch(
        self,
        pr_ref: PRRef,
        path: str,
        content: bytes,
        commit_message: str,
    ) -> None:
        """Write bytes to path in a single commit on the PR head branch."""
        pr = await self.get_pr(pr_ref)
        branch = pr.head_branch
        repo = pr_ref.repo

        # Check if file exists to get the current SHA (required for updates)
        sha: str | None = None
        try:
            existing = await self._get(
                f"{_GITHUB_API}/repos/{repo.owner}/{repo.name}/contents/{path}",
                params={"ref": branch},
            )
            if isinstance(existing, dict):
                sha = str(existing.get("sha", ""))
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise

        encoded = base64.b64encode(content).decode()
        payload: dict[str, Any] = {
            "message": commit_message,
            "content": encoded,
            "branch": branch,
        }
        if sha:
            payload["sha"] = sha

        await self._put(
            f"{_GITHUB_API}/repos/{repo.owner}/{repo.name}/contents/{path}",
            json=payload,
        )

    async def copy_file_on_branch(
        self,
        pr_ref: PRRef,
        src_path: str,
        dest_path: str,
    ) -> None:
        """Copy src_path → dest_path in a single commit on the PR head branch."""
        src_content = await self.get_file_contents(pr_ref, src_path)
        if src_content is None:
            raise FileNotFoundError(f"Source path {src_path!r} not found on PR {pr_ref}")
        await self.put_file_on_branch(
            pr_ref,
            dest_path,
            src_content,
            commit_message=f"copy {src_path} → {dest_path}",
        )

    async def last_workflow_run_at(
        self,
        pr_ref: PRRef,
        workflow_name: str,
    ) -> datetime | None:
        """Return the most recent run timestamp for the named workflow on the PR branch."""
        pr = await self.get_pr(pr_ref)
        branch = pr.head_branch
        repo = pr_ref.repo
        try:
            data = await self._get(
                f"{_GITHUB_API}/repos/{repo.owner}/{repo.name}/actions/workflows"
                f"/{workflow_name}/runs",
                params={"branch": branch, "per_page": 1},
            )
            runs = data.get("workflow_runs", [])
            if not runs:
                return None
            run = runs[0]
            ts_str = str(run.get("created_at", ""))
            if not ts_str:
                return None
            return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except httpx.HTTPStatusError:
            return None

    async def last_dispatch_run_at(self, pr_ref: PRRef) -> datetime | None:
        """Return the most recent dispatch run timestamp (Actions workflow) for the PR branch."""
        return await self.last_workflow_run_at(pr_ref, _DISPATCH_WORKFLOW_NAME)
