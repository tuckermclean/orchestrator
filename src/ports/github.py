"""GitHubForgePort — GitHub REST API implementation of ForgePort."""

from __future__ import annotations

import base64
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from datetime import datetime
from typing import Any

import httpx
import jwt

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

# GitHub App installation tokens expire after 1 hour; refresh with this margin (seconds)
_APP_TOKEN_REFRESH_MARGIN = 120


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
        from urllib.parse import quote

        number = entity_ref.number
        repo = entity_ref.repo
        # Encode label name for URL (safe='' ensures all special chars are encoded)
        encoded = quote(label, safe="")
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

    async def _get_check_runs_paginated(
        self, url: str, params: dict[str, Any] | None = None
    ) -> list[Any]:
        """Paginate the check-runs wrapper endpoint ``{total_count, check_runs: [...]}``.

        The check-runs endpoint returns ``{"total_count": N, "check_runs": [...]}``
        rather than a bare list, so _get_paginated (which expects a bare list) cannot
        be used directly.  This method loops until all pages are fetched.
        """
        page_params: dict[str, Any] = {"per_page": 100, **(params or {})}
        all_runs: list[Any] = []
        page = 1
        while True:
            page_params["page"] = page
            resp = await self._client.get(url, headers=self._headers(), params=page_params)
            resp.raise_for_status()
            data = resp.json()
            # Handle both wrapper dict form and bare list (test compatibility)
            if isinstance(data, dict):
                page_runs: list[Any] = data.get("check_runs", [])
                total_count = int(data.get("total_count", len(page_runs)))
            else:
                page_runs = list(data)
                total_count = len(all_runs) + len(page_runs)
            all_runs.extend(page_runs)
            # Stop when we have all runs or the page is partial
            if len(page_runs) < 100 or len(all_runs) >= total_count:
                break
            page += 1
        return all_runs

    async def get_check_runs(self, pr_ref: PRRef) -> list[CheckRun]:
        """Fetch all check runs for the PR's head commit (paginated).

        GitHub's check-runs endpoint returns a wrapper object
        ``{"total_count": N, "check_runs": [...]}``, not a bare list.
        All pages are fetched so that no BLOCKING_CI_CHECK can be silently dropped.
        """
        # Fetch the PR's HEAD commit SHA directly
        pr_data = await self._get(
            f"{_GITHUB_API}/repos/{pr_ref.repo.owner}/{pr_ref.repo.name}"
            f"/pulls/{pr_ref.number}"
        )
        head_sha = str(pr_data["head"]["sha"])
        items = await self._get_check_runs_paginated(
            f"{_GITHUB_API}/repos/{pr_ref.repo.owner}/{pr_ref.repo.name}"
            f"/commits/{head_sha}/check-runs",
        )
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


# ---------------------------------------------------------------------------
# GitHub App installation auth
# ---------------------------------------------------------------------------


class _InstallationTokenAuth(httpx.Auth):
    """httpx auth flow that mints/refreshes a GitHub App installation token and
    sets it on every outgoing request.

    This guarantees authentication for ALL requests issued through the client —
    including base-class helpers (e.g. ``_get_paginated``) that call the client
    directly rather than through the overridden ``_get``/``_post`` methods. The
    supplied ``get_token`` coroutine caches the token, so this does not re-mint
    on every call.
    """

    def __init__(self, get_token: Callable[[], Awaitable[str]]) -> None:
        self._get_token = get_token

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        # The installation-token mint request (POST /app/installations/{id}/access_tokens)
        # authenticates with the App JWT it already carries — it must NOT go through this
        # flow, or it would recurse (mint → auth flow → mint → …). Pass it through untouched.
        if request.url.path.endswith("/access_tokens"):
            yield request
            return
        token = await self._get_token()
        request.headers["Authorization"] = f"Bearer {token}"
        yield request


