# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""GitHub bridge for node_github_review_effect (OMN-13212 / B2).

Async httpx-based GitHub REST adapter that the EFFECT node composes internally to
perform review-side I/O (post threads, poll resolutions, post the report). Moved
from the deleted ``node_pr_review_bot.adapter_github_bridge``; the only behavioural
change is the token is supplied to the constructor (resolved at the effect's
``handle()`` boundary via the canonical secret-store resolver) rather than read
from ``os.environ`` inside the bridge.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

_GITHUB_API_BASE = "https://api.github.com"
_RATE_LIMIT_THRESHOLD = 50
_MAX_RETRIES = 3
_BACKOFF_BASE_SECONDS = 2.0


class PrMetadata(BaseModel):
    """Minimal PR metadata needed by the review effect."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    number: int
    title: str
    body: str
    author: str
    head_sha: str
    base_ref: str
    head_ref: str
    state: str
    files_changed: tuple[str, ...] = Field(default_factory=tuple)


class ReviewThread(BaseModel):
    """A single GitHub pull request review comment (thread)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int
    body: str
    path: str
    line: int | None
    commit_id: str
    user_login: str
    created_at: str
    updated_at: str
    in_reply_to_id: int | None
    resolved: bool


class AdapterGithubReviewBridge:
    """Async httpx-based GitHub REST adapter for review-side I/O.

    The GitHub token is supplied to the constructor (already resolved from the
    contract-declared ``api_key_ref`` via the canonical secret-store resolver).
    Implements exponential backoff when the rate-limit headroom drops below the
    threshold.
    """

    def __init__(self, *, token: str) -> None:
        if not token.strip():
            raise ValueError("AdapterGithubReviewBridge requires a non-empty token")
        self._token = token

    def _build_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _check_rate_limit(self, response: httpx.Response) -> None:
        remaining = response.headers.get("X-RateLimit-Remaining")
        if remaining is not None and int(remaining) < _RATE_LIMIT_THRESHOLD:
            reset_at = response.headers.get("X-RateLimit-Reset", "unknown")
            logger.warning(
                "GitHub rate limit low: %s requests remaining (resets at %s)",
                remaining,
                reset_at,
            )

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, str | int] | None = None,
        timeout: float = 30.0,
    ) -> httpx.Response:
        """Execute a request with exponential backoff on 429 / 5xx responses."""
        headers = self._build_headers()
        last_exc: Exception | None = None

        for attempt in range(_MAX_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.request(
                        method, url, headers=headers, json=json, params=params
                    )
                self._check_rate_limit(response)

                if response.status_code == 429 or response.status_code >= 500:
                    retry_after = response.headers.get("Retry-After")
                    wait = (
                        float(retry_after)
                        if retry_after
                        else _BACKOFF_BASE_SECONDS ** (attempt + 1)
                    )
                    logger.warning(
                        "GitHub API %s %s returned %d, retrying in %.1fs (attempt %d/%d)",
                        method,
                        url,
                        response.status_code,
                        wait,
                        attempt + 1,
                        _MAX_RETRIES,
                    )
                    await asyncio.sleep(wait)
                    continue

                response.raise_for_status()
                return response

            except httpx.TimeoutException as exc:
                last_exc = exc
                wait = _BACKOFF_BASE_SECONDS ** (attempt + 1)
                logger.warning(
                    "GitHub API timeout on %s %s, retrying in %.1fs", method, url, wait
                )
                await asyncio.sleep(wait)

        if last_exc is not None:
            raise last_exc
        raise RuntimeError(
            f"GitHub API {method} {url} failed after {_MAX_RETRIES} attempts"
        )

    async def _paginate(
        self,
        url: str,
        *,
        params: dict[str, str | int] | None = None,
        timeout: float = 30.0,
    ) -> list[dict[str, Any]]:
        """Collect all pages of a list endpoint."""
        base_params: dict[str, str | int] = {"per_page": 100}
        if params:
            base_params.update(params)

        results: list[dict[str, Any]] = []
        page = 1
        while True:
            base_params["page"] = page
            response = await self._request(
                "GET", url, params=base_params, timeout=timeout
            )
            data: list[dict[str, Any]] = response.json()
            if not data:
                break
            results.extend(data)
            if len(data) < 100:
                break
            page += 1

        return results

    async def fetch_pr_metadata(self, repo: str, pr_number: int) -> PrMetadata:
        """Fetch PR title, description, author, files changed, and head SHA."""
        pr_url = f"{_GITHUB_API_BASE}/repos/{repo}/pulls/{pr_number}"
        files_url = f"{_GITHUB_API_BASE}/repos/{repo}/pulls/{pr_number}/files"

        response = await self._request("GET", pr_url)
        pr_data: dict[str, Any] = response.json()

        files_data = await self._paginate(files_url)
        files_changed = tuple(f["filename"] for f in files_data)

        return PrMetadata(
            number=pr_number,
            title=pr_data.get("title", ""),
            body=pr_data.get("body") or "",
            author=pr_data.get("user", {}).get("login", ""),
            head_sha=pr_data.get("head", {}).get("sha", ""),
            base_ref=pr_data.get("base", {}).get("ref", ""),
            head_ref=pr_data.get("head", {}).get("ref", ""),
            state=pr_data.get("state", ""),
            files_changed=files_changed,
        )

    async def fetch_review_threads(
        self, repo: str, pr_number: int
    ) -> list[ReviewThread]:
        """Fetch all review comment threads on a PR (paginated)."""
        url = f"{_GITHUB_API_BASE}/repos/{repo}/pulls/{pr_number}/comments"
        raw = await self._paginate(url)
        return [self._parse_review_thread(c) for c in raw]

    async def post_review_comment(
        self,
        repo: str,
        pr_number: int,
        commit_id: str,
        path: str,
        line: int,
        body: str,
    ) -> ReviewThread:
        """Post a new line-level review comment on the PR."""
        url = f"{_GITHUB_API_BASE}/repos/{repo}/pulls/{pr_number}/comments"
        payload: dict[str, Any] = {
            "body": body,
            "commit_id": commit_id,
            "path": path,
            "line": line,
            "side": "RIGHT",
        }
        response = await self._request("POST", url, json=payload)
        return self._parse_review_thread(response.json())

    async def post_pr_comment(self, repo: str, pr_number: int, body: str) -> int:
        """Post a general PR issue comment (not a review thread). Returns comment ID."""
        url = f"{_GITHUB_API_BASE}/repos/{repo}/issues/{pr_number}/comments"
        response = await self._request("POST", url, json={"body": body})
        data: dict[str, Any] = response.json()
        return int(data["id"])

    @staticmethod
    def _parse_review_thread(data: dict[str, Any]) -> ReviewThread:
        return ReviewThread(
            id=int(data["id"]),
            body=data.get("body") or "",
            path=data.get("path") or "",
            line=data.get("line"),
            commit_id=data.get("commit_id") or data.get("original_commit_id") or "",
            user_login=data.get("user", {}).get("login", ""),
            created_at=data.get("created_at") or "",
            updated_at=data.get("updated_at") or "",
            in_reply_to_id=data.get("in_reply_to_id"),
            resolved=bool(data.get("resolved", False)),
        )


__all__: list[str] = ["AdapterGithubReviewBridge", "PrMetadata", "ReviewThread"]
