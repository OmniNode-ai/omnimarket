# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Minimal GitHub REST/GraphQL helper for omnimarket effect nodes.

The GitHub token is NEVER read from os.environ here.  All public functions
require an explicit ``token`` parameter — the caller resolves the value
through the contract-declared ``api_key_ref`` (ref name: ``GITHUB_TOKEN``)
via :func:`omnimarket.inference.secret_store_resolver.resolve_api_key` at
the effect boundary, then passes the resolved bare string here.

This keeps the I/O helper ignorant of env-var names (rule: no raw env reads
in infrastructure helpers).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from omnimarket.config.service_endpoints import GITHUB_GRAPHQL_URL, GITHUB_REST_URL

_GITHUB_REST = GITHUB_REST_URL
_GITHUB_GRAPHQL = GITHUB_GRAPHQL_URL
_GITHUB_API_VERSION = "2026-03-10"
_REQUEST_TIMEOUT = 30.0

_PR_GRAPHQL_QUERY = """
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


class GitHubApiError(RuntimeError):
    """Raised when a GitHub API request fails.

    ``status_code`` carries the HTTP status when the failure originated from an
    :class:`urllib.error.HTTPError` (``None`` for network/decode failures). Callers
    that need to classify a specific status — e.g. HTTP 409 stale-metadata on a
    workflow-run cancel — branch on ``status_code`` rather than string-matching the
    message.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def split_repo(repo: str) -> tuple[str, str]:
    owner, sep, repo_name = repo.partition("/")
    if not owner or not sep or not repo_name:
        raise GitHubApiError(f"invalid repo slug: {repo!r}")
    return owner, repo_name


def _normalize_open_pr_rollup(
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


def _normalize_review_threads(
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


def _base_headers(token: str) -> dict[str, str]:
    """Build standard GitHub API headers from a resolved bearer token.

    Args:
        token: Resolved GitHub token value (never an env-var name).

    Returns:
        Header dict including Authorization, Accept, and API-version fields.
    """
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": _GITHUB_API_VERSION,
    }


def rest_json(
    method: str,
    path: str,
    *,
    token: str,
    body: dict[str, object] | None = None,
) -> dict[str, Any]:
    """Execute a GitHub REST API call returning a JSON dict.

    Args:
        method: HTTP method (GET, POST, PATCH, …).
        path: API path starting with ``/`` (appended to https://api.github.com).
        token: Resolved GitHub bearer token.
        body: Optional JSON request body.

    Raises:
        GitHubApiError: On HTTP, network, or JSON decode failures.
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = _base_headers(token)
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"{_GITHUB_REST}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise GitHubApiError(detail or str(exc), status_code=exc.code) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise GitHubApiError(str(exc)) from exc

    if not raw:
        return {}
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise GitHubApiError(f"invalid JSON response for {path}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise GitHubApiError(f"unexpected JSON response type for {path}")
    return parsed


def rest_json_array(
    method: str,
    path: str,
    *,
    token: str,
) -> list[dict[str, Any]]:
    """Execute a GitHub REST API call returning a JSON array of objects.

    Sibling of :func:`rest_json` for the list-returning endpoints (e.g.
    ``pulls/{n}/files``, ``pulls/{n}/commits``) where the top-level payload is a
    JSON array rather than an object — ``rest_json`` rejects those with
    ``GitHubApiError`` by design (dict-only contract).

    Args:
        method: HTTP method (GET, POST, PATCH, …).
        path: API path starting with ``/`` (appended to https://api.github.com).
        token: Resolved GitHub bearer token.

    Raises:
        GitHubApiError: On HTTP, network, or JSON decode failures.
    """
    headers = _base_headers(token)
    req = urllib.request.Request(
        f"{_GITHUB_REST}{path}",
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise GitHubApiError(detail or str(exc), status_code=exc.code) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise GitHubApiError(str(exc)) from exc

    if not raw:
        return []
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise GitHubApiError(f"invalid JSON response for {path}: {exc}") from exc
    if not isinstance(parsed, list):
        raise GitHubApiError(f"unexpected JSON response type for {path}")
    return parsed


def rest_no_content(
    method: str,
    path: str,
    *,
    token: str,
    body: dict[str, object] | None = None,
) -> None:
    """Execute a GitHub REST API call that returns no body (204/etc.).

    Args:
        method: HTTP method.
        path: API path starting with ``/``.
        token: Resolved GitHub bearer token.
        body: Optional JSON request body.

    Raises:
        GitHubApiError: On HTTP or network failures.
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = _base_headers(token)
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"{_GITHUB_REST}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT):
            return
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise GitHubApiError(detail or str(exc), status_code=exc.code) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise GitHubApiError(str(exc)) from exc


def graphql(
    query: str,
    variables: dict[str, object],
    *,
    token: str,
) -> dict[str, Any]:
    """Execute a GitHub GraphQL query.

    Args:
        query: GraphQL query/mutation string.
        variables: Variables dict for the query.
        token: Resolved GitHub bearer token.

    Raises:
        GitHubApiError: On HTTP, network, JSON decode, or GraphQL error responses.
    """
    headers = _base_headers(token)
    headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        _GITHUB_GRAPHQL,
        data=json.dumps({"query": query, "variables": variables}).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise GitHubApiError(detail or str(exc), status_code=exc.code) from exc
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise GitHubApiError(str(exc)) from exc
    if body.get("errors"):
        raise GitHubApiError(json.dumps(body["errors"]))
    data = body.get("data")
    if not isinstance(data, dict):
        raise GitHubApiError("missing GraphQL data payload")
    return data


class GitHubHttpTransport:
    """Reusable GitHub HTTP client using GraphQL for PRs and REST for protection.

    The caller must pass a resolved bearer token. This class deliberately raises
    ``GitHubApiError`` so node-specific adapters can translate failures into
    their own transport error contracts.
    """

    def __init__(self, token: str) -> None:
        if not token:
            raise RuntimeError(
                "GitHub token must not be empty. Resolve it via the contract "
                "api_key_ref before constructing GitHubHttpTransport."
            )
        self._token = token

    def _graphql(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
        return graphql(query, variables, token=self._token)

    def _rest_get(self, path: str) -> dict[str, Any] | None:
        try:
            return rest_json("GET", path, token=self._token)
        except GitHubApiError as exc:
            if exc.status_code == 404:
                return None
            raise

    def fetch_open_prs(self, repo: str) -> list[dict[str, Any]]:
        """Fetch open PRs via GitHub GraphQL API."""
        owner, name = split_repo(repo)
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
                label_nodes = (node.get("labels") or {}).get("nodes", [])
                node["labels"] = [
                    {"name": ln["name"]} for ln in label_nodes if ln and "name" in ln
                ]

                commit_nodes = (node.get("statusCheckRollup") or {}).get("nodes") or []
                rollup_nodes: list[dict[str, Any]] = []
                if commit_nodes:
                    rollup_nodes = (
                        (commit_nodes[0].get("commit") or {}).get(
                            "statusCheckRollup", {}
                        )
                        or {}
                    ).get("contexts", {}).get("nodes", []) or []
                node["statusCheckRollup"] = _normalize_open_pr_rollup(rollup_nodes)

                raw_threads = (node.get("reviewThreads") or {}).get("nodes") or []
                node["reviewThreads"] = _normalize_review_threads(raw_threads)

                raw_files = (node.get("files") or {}).get("nodes") or []
                node["files"] = [
                    {"path": file_node["path"]}
                    for file_node in raw_files
                    if isinstance(file_node, dict) and file_node.get("path")
                ]
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
