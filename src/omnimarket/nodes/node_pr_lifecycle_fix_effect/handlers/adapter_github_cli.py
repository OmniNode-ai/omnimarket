# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Live GitHub API adapter for pr_lifecycle_fix_effect."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from omnimarket.github_api import (
    GitHubApiError,
    graphql,
    rest_json,
    rest_no_content,
    split_repo,
)
from omnimarket.inference.secret_store_resolver import (
    resolve_api_key,
    resolve_api_key_async,
)
from omnimarket.nodes.contract_topics import contract_secret_ref

logger = logging.getLogger(__name__)

_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"

# GitHub returns 409 when force-cancelling a workflow re-run that has not yet
# queued ("Cannot cancel a workflow re-run that has not yet queued"). The run is
# already terminal/stale from the cleanup's point of view, so a 409 is classified
# as stale metadata and skipped — never retried, never treated as a failure.
_HTTP_CONFLICT = 409


def _resolve_github_token() -> str:
    """Resolve the GitHub token from the contract-declared ref (OMN-12856).

    Sync variant — only safe to call from sync helpers running in
    ``asyncio.to_thread`` (e.g. ``_failed_run_ids_sync``,
    ``_resolve_conflicts_sync``).
    """
    ref = contract_secret_ref(_CONTRACT_PATH, "GITHUB_TOKEN")
    secret = resolve_api_key(ref)
    if secret is None:
        raise RuntimeError(
            f"api_key_ref {ref!r} resolved to None — "
            "ensure GITHUB_TOKEN is set in the secret store."
        )
    return secret.get_secret_value()


async def _resolve_github_token_async() -> str:
    """Async variant — call from async methods that are not inside asyncio.to_thread."""
    ref = contract_secret_ref(_CONTRACT_PATH, "GITHUB_TOKEN")
    secret = await resolve_api_key_async(ref)
    if secret is None:
        raise RuntimeError(
            f"api_key_ref {ref!r} resolved to None — "
            "ensure GITHUB_TOKEN is set in the secret store."
        )
    return secret.get_secret_value()


