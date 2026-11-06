# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure GraphQL query text and response normalizers for the PR poller.

No I/O lives here; the HTTP transport stays in :mod:`omnimarket.github_api`.
"""

from __future__ import annotations

from typing import Any

PR_GRAPHQL_QUERY = """
query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: [OPEN], first: 100, after: $after, orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo {
        hasNextPage
        endCursor
      }
      nodes {
        id
        number
        title
        isDraft
        mergeable
        mergeStateStatus
        reviewDecision
        headRefName
        baseRefName
        headRefOid
        reviewThreads(first: 50) {
          nodes {
            isResolved
            comments(first: 20) {
              nodes {
                id
              }
            }
          }
        }
        files(first: 100) {
          nodes {
            path
          }
        }
        labels(first: 20) {
          nodes { name }
        }
        statusCheckRollup: commits(last: 1) {
          nodes {
            commit {
              statusCheckRollup {
                contexts(first: 100) {
                  nodes {
                    __typename
                    ... on StatusContext {
                      context
                      state
                      description
                      targetUrl
                    }
                    ... on CheckRun {
                      name
                      status
                      conclusion
                      detailsUrl
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
}
"""


def normalize_open_pr_rollup(
    rollup_nodes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    normalized_rollup: list[dict[str, Any]] = []
    for ctx in rollup_nodes:
        typename = ctx.get("__typename", "")
        if typename == "CheckRun":
            normalized_rollup.append(
                {
                    "name": ctx.get("name", ""),
                    "conclusion": (ctx.get("conclusion") or "").upper(),
                    "status": ctx.get("status", ""),
                    "detailsUrl": ctx.get("detailsUrl", ""),
                }
            )
        elif typename == "StatusContext":
            state = (ctx.get("state") or "").upper()
            normalized_rollup.append(
                {
                    "context": ctx.get("context", ""),
                    "conclusion": "SUCCESS" if state == "SUCCESS" else state,
                    "state": ctx.get("state", ""),
                    "detailsUrl": ctx.get("targetUrl", ""),
                }
            )
    for ctx in normalized_rollup:
        ctx["isRequired"] = True
    return normalized_rollup


def normalize_review_threads(
    raw_threads: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    normalized_threads: list[dict[str, Any]] = []
    for thread in raw_threads:
        comment_nodes = ((thread.get("comments") or {}).get("nodes")) or []
        normalized_threads.append(
            {
                "isResolved": bool(thread.get("isResolved", False)),
                "comments": [
                    {"id": comment["id"]}
                    for comment in comment_nodes
                    if isinstance(comment, dict) and comment.get("id")
                ],
            }
        )
    return normalized_threads
