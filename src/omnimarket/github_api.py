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

_GITHUB_REST = "https://api.github.com"
_GITHUB_GRAPHQL = "https://api.github.com/graphql"
_GITHUB_API_VERSION = "2026-03-10"
_REQUEST_TIMEOUT = 30.0


class GitHubApiError(RuntimeError):
    """Raised when a GitHub API request fails."""


def split_repo(repo: str) -> tuple[str, str]:
    owner, sep, repo_name = repo.partition("/")
    if not owner or not sep or not repo_name:
        raise GitHubApiError(f"invalid repo slug: {repo!r}")
    return owner, repo_name


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
        raise GitHubApiError(detail or str(exc)) from exc
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
        raise GitHubApiError(detail or str(exc)) from exc
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
        raise GitHubApiError(detail or str(exc)) from exc
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise GitHubApiError(str(exc)) from exc
    if body.get("errors"):
        raise GitHubApiError(json.dumps(body["errors"]))
    data = body.get("data")
    if not isinstance(data, dict):
        raise GitHubApiError("missing GraphQL data payload")
    return data