_PR_STATUS_QUERY = """
query($owner: String!, $repo: String!, $prNumber: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $prNumber) {
      commits(last: 1) {
        nodes {
          commit {
            statusCheckRollup {
              contexts(first: 100) {
                nodes {
                  __typename
                  ... on StatusContext {
                    targetUrl
                    state
                  }
                  ... on CheckRun {
                    detailsUrl
                    conclusion
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""


class GitHubCliAdapter:
    """Call GitHub APIs to rerun failed checks and update BEHIND branches."""

    async def rerun_failed_checks(self, repo: str, pr_number: int) -> str:
        run_ids = await asyncio.to_thread(self._failed_run_ids_sync, repo, pr_number)
        if not run_ids:
            return f"no failed checks on {repo}#{pr_number}"
        token = await _resolve_github_token_async()
        for run_id in run_ids:
            owner, repo_name = split_repo(repo)
            await asyncio.to_thread(
                rest_no_content,
                "POST",
                f"/repos/{owner}/{repo_name}/actions/runs/{run_id}/rerun-failed-jobs",
                token=token,
            )
        return f"rerequested {len(run_ids)} failed run(s) on {repo}#{pr_number}"

    async def cancel_obsolete_runs(self, repo: str, pr_number: int) -> str:
        """Force-cancel ``pull_request`` workflow runs on obsolete heads only.

        Stale-run cleanup (F4, OMN-13320). When a PR is pushed multiple times,
        GitHub leaves in-flight workflow runs queued against the now-superseded
        head SHAs. Those obsolete runs hold merge-queue slots and add latency, so
        cleanup force-cancels them via the ``force-cancel`` endpoint.

        The run tied to the **current** PR head SHA is never cancelled — cancelling
        it would re-trigger the very checks the merge is waiting on (the extra
        delay this fix exists to remove). Ancient re-runs that have not yet queued
        return HTTP 409; that is classified as stale metadata and skipped (not
        retried, not surfaced as an error).
        """
        return await asyncio.to_thread(self._cancel_obsolete_runs_sync, repo, pr_number)

    def _cancel_obsolete_runs_sync(self, repo: str, pr_number: int) -> str:
        token = _resolve_github_token()
        owner, repo_name = split_repo(repo)

        pr = rest_json(
            "GET", f"/repos/{owner}/{repo_name}/pulls/{pr_number}", token=token
        )
        head = pr.get("head") or {}
        head_sha = head.get("sha")
        head_ref = head.get("ref")
        if not isinstance(head_sha, str) or not head_sha:
            raise RuntimeError(
                f"cancel-obsolete-runs failed on {repo}#{pr_number}: missing head sha"
            )

        runs_path = f"/repos/{owner}/{repo_name}/actions/runs?event=pull_request"
        if isinstance(head_ref, str) and head_ref:
            runs_path += f"&branch={head_ref}"
        runs = rest_json("GET", runs_path, token=token)
        workflow_runs = runs.get("workflow_runs") or []

        cancelled = 0
        skipped_stale = 0
        protected_head = False
        for run in workflow_runs:
            if not isinstance(run, dict):
                continue
            run_id = run.get("id")
            run_head_sha = run.get("head_sha")
            if run_id is None or not isinstance(run_head_sha, str):
                continue
            # Never cancel the run tied to the current PR head — that is the run
            # the merge is waiting on; cancelling it just re-triggers the checks.
            if run_head_sha == head_sha:
                protected_head = True
                continue
            try:
                rest_no_content(
                    "POST",
                    f"/repos/{owner}/{repo_name}/actions/runs/{run_id}/force-cancel",
                    token=token,
                )
                cancelled += 1
            except GitHubApiError as exc:
                if exc.status_code == _HTTP_CONFLICT:
                    # Stale metadata: re-run that never queued. Skip, do not retry.
                    skipped_stale += 1
                    logger.info(
                        "cancel-obsolete-runs: skipping stale run %s on %s#%s "
                        "(HTTP 409, never queued)",
                        run_id,
                        repo,
                        pr_number,
                    )
                    continue
                raise

        return (
            f"force-cancelled {cancelled} obsolete-head run(s) on {repo}#{pr_number} "
            f"(skipped {skipped_stale} stale, "
            f"head run {'protected' if protected_head else 'absent'})"
        )

    async def resolve_conflicts(self, repo: str, pr_number: int) -> str:
        return await asyncio.to_thread(self._resolve_conflicts_sync, repo, pr_number)

    def _resolve_conflicts_sync(self, repo: str, pr_number: int) -> str:
        token = _resolve_github_token()
        owner, repo_name = split_repo(repo)
        pr = rest_json(
            "GET", f"/repos/{owner}/{repo_name}/pulls/{pr_number}", token=token
        )
        head = pr.get("head") or {}
        head_sha = head.get("sha")
        if not isinstance(head_sha, str) or not head_sha:
            raise RuntimeError(
                f"update-branch failed on {repo}#{pr_number}: missing head sha"
            )
        try:
            rest_json(
                "PUT",
                f"/repos/{owner}/{repo_name}/pulls/{pr_number}/update-branch",
                token=token,
                body={"expected_head_sha": head_sha},
            )
        except Exception as exc:
            raise RuntimeError(
                f"update-branch failed on {repo}#{pr_number}: {exc} — falling back to manual resolution"
            ) from exc

        refreshed = rest_json(
            "GET", f"/repos/{owner}/{repo_name}/pulls/{pr_number}", token=token
        )
        refreshed_head = refreshed.get("head") or {}
        new_sha = refreshed_head.get("sha")
        if isinstance(new_sha, str) and new_sha:
            return new_sha
        return f"update-branch succeeded on {repo}#{pr_number}"

    def _failed_run_ids_sync(self, repo: str, pr_number: int) -> list[str]:
        token = _resolve_github_token()
        owner, repo_name = split_repo(repo)
        data = graphql(
            _PR_STATUS_QUERY,
            {"owner": owner, "repo": repo_name, "prNumber": pr_number},
            token=token,
        )
        checks = (
            ((((data.get("repository") or {}).get("pullRequest")) or {}).get("commits"))
            or {}
        ).get("nodes", [])
        if not checks:
            return []
        rollup_nodes = (
            (
                (
                    (
                        (
                            (checks[0] if isinstance(checks[0], dict) else {}).get(
                                "commit"
                            )
                        )
                        or {}
                    ).get("statusCheckRollup")
                )
                or {}
            )
            .get("contexts", {})
            .get("nodes", [])
        )
        ids: list[str] = []
        seen: set[str] = set()
        for check in rollup_nodes:
            if not isinstance(check, dict):
                continue
            typename = check.get("__typename", "")
            if typename == "StatusContext":
                conclusion = (
                    "SUCCESS"
                    if (check.get("state") or "").upper() == "SUCCESS"
                    else (check.get("state") or "").upper()
                )
                details = check.get("targetUrl") or ""
            else:
                conclusion = (check.get("conclusion") or "").upper()
                details = check.get("detailsUrl") or ""
            if conclusion not in {
                "FAILURE",
                "TIMED_OUT",
                "CANCELLED",
                "ACTION_REQUIRED",
            }:
                continue
            run_id = _run_id_from_details_url(details)
            if run_id and run_id not in seen:
                seen.add(run_id)
                ids.append(run_id)
        return ids


def _run_id_from_details_url(details_url: str) -> str | None:
    """Parse a GitHub check ``detailsUrl`` of the form
    ``https://github.com/<owner>/<repo>/actions/runs/<run_id>/...`` → ``<run_id>``.
    """
    if not details_url or "/actions/runs/" not in details_url:
        return None
    tail = details_url.split("/actions/runs/", 1)[1]
    run_id = tail.split("/", 1)[0].split("?", 1)[0]
    return run_id or None


__all__ = ["GitHubCliAdapter"]
