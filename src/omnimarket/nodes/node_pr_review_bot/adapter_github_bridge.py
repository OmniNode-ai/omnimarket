"""GitHub Bridge Adapter — abstracts all GitHub API operations for node_pr_review_bot.

Provides async httpx-based methods for:
- Fetching PR metadata (title, description, author, files changed)
- Fetching PR review status and existing review threads
- Posting review comments on specific lines
- Creating / updating review threads
- Checking for existing bot threads (dedup guard per design doc R10)
- Rate-limit-aware request execution with exponential backoff (design doc R8)

All methods read the GitHub token from the environment (``GITHUB_TOKEN``).
No tokens are hardcoded.
"""

from __future__ import annotations

import asyncio
import logging
import os
from abc import ABC, abstractmethod
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_GITHUB_API_BASE = "https://api.github.com"
_RATE_LIMIT_THRESHOLD = 50  # back off when remaining < this
_MAX_RETRIES = 3
_BACKOFF_BASE_SECONDS = 2.0


# ---------------------------------------------------------------------------
# Data shapes returned by the bridge
# ---------------------------------------------------------------------------


class PrMetadata:
    """Minimal PR metadata needed by the review bot."""

    __slots__ = (
        "author",
        "base_ref",
        "body",
        "files_changed",
        "head_ref",
        "head_sha",
        "number",
        "state",
        "title",
    )

    def __init__(
        self,
        *,
        number: int,
        title: str,
        body: str,
        author: str,
        head_sha: str,
        base_ref: str,
        head_ref: str,
        state: str,
        files_changed: list[str],
    ) -> None:
        self.number = number
        self.title = title
        self.body = body
        self.author = author
        self.head_sha = head_sha
        self.base_ref = base_ref
        self.head_ref = head_ref
        self.state = state
        self.files_changed = files_changed


class ReviewThread:
    """A single GitHub pull request review comment (thread)."""

    __slots__ = (
        "body",
        "commit_id",
        "created_at",
        "id",
        "in_reply_to_id",
        "line",
        "path",
        "resolved",
        "updated_at",
        "user_login",
    )

    def __init__(
        self,
        *,
        id: int,
        body: str,
        path: str,
        line: int | None,
        commit_id: str,
        user_login: str,
        created_at: str,
        updated_at: str,
        in_reply_to_id: int | None,
        resolved: bool,
    ) -> None:
        self.id = id
        self.body = body
        self.path = path
        self.line = line
        self.commit_id = commit_id
        self.user_login = user_login
        self.created_at = created_at
        self.updated_at = updated_at
        self.in_reply_to_id = in_reply_to_id
        self.resolved = resolved


class PrReviewStatus:
    """Aggregated review status for a PR."""

    __slots__ = ("reviews", "state", "unresolved_thread_count")

    def __init__(
        self,
        *,
        state: str,
        reviews: list[dict[str, Any]],
        unresolved_thread_count: int,
    ) -> None:
        self.state = state
        self.reviews = reviews
        self.unresolved_thread_count = unresolved_thread_count


# ---------------------------------------------------------------------------
# Protocol (ABC)
# ---------------------------------------------------------------------------


class GitHubBridgeProtocol(ABC):
    """Protocol for all GitHub API operations needed by node_pr_review_bot."""

    @abstractmethod
    async def fetch_pr_metadata(self, repo: str, pr_number: int) -> PrMetadata:
        """Return title, description, author, files changed, and head SHA."""
        ...

    @abstractmethod
    async def fetch_review_threads(
        self, repo: str, pr_number: int
    ) -> list[ReviewThread]:
        """Return all review comment threads on a PR."""
        ...

    @abstractmethod
    async def fetch_review_status(self, repo: str, pr_number: int) -> PrReviewStatus:
        """Return review state and unresolved thread count."""
        ...

    @abstractmethod
    async def post_review_comment(
        self,
        repo: str,
        pr_number: int,
        commit_id: str,
        path: str,
        line: int,
        body: str,
    ) -> ReviewThread:
        """Post a new review comment on a specific file line. Returns the created thread."""
        ...

    @abstractmethod
    async def post_pr_comment(
        self,
        repo: str,
        pr_number: int,
        body: str,
    ) -> int:
        """Post a general (non-review) PR comment. Returns the comment ID."""
        ...

    @abstractmethod
    async def reply_to_review_comment(
        self,
        repo: str,
        pr_number: int,
        in_reply_to_id: int,
        body: str,
    ) -> ReviewThread:
        """Reply to an existing review thread."""
        ...

    @abstractmethod
    async def find_bot_thread_for_finding(
        self,
        repo: str,
        pr_number: int,
        finding_id: str,
        bot_login: str,
    ) -> ReviewThread | None:
        """Return existing bot thread for a finding ID, or None if not posted yet (R10 dedup)."""
        ...

    @abstractmethod
    async def fetch_thread_replies(
        self, repo: str, pr_number: int, thread_id: int
    ) -> list[ReviewThread]:
        """Return all replies to a specific review thread."""
        ...


# ---------------------------------------------------------------------------
# Concrete implementation
# ---------------------------------------------------------------------------