class GitHubAppForgePort(GitHubForgePort):
    """ForgePort that authenticates via a GitHub App installation token.

    When GITHUB_APP_ID, GITHUB_APP_PRIVATE_KEY, and GITHUB_APP_INSTALLATION_ID
    are set, this class mints an installation access token by:
      1. Signing a short-lived JWT with the App's RS256 private key.
      2. Exchanging the JWT for an installation access token via
         POST /app/installations/{id}/access_tokens.
      3. Caching the token and refreshing it before the ~1 h expiry.

    Credentials come exclusively from environment variables (I3).
    """

    def __init__(
        self,
        app_id: str,
        private_key_pem: str,
        installation_id: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        # We pass an empty string as token; _get_token() provides the real one.
        super().__init__(token="", client=client)
        self._app_id = app_id
        self._private_key_pem = private_key_pem
        self._installation_id = installation_id
        self._cached_token: str = ""
        self._token_expires_at: float = 0.0  # Unix timestamp
        # Attach an auth flow so EVERY request through this client — including the
        # base-class paginated GETs that call self._client directly (not via the
        # overridden _get/_post helpers) — mints + attaches the installation token.
        # Without this, _get_paginated sent an empty `Bearer ` header (App mode has
        # no static token). The flow caches via _get_token, so it does not re-mint.
        self._client.auth = _InstallationTokenAuth(self._get_token)

    def _mint_app_jwt(self) -> str:
        """Return a short-lived GitHub App JWT (10 min) signed with the private key."""
        now = int(time.time())
        payload = {
            "iat": now - 60,  # allow 60 s clock skew
            "exp": now + 600,  # 10 minutes
            "iss": self._app_id,
        }
        return jwt.encode(payload, self._private_key_pem, algorithm="RS256")

    async def _refresh_token(self) -> None:
        """Mint a fresh installation access token and cache it."""
        app_jwt = self._mint_app_jwt()
        headers = {
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        resp = await self._client.post(
            f"{_GITHUB_API}/app/installations/{self._installation_id}/access_tokens",
            headers=headers,
            json={},
        )
        resp.raise_for_status()
        data = resp.json()
        self._cached_token = str(data["token"])
        # GitHub expiry is an ISO-8601 string; parse conservatively with margin
        expires_at_str = str(data.get("expires_at", ""))
        if expires_at_str:
            expires_dt = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
            self._token_expires_at = expires_dt.timestamp() - _APP_TOKEN_REFRESH_MARGIN
        else:
            # Fallback: treat as ~55 min from now
            self._token_expires_at = time.time() + 3300

    async def _get_token(self) -> str:
        """Return a valid installation access token, refreshing if near expiry."""
        if not self._cached_token or time.time() >= self._token_expires_at:
            await self._refresh_token()
        return self._cached_token

    def _headers(self) -> dict[str, str]:
        # Override is intentionally sync; callers that need the latest token must
        # call await self._get_token() first.  The async methods below do this.
        # For the cached (still-valid) token case this is fine.
        return {
            "Authorization": f"Bearer {self._cached_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def _get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        self._token = await self._get_token()
        self._cached_token = self._token
        resp = await self._client.get(url, headers=self._headers(), params=params)
        resp.raise_for_status()
        return resp.json()

    async def _post(self, url: str, json: dict[str, Any]) -> Any:
        self._token = await self._get_token()
        self._cached_token = self._token
        resp = await self._client.post(url, headers=self._headers(), json=json)
        resp.raise_for_status()
        return resp.json()

    async def _patch(self, url: str, json: dict[str, Any]) -> Any:
        self._token = await self._get_token()
        self._cached_token = self._token
        resp = await self._client.patch(url, headers=self._headers(), json=json)
        resp.raise_for_status()
        return resp.json()

    async def _put(self, url: str, json: dict[str, Any]) -> Any:
        self._token = await self._get_token()
        self._cached_token = self._token
        resp = await self._client.put(url, headers=self._headers(), json=json)
        resp.raise_for_status()
        return resp.json()

    async def _delete(self, url: str) -> None:
        self._token = await self._get_token()
        self._cached_token = self._token
        resp = await self._client.delete(url, headers=self._headers())
        if resp.status_code not in (200, 204, 404):
            resp.raise_for_status()
