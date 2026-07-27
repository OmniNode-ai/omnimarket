# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read transport for node_github_repo_gateway_effect.

Defines the narrow read surface the gateway's read functions depend on and a
real implementation that REUSES the existing typed GitHub transport — never a
subprocess ``gh`` shell-out:

* repo-scoped reads (``fetch_open_prs`` / ``fetch_branch_protection``) delegate
  verbatim to :class:`GitHubHttpTransport`;
* PR-scoped reads use the module-level :func:`omnimarket.github_api.graphql`
  helper with a single-PR query, normalized to the same stable shapes
  ``GitHubHttpTransport`` produces.

The token is passed in already-resolved; this module never reads any process
environment variable.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from omnimarket.github_api import (
    GitHubApiError,
    GitHubHttpTransport,
    graphql,
    split_repo,
)
from omnimarket.nodes.node_merge_sweep_compute.protocols import GitHubTransportError

# Single-PR query mirroring the fields the read functions consume. Uses the same
# inline-fragment shape as the merge-sweep list query (fragments live inside
# contexts.nodes to avoid GitHub's cannotSpreadFragment error).
_PR_DETAIL_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      number
      title
      isDraft
      mergeable
      mergeStateStatus
      reviewDecision
      headRefName
      baseRefName
      merged
      mergeCommit { oid }
      reviewThreads(first: 100) {
        nodes { isResolved }
      }
      commits(last: 1) {
        nodes {
          commit {
            statusCheckRollup {
              state
              contexts(first: 100) {
                nodes {
                  __typename
                  ... on StatusContext {
                    context
                    state
                  }
                  ... on CheckRun {
                    name
                    status
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


@runtime_checkable
class GitHubReadTransportProtocol(Protocol):
    """The narrow read surface the gateway's read functions depend on."""

    def fetch_open_prs(self, repo: str) -> list[dict[str, Any]]:
        """Return open PRs for ``repo`` (org/name)."""
        ...

    def fetch_branch_protection(self, repo: str) -> int | None:
        """Return required approving review count for the default branch."""
        ...

    def fetch_pr_detail(self, repo: str, pr_number: int) -> dict[str, Any]:
        """Return a normalized detail dict for a single PR.

        Shape::

            {
              "number": int, "title": str, "isDraft": bool,
              "mergeable": str, "mergeStateStatus": str,
              "reviewDecision": str | None,
              "headRefName": str, "baseRefName": str,
              "merged": bool, "mergeCommitOid": str | None,
              "reviewThreads": [{"isResolved": bool}, ...],
              "statusCheckRollup": [
                  {"name": str, "conclusion": str, "status": str,
                   "isRequired": bool}, ...
              ],
              "rollupState": str,   # SUCCESS/FAILURE/PENDING/... or ""
            }
        """
        ...


def _normalize_rollup_contexts(
    rollup_nodes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Normalize StatusContext | CheckRun union nodes to a stable dict shape.

    Mirrors GitHubHttpTransport's normalization so PR-scoped and repo-scoped reads
    speak the same contexts vocabulary.
    """
    normalized: list[dict[str, Any]] = []
    for ctx in rollup_nodes:
        typename = ctx.get("__typename", "")
        if typename == "CheckRun":
            normalized.append(
                {
                    "name": ctx.get("name", ""),
                    "conclusion": (ctx.get("conclusion") or "").upper(),
                    "status": ctx.get("status", ""),
                }
            )
        elif typename == "StatusContext":
            state = (ctx.get("state") or "").upper()
            normalized.append(
                {
                    "name": ctx.get("context", ""),
                    "conclusion": "SUCCESS" if state == "SUCCESS" else state,
                    "status": ctx.get("state", ""),
                }
            )
    # GitHub GraphQL does not expose isRequired on the rollup; treat all as
    # required for safety (the classifier's empty-set case is already handled).
    for ctx in normalized:
        ctx["isRequired"] = True
    return normalized


class RealGitHubReadTransport(GitHubReadTransportProtocol):
    """Real transport reusing GitHubHttpTransport + the github_api graphql helper."""

    def __init__(self, token: str) -> None:
        if not token:
            raise RuntimeError(
                "GitHub token must not be empty. Resolve it via the contract "
                "api_key_ref before constructing RealGitHubReadTransport."
            )
        self._token = token
        self._client = GitHubHttpTransport(token)

    def fetch_open_prs(self, repo: str) -> list[dict[str, Any]]:
        try:
            return self._client.fetch_open_prs(repo)
        except GitHubApiError as exc:
            raise GitHubTransportError(f"GitHub request failed: {exc}") from exc

    def fetch_branch_protection(self, repo: str) -> int | None:
        try:
            return self._client.fetch_branch_protection(repo)
        except GitHubApiError as exc:
            raise GitHubTransportError(f"GitHub request failed: {exc}") from exc

    def fetch_pr_detail(self, repo: str, pr_number: int) -> dict[str, Any]:
        owner, name = split_repo(repo)
        try:
            data = graphql(
                _PR_DETAIL_QUERY,
                {"owner": owner, "name": name, "number": pr_number},
                token=self._token,
            )
        except GitHubApiError as exc:
            raise GitHubTransportError(f"GitHub request failed: {exc}") from exc
        pr = ((data.get("repository") or {}).get("pullRequest")) or {}
        if not pr:
            raise RuntimeError(f"PR not found: {repo}#{pr_number}")

        commit_nodes = (pr.get("commits") or {}).get("nodes") or []
        rollup_obj: dict[str, Any] = {}
        if commit_nodes:
            rollup_obj = (commit_nodes[0].get("commit") or {}).get(
                "statusCheckRollup"
            ) or {}
        rollup_nodes = (rollup_obj.get("contexts") or {}).get("nodes") or []

        thread_nodes = (pr.get("reviewThreads") or {}).get("nodes") or []
        review_threads = [
            {"isResolved": bool(t.get("isResolved", False))} for t in thread_nodes
        ]

        merge_commit = pr.get("mergeCommit") or {}
        return {
            "number": pr.get("number", pr_number),
            "title": pr.get("title", ""),
            "isDraft": bool(pr.get("isDraft", False)),
            "mergeable": pr.get("mergeable", "UNKNOWN"),
            "mergeStateStatus": pr.get("mergeStateStatus", "UNKNOWN"),
            "reviewDecision": pr.get("reviewDecision"),
            "headRefName": pr.get("headRefName", ""),
            "baseRefName": pr.get("baseRefName", ""),
            "merged": bool(pr.get("merged", False)),
            "mergeCommitOid": merge_commit.get("oid")
            if isinstance(merge_commit, dict)
            else None,
            "reviewThreads": review_threads,
            "statusCheckRollup": _normalize_rollup_contexts(rollup_nodes),
            "rollupState": (rollup_obj.get("state") or "").upper(),
        }


__all__: list[str] = [
    "GitHubReadTransportProtocol",
    "RealGitHubReadTransport",
]