class AdapterGitHubBridge(GitHubBridgeProtocol):
    """Async httpx-based GitHub API adapter.

    Reads ``GITHUB_TOKEN`` from the environment. Never accepts a token as a
    constructor argument to prevent accidental hardcoding at call sites.

    Implements exponential backoff when the rate-limit ``X-RateLimit-Remaining``
    header drops below the threshold (design doc R8).
    """

    def __init__(self, *, token_env_var: str = "GITHUB_TOKEN") -> None:
        self._token_env_var = token_env_var

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_token(self) -> str:
        token = os.environ.get(self._token_env_var, "")
        if not token:
            msg = (
                f"GitHub token not found in environment variable {self._token_env_var!r}. "
                "Set GITHUB_TOKEN before running the PR review bot."
            )
            raise RuntimeError(msg)
        return token

    def _build_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _check_rate_limit(self, response: httpx.Response) -> None:
        """Log a warning if rate-limit headroom is low (R8)."""
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
        """Execute a request with exponential backoff on 429 / 5xx responses (R8)."""
        headers = self._build_headers()
        last_exc: Exception | None = None

        for attempt in range(_MAX_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.request(
                        method,
                        url,
                        headers=headers,
                        json=json,
                        params=params,
                    )
                self._check_rate_limit(response)

                if response.status_code == 429 or response.status_code >= 500:
                    retry_after = response.headers.get("Retry-After")
                    if retry_after:
                        wait = float(retry_after)
                    else:
                        wait = _BACKOFF_BASE_SECONDS ** (attempt + 1)
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
                    "GitHub API timeout on %s %s, retrying in %.1fs",
                    method,
                    url,
                    wait,
                )
                await asyncio.sleep(wait)

        if last_exc is not None:
            raise last_exc
        msg = f"GitHub API {method} {url} failed after {_MAX_RETRIES} attempts"
        raise RuntimeError(msg)

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

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def fetch_pr_metadata(self, repo: str, pr_number: int) -> PrMetadata:
        """Fetch PR title, description, author, files changed, and head SHA."""
        pr_url = f"{_GITHUB_API_BASE}/repos/{repo}/pulls/{pr_number}"
        files_url = f"{_GITHUB_API_BASE}/repos/{repo}/pulls/{pr_number}/files"

        response = await self._request("GET", pr_url)
        pr_data: dict[str, Any] = response.json()

        files_data = await self._paginate(files_url)
        files_changed = [f["filename"] for f in files_data]

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

    async def fetch_review_status(self, repo: str, pr_number: int) -> PrReviewStatus:
        """Fetch reviews and count unresolved threads."""
        reviews_url = f"{_GITHUB_API_BASE}/repos/{repo}/pulls/{pr_number}/reviews"
        threads_url = f"{_GITHUB_API_BASE}/repos/{repo}/pulls/{pr_number}/comments"

        reviews_raw = await self._paginate(reviews_url)
        threads_raw = await self._paginate(threads_url)

        # GitHub REST does not expose a "resolved" field on review comments.
        # Top-level comments (in_reply_to_id is null) represent thread roots;
        # replies are follow-ups on an existing thread. We count root comments
        # as a proxy for thread count. HandlerThreadWatcher tracks actual
        # resolution by polling GraphQL or detecting bot-reply markers.
        unresolved = sum(1 for t in threads_raw if t.get("in_reply_to_id") is None)

        latest_states: dict[str, str] = {}
        for review in reviews_raw:
            login = review.get("user", {}).get("login", "")
            state = review.get("state", "")
            if login:
                latest_states[login] = state

        # Aggregate: CHANGES_REQUESTED > APPROVED > COMMENTED
        agg_state = "COMMENTED"
        for state in latest_states.values():
            if state == "CHANGES_REQUESTED":
                agg_state = "CHANGES_REQUESTED"
                break
            if state == "APPROVED":
                agg_state = "APPROVED"

        return PrReviewStatus(
            state=agg_state,
            reviews=reviews_raw,
            unresolved_thread_count=unresolved,
        )

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

    async def post_pr_comment(
        self,
        repo: str,
        pr_number: int,
        body: str,
    ) -> int:
        """Post a general PR issue comment (not a review thread). Returns comment ID."""
        url = f"{_GITHUB_API_BASE}/repos/{repo}/issues/{pr_number}/comments"
        response = await self._request("POST", url, json={"body": body})
        data: dict[str, Any] = response.json()
        return int(data["id"])

    async def reply_to_review_comment(
        self,
        repo: str,
        pr_number: int,
        in_reply_to_id: int,
        body: str,
    ) -> ReviewThread:
        """Reply to an existing review thread."""
        url = f"{_GITHUB_API_BASE}/repos/{repo}/pulls/{pr_number}/comments"
        payload: dict[str, Any] = {
            "body": body,
            "in_reply_to": in_reply_to_id,
        }
        response = await self._request("POST", url, json=payload)
        return self._parse_review_thread(response.json())

    async def find_bot_thread_for_finding(
        self,
        repo: str,
        pr_number: int,
        finding_id: str,
        bot_login: str,
    ) -> ReviewThread | None:
        """Return existing bot thread for a finding, or None (R10 dedup).

        Bot threads embed the finding ID in the body as a unique marker:
        ``<!-- omnibot:finding:{finding_id} -->``
        """
        marker = f"<!-- omnibot:finding:{finding_id} -->"
        threads = await self.fetch_review_threads(repo, pr_number)
        for thread in threads:
            if thread.user_login == bot_login and marker in thread.body:
                return thread
        return None

    async def fetch_thread_replies(
        self, repo: str, pr_number: int, thread_id: int
    ) -> list[ReviewThread]:
        """Return all comments in the same review thread (including the root)."""
        all_threads = await self.fetch_review_threads(repo, pr_number)
        # Root comment or any reply to the same thread
        return [
            t for t in all_threads if t.id == thread_id or t.in_reply_to_id == thread_id
        ]

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

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


__all__: list[str] = [
    "AdapterGitHubBridge",
    "GitHubBridgeProtocol",
    "PrMetadata",
    "PrReviewStatus",
    "ReviewThread",
]
