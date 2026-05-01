"""Minimal GitHub REST/GraphQL helper for omnimarket effect nodes."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, cast

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


def _token() -> str:
    token = os.environ.get("GH_PAT", "")
    if not token:
        raise GitHubApiError("GH_PAT environment variable is not set")
    return token


def _base_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_token()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": _GITHUB_API_VERSION,
    }


def rest_json(
    method: str,
    path: str,
    *,
    body: dict[str, object] | None = None,
) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = _base_headers()
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


def rest_text(
    method: str,
    path: str,
    *,
    body: dict[str, object] | None = None,
) -> str:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = _base_headers()
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
            return cast(bytes, resp.read()).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise GitHubApiError(detail or str(exc)) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise GitHubApiError(str(exc)) from exc


def rest_no_content(
    method: str,
    path: str,
    *,
    body: dict[str, object] | None = None,
) -> None:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = _base_headers()
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


def graphql(query: str, variables: dict[str, object]) -> dict[str, Any]:
    headers = _base_headers()
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
