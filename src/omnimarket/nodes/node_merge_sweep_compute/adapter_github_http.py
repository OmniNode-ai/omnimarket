# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""GitHub HTTP adapter for node_merge_sweep — zero external dependencies.

Uses GitHub GraphQL API (via urllib) to fetch open PRs with rich status fields,
and REST API for branch protection. Reads GH_PAT from env (fail-fast, no fallback).

This is the ONLY file in node_merge_sweep that touches the network.
Everything else works against GitHubPrFetchProtocol.

OMN-MERGE-SWEEP.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

from omnimarket.nodes.node_merge_sweep_compute.protocols import (
    GitHubPrFetchProtocol,
    GitHubTransportError,
)

_log = logging.getLogger(__name__)

_GITHUB_GRAPHQL = "https://api.github.com/graphql"
_GITHUB_REST = "https://api.github.com"
_REQUEST_TIMEOUT = 30

# GraphQL query for open PRs with all the fields we need.
# Mirrors the output of `gh pr list --json number,title,mergeable,mergeStateStatus,
# statusCheckRollup,reviewDecision,headRefName,baseRefName,isDraft,labels,headRefOid`
_PR_GRAPHQL_QUERY = """
query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: [OPEN], first: 100, after: $after, orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo {
        hasNextPage
        endCursor
      }
      nodes {
        number
        title
        isDraft
        mergeable
        mergeStateStatus
        reviewDecision
        headRefName
        baseRefName
        headRefOid
        labels(first: 20) {
          nodes { name }
        }
        statusCheckRollup: commits(last: 1) {
          nodes {
            commit {
              statusCheckRollup {
                contexts {
                  ... on StatusContext {
                    context
                    state
                    description
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


def _split_repo(repo: str) -> tuple[str, str]:
    """Split 'OmniNode-ai/omnimarket' into ('OmniNode-ai', 'omnimarket')."""
    parts = repo.split("/", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid repo format: {repo!r} — expected 'org/name'")
    return parts[0], parts[1]


class GitHubHttpClient(GitHubPrFetchProtocol):
    """Real GitHub HTTP client using GraphQL for PRs + REST for branch protection.

    Reads GH_PAT from the environment at construction time (fail-fast).
    """

    def __init__(self) -> None:
        token = os.environ.get("GH_PAT", "")
        if not token:
            raise RuntimeError(
                "GH_PAT environment variable is not set. "
                "Export it before running node_merge_sweep."
            )
        self._token = token

    def _graphql(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
        """Execute a GraphQL query. Returns the data dict.

        Raises GitHubTransportError on network, auth, or decode failures so
        callers can distinguish transport failure from an empty result.
        """
        payload = json.dumps({"query": query, "variables": variables}).encode()
        req = urllib.request.Request(
            _GITHUB_GRAPHQL,
            data=payload,
            headers={
                "Authorization": f"bearer {self._token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
                body = json.loads(resp.read())
        except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
            raise GitHubTransportError(f"GraphQL request failed: {exc}") from exc
        if "errors" in body:
            _log.warning("GraphQL errors: %s", body["errors"])
            raise GitHubTransportError(f"GraphQL returned errors: {body['errors']}")
        data = body.get("data")
        return data if isinstance(data, dict) else {}

    def _rest_get(self, path: str) -> dict[str, Any] | None:
        """Execute a REST API GET. Returns parsed JSON or None for 404.

        Raises GitHubTransportError on network, auth, or non-404 HTTP failures.
        Returns None only for 404 (resource genuinely absent).
        """
        url = f"{_GITHUB_REST}{path}"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"bearer {self._token}",
                "Accept": "application/vnd.github+json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
                return json.loads(resp.read())  # type: ignore[no-any-return]
        except urllib.error.HTTPError as exc:
            # 404 = no branch protection configured — that's a valid business state
            if exc.code == 404:
                return None
            raise GitHubTransportError(f"REST API error for {path}: {exc}") from exc
        except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
            raise GitHubTransportError(
                f"REST API request failed for {path}: {exc}"
            ) from exc

    def fetch_open_prs(self, repo: str) -> list[dict[str, Any]]:
        """Fetch open PRs via GitHub GraphQL API."""
        owner, name = _split_repo(repo)
        all_prs: list[dict[str, Any]] = []
        cursor: str | None = None

        while True:
            variables: dict[str, object] = {"owner": owner, "name": name}
            if cursor:
                variables["after"] = cursor

            data = self._graphql(_PR_GRAPHQL_QUERY, variables)
            repo_data = data.get("repository")
            if not repo_data:
                break

            pr_conn = repo_data.get("pullRequests", {})
            nodes = pr_conn.get("nodes", [])
            for node in nodes:
                # Flatten labels; skip nodes without a name key (malformed API response)
                label_nodes = (node.get("labels") or {}).get("nodes", [])
                node["labels"] = [
                    {"name": ln["name"]} for ln in label_nodes if ln and "name" in ln
                ]

                # Flatten statusCheckRollup from nested commit structure
                rollup_nodes = (
                    ((node.get("statusCheckRollup") or {}).get("nodes") or [{}])[0]
                    .get("commit", {})
                    .get("statusCheckRollup", {})
                    .get("contexts", [])
                )
                # Normalize to the same shape as gh pr list --json
                normalized_rollup: list[dict[str, Any]] = []
                for ctx in rollup_nodes:
                    if "conclusion" in ctx:
                        # CheckRun
                        normalized_rollup.append(
                            {
                                "name": ctx.get("name", ""),
                                "conclusion": (ctx.get("conclusion") or "").upper(),
                                "status": ctx.get("status", ""),
                            }
                        )
                    else:
                        # StatusContext
                        state = (ctx.get("state") or "").upper()
                        normalized_rollup.append(
                            {
                                "context": ctx.get("context", ""),
                                "conclusion": "SUCCESS"
                                if state == "SUCCESS"
                                else state,
                                "state": ctx.get("state", ""),
                            }
                        )
                # Mark isRequired — GitHub GraphQL doesn't expose this directly
                # in the statusCheckRollup. We treat all as required for safety;
                # the consumer's _is_green() already handles the empty case.
                for ctx in normalized_rollup:
                    ctx["isRequired"] = True
                node["statusCheckRollup"] = normalized_rollup
                all_prs.append(node)

            page_info = pr_conn.get("pageInfo", {})
            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")

        return all_prs

    def fetch_branch_protection(self, repo: str) -> int | None:
        """Fetch required_approving_review_count via REST API."""
        data = self._rest_get(f"/repos/{repo}/branches/main/protection")
        if data is None:
            return None
        reviews = data.get("required_pull_request_reviews")
        if not isinstance(reviews, dict):
            return None
        raw = reviews.get("required_approving_review_count")
        if isinstance(raw, int):
            return raw
        return None


__all__: list[str] = ["GitHubHttpClient"]
