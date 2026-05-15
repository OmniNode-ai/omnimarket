# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Live GitHub API adapter for pr_lifecycle_fix_effect."""

from __future__ import annotations

import asyncio
import logging

from omnimarket.github_api import (
    graphql,
    rest_json,
    rest_no_content,
    split_repo,
)

logger = logging.getLogger(__name__)

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
        for run_id in run_ids:
            owner, repo_name = split_repo(repo)
            await asyncio.to_thread(
                rest_no_content,
                "POST",
                f"/repos/{owner}/{repo_name}/actions/runs/{run_id}/rerun-failed-jobs",
            )
        return f"rerequested {len(run_ids)} failed run(s) on {repo}#{pr_number}"

    async def resolve_conflicts(self, repo: str, pr_number: int) -> str:
        return await asyncio.to_thread(self._resolve_conflicts_sync, repo, pr_number)

    def _resolve_conflicts_sync(self, repo: str, pr_number: int) -> str:
        owner, repo_name = split_repo(repo)
        pr = rest_json("GET", f"/repos/{owner}/{repo_name}/pulls/{pr_number}")
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
                body={"expected_head_sha": head_sha},
            )
        except Exception as exc:
            raise RuntimeError(
                f"update-branch failed on {repo}#{pr_number}: {exc} — falling back to manual resolution"
            ) from exc

        refreshed = rest_json("GET", f"/repos/{owner}/{repo_name}/pulls/{pr_number}")
        refreshed_head = refreshed.get("head") or {}
        new_sha = refreshed_head.get("sha")
        if isinstance(new_sha, str) and new_sha:
            return new_sha
        return f"update-branch succeeded on {repo}#{pr_number}"

    def _failed_run_ids_sync(self, repo: str, pr_number: int) -> list[str]:
        owner, repo_name = split_repo(repo)
        data = graphql(
            _PR_STATUS_QUERY,
            {"owner": owner, "repo": repo_name, "prNumber": pr_number},
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
