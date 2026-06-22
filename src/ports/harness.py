"""RealHarnessPort — anthropics/claude-code-action HarnessPort implementation."""

from __future__ import annotations

import json
from typing import Any

import httpx

from src.domain.types import (
    DispatchContext,
    PRRef,
    RunHandle,
    RunStatus,
)
from src.ports.github import (
    _DISPATCH_WORKFLOW_NAME,
    _GITHUB_API,
    _parse_run_conclusion,
    _parse_run_state,
)

# claude-code-action workflow filename (dispatched on the target repo).
# The base name is shared with github.py to avoid duplication.
_CLAUDE_CODE_ACTION_WORKFLOW = f"{_DISPATCH_WORKFLOW_NAME}.yml"

# Workflow ref (branch/tag) for triggering the claude-code-action workflow
_CLAUDE_CODE_ACTION_REF = "main"


class RealHarnessPort:
    """HarnessPort that dispatches via anthropics/claude-code-action.

    Dispatch is fire-and-forget: the workflow is triggered on the forge repo and
    a RunHandle is returned immediately.  The Engine polls get_run_status.

    The harness API key is never logged or injected into the sandbox; it is held
    only by this adapter (invariant I3).
    """

    def __init__(
        self,
        forge_token: str,
        repo_owner: str,
        repo_name: str,
        harness_api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._forge_token = forge_token
        self._repo_owner = repo_owner
        self._repo_name = repo_name
        # harness_api_key reserved for future authenticated harness APIs
        self._harness_api_key = harness_api_key
        self._client = client or httpx.AsyncClient(
            base_url=_GITHUB_API,
            headers={
                "Authorization": f"Bearer {forge_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._forge_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def _post(self, url: str, json_body: dict[str, Any]) -> Any:
        resp = await self._client.post(url, headers=self._headers(), json=json_body)
        resp.raise_for_status()
        # 204 No Content is the normal success for workflow_dispatch
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()

    async def _get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        resp = await self._client.get(url, headers=self._headers(), params=params)
        resp.raise_for_status()
        return resp.json()

    async def _delete(self, url: str) -> None:
        resp = await self._client.delete(url, headers=self._headers())
        if resp.status_code not in (200, 204):
            resp.raise_for_status()

    # ------------------------------------------------------------------
    # HarnessPort implementation
    # ------------------------------------------------------------------

    async def dispatch(self, context: DispatchContext) -> RunHandle:
        """Dispatch a claude-code-action workflow run (fire-and-forget).

        Triggers workflow_dispatch on the target repo.  The workflow ID returned
        by GitHub is used to construct a RunHandle.  Returns immediately.

        Security — I3 / I9 / D2:
          - FORGE_TOKEN and HARNESS_API_KEY are NEVER included in workflow inputs.
            This adapter holds credentials but does not forward them to the sandbox.
          - allowed_agent_refs is serialized into workflow inputs as a JSON string so
            the agent runtime (claude-code-action) can enforce the allow-set at
            execution time.
          - Out-of-set spawn rejection is enforced by the agent-runtime layer
            (claude-code-action contract + harness), NOT by this dispatch call.
            This adapter cannot observe sub-agent spawns that occur inside the
            sandboxed workflow execution.  The serialized allowed_agent_refs value
            is the authoritative contract boundary passed to that runtime.
        """
        # Build inputs from DispatchContext (I3: never expose forge credentials)
        inputs: dict[str, str] = {
            "contract": context.contract,
            "model": context.model,
            "max_turns": str(context.max_turns),
            "forge_token_scope": context.forge_token_scope,
        }
        if context.issue_ref is not None:
            inputs["issue_number"] = str(context.issue_ref.number)
            inputs["issue_repo"] = (
                f"{context.issue_ref.repo.owner}/{context.issue_ref.repo.name}"
            )
        if context.pr_ref is not None:
            inputs["pr_number"] = str(context.pr_ref.number)
        if context.allowed_agent_refs is not None:
            inputs["allowed_agent_refs"] = json.dumps(context.allowed_agent_refs)

        ref = _CLAUDE_CODE_ACTION_REF
        await self._post(
            f"{_GITHUB_API}/repos/{self._repo_owner}/{self._repo_name}"
            f"/actions/workflows/{_CLAUDE_CODE_ACTION_WORKFLOW}/dispatches",
            json_body={"ref": ref, "inputs": inputs},
        )

        # GitHub workflow_dispatch returns 204 with no run ID.  We resolve the
        # most recently created run for this workflow to build the handle.
        run_id = await self._resolve_latest_run_id()
        return RunHandle(run_id=run_id)

    async def _resolve_latest_run_id(self) -> str:
        """Resolve the most recently triggered workflow run ID."""
        try:
            data = await self._get(
                f"{_GITHUB_API}/repos/{self._repo_owner}/{self._repo_name}"
                f"/actions/workflows/{_CLAUDE_CODE_ACTION_WORKFLOW}/runs",
                params={"per_page": 1},
            )
            runs = data.get("workflow_runs", [])
            if runs:
                return str(runs[0]["id"])
        except httpx.HTTPStatusError:
            pass
        # Fallback: generate a synthetic ID so dispatch never blocks
        return f"dispatch-{id(self)}"

    async def trigger_workflow(
        self,
        name: str,
        ref: str,
        inputs: dict[str, object],
    ) -> None:
        """Trigger an arbitrary GitHub Actions workflow by filename."""
        await self._post(
            f"{_GITHUB_API}/repos/{self._repo_owner}/{self._repo_name}"
            f"/actions/workflows/{name}/dispatches",
            json_body={"ref": ref, "inputs": inputs},
        )

    async def trigger_ci(self, pr_ref: PRRef) -> None:
        """Re-trigger CI on a PR by re-running the most recent failed workflow run.

        Implements the RC-1 'trigger-ci' recovery by calling the GitHub Actions
        rerun-failed-jobs API on the most recent workflow run for the PR's head SHA.

        Strategy:
          1. Fetch the PR to get the head SHA.
          2. List workflow runs for the head SHA to find the most recent one.
          3. POST to /actions/runs/{run_id}/rerun-failed-jobs.

        Raises httpx.HTTPStatusError on unexpected failures (does NOT swallow errors
        silently — callers can decide to log/ignore if needed).
        """
        pr_data = await self._get(
            f"{_GITHUB_API}/repos/{pr_ref.repo.owner}/{pr_ref.repo.name}"
            f"/pulls/{pr_ref.number}"
        )
        head_sha = str(pr_data["head"]["sha"])

        # Find the most recent workflow run for this commit
        runs_data = await self._get(
            f"{_GITHUB_API}/repos/{pr_ref.repo.owner}/{pr_ref.repo.name}"
            f"/actions/runs",
            params={"head_sha": head_sha, "per_page": 1},
        )
        runs = runs_data.get("workflow_runs", [])
        if not runs:
            # No workflow runs found for this SHA — nothing to rerun
            return

        run_id = str(runs[0]["id"])

        # Re-run failed jobs for the most recent run
        await self._post(
            f"{_GITHUB_API}/repos/{pr_ref.repo.owner}/{pr_ref.repo.name}"
            f"/actions/runs/{run_id}/rerun-failed-jobs",
            json_body={},
        )

    async def get_run_status(self, handle: RunHandle) -> RunStatus:
        """Poll the GitHub Actions run status for the given handle."""
        try:
            data = await self._get(
                f"{_GITHUB_API}/repos/{self._repo_owner}/{self._repo_name}"
                f"/actions/runs/{handle.run_id}"
            )
            status_str = str(data.get("status", "queued"))
            conclusion_str = data.get("conclusion")
            state = _parse_run_state(None, status_str)
            conclusion = _parse_run_conclusion(
                str(conclusion_str) if conclusion_str else None
            )
            return RunStatus(state=state, conclusion=conclusion)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return RunStatus(state="queued")
            raise

    async def cancel(self, handle: RunHandle) -> None:
        """Cancel the GitHub Actions run for the given handle (idempotent)."""
        try:
            await self._post(
                f"{_GITHUB_API}/repos/{self._repo_owner}/{self._repo_name}"
                f"/actions/runs/{handle.run_id}/cancel",
                json_body={},
            )
        except httpx.HTTPStatusError as exc:
            # 404 = run already gone (terminal no-op per SPEC §9.2)
            # 409 = already in a terminal state (idempotent no-op per SPEC §9.2)
            # 422 = already completed (idempotent no-op per SPEC §9.2)
            if exc.response.status_code not in (404, 409, 422):
                raise
