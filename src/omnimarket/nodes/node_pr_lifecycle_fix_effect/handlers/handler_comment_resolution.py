# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerCommentResolution — auto-resolve trivial bot review threads.

Resolves trivial CodeRabbit/bot review threads before merge attempt.
"Trivial" = comment body matches known bot patterns (nit, style, minor, nitpick)
AND has no human reply.

In dry_run=True: returns list of resolvable threads without acting.

Related:
    - OMN-8207: Task 10 — Add HandlerCommentResolution + HandlerAdminMerge
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from omnimarket.github_api import GitHubApiError, graphql, split_repo

logger = logging.getLogger(__name__)

# Patterns that identify trivial bot comments
_TRIVIAL_BOT_PATTERNS = re.compile(
    r"\b(nit|nitpick|nit-pick|style|minor|trivial|suggestion)\b",
    re.IGNORECASE,
)

# Bot login names to detect
_BOT_LOGINS = frozenset(
    {
        "coderabbitai",
        "coderabbitai[bot]",
        "github-actions[bot]",
        "dependabot[bot]",
    }
)

_REVIEW_THREADS_QUERY = """
query($owner: String!, $repo: String!, $prNumber: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $prNumber) {
      reviewThreads(first: 100) {
        nodes {
          id
          isResolved
          comments(first: 20) {
            nodes {
              body
              author {
                login
              }
            }
          }
        }
      }
    }
  }
}
"""

_RESOLVE_THREAD_MUTATION = """
mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread {
      id
      isResolved
    }
  }
}
"""


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


class ModelCommentResolutionResult(BaseModel):
    """Result of a comment resolution pass on a PR."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr_number: int
    repo: str
    resolved_count: int = 0
    preserved_count: int = 0
    dry_run: bool = False


# ---------------------------------------------------------------------------
# Adapter protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ProtocolCommentAdapter(Protocol):
    """Minimal GitHub comment/review operations for comment resolution."""

    async def list_review_comments(
        self, repo: str, pr_number: int
    ) -> list[dict[str, object]]:
        """Return unresolved review-thread summaries from the GitHub API."""
        ...

    async def resolve_thread(
        self, repo: str, pr_number: int, thread_id: str | int
    ) -> None:
        """Mark a review thread resolved."""
        ...


# ---------------------------------------------------------------------------
# Default live adapter (GitHub GraphQL)
# ---------------------------------------------------------------------------


class _LiveCommentAdapter:
    async def list_review_comments(
        self, repo: str, pr_number: int
    ) -> list[dict[str, object]]:
        return await asyncio.to_thread(self._list_review_comments_sync, repo, pr_number)

    def _list_review_comments_sync(
        self, repo: str, pr_number: int
    ) -> list[dict[str, object]]:
        owner, repo_name = split_repo(repo)
        data = graphql(
            _REVIEW_THREADS_QUERY,
            {"owner": owner, "repo": repo_name, "prNumber": pr_number},
        )
        thread_nodes = (
            (
                (((data.get("repository") or {}).get("pullRequest")) or {}).get(
                    "reviewThreads"
                )
            )
            or {}
        ).get("nodes", [])
        results: list[dict[str, object]] = []
        for thread in thread_nodes:
            if not isinstance(thread, dict) or thread.get("isResolved"):
                continue
            comments = ((thread.get("comments") or {}).get("nodes")) or []
            if not comments:
                continue
            first = comments[0] if isinstance(comments[0], dict) else {}
            author = first.get("author") or {}
            login = str(author.get("login", "")) if isinstance(author, dict) else ""
            body = str(first.get("body", ""))
            has_human_reply = any(
                isinstance(comment, dict)
                and isinstance(comment.get("author"), dict)
                and str((comment.get("author") or {}).get("login", ""))
                not in _BOT_LOGINS
                and not str((comment.get("author") or {}).get("login", "")).endswith(
                    "[bot]"
                )
                for comment in comments[1:]
            )
            thread_id = thread.get("id")
            if isinstance(thread_id, str) and thread_id:
                results.append(
                    {
                        "id": thread_id,
                        "user": {"login": login},
                        "body": body,
                        "has_human_reply": has_human_reply,
                    }
                )
        return results

    async def resolve_thread(
        self, repo: str, pr_number: int, thread_id: str | int
    ) -> None:
        del repo, pr_number
        await asyncio.to_thread(self._resolve_thread_sync, thread_id)

    def _resolve_thread_sync(self, thread_id: str | int) -> None:
        try:
            graphql(_RESOLVE_THREAD_MUTATION, {"threadId": str(thread_id)})
        except GitHubApiError as exc:
            raise RuntimeError(
                f"resolveReviewThread failed for {thread_id}: {exc}"
            ) from exc


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class HandlerCommentResolution:
    """Resolves trivial bot review threads before merge attempt.

    "Trivial" = comment matches bot pattern AND has no human reply.
    In dry_run mode returns counts without making API calls.
    """

    def __init__(self, adapter: ProtocolCommentAdapter | None = None) -> None:
        self._adapter: ProtocolCommentAdapter = adapter or _LiveCommentAdapter()

    @property
    def handler_type(self) -> str:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> str:
        return "EFFECT"

    @property
    def correlation_id(self) -> UUID | None:
        return None

    def _is_trivial_bot_comment(self, comment: dict[str, object]) -> bool:
        """Return True if comment is a trivial bot comment with no human reply."""
        user = comment.get("user") or {}
        login = str(user.get("login", "")) if isinstance(user, dict) else ""
        body = str(comment.get("body", ""))
        is_bot = login in _BOT_LOGINS or login.endswith("[bot]")
        is_trivial = bool(_TRIVIAL_BOT_PATTERNS.search(body))
        has_human_reply = bool(comment.get("has_human_reply"))
        return is_bot and is_trivial and not has_human_reply

    def _is_human_comment(self, comment: dict[str, object]) -> bool:
        """Return True if comment is from a human (not a bot)."""
        user = comment.get("user") or {}
        login = str(user.get("login", "")) if isinstance(user, dict) else ""
        return login not in _BOT_LOGINS and not login.endswith("[bot]")

    async def handle(
        self, *, pr_number: int, repo: str, dry_run: bool = False
    ) -> ModelCommentResolutionResult:
        """Resolve trivial bot comments on a PR.

        Args:
            pr_number: GitHub PR number.
            repo: Repo slug (owner/repo).
            dry_run: When True, return counts without resolving.

        Returns:
            ModelCommentResolutionResult with resolved/preserved counts.
        """
        logger.info(
            "comment-resolution: pr=%s repo=%s dry_run=%s", pr_number, repo, dry_run
        )

        comments = await self._adapter.list_review_comments(
            repo=repo, pr_number=pr_number
        )

        resolved_count = 0
        preserved_count = 0

        for comment in comments:
            thread_id = comment.get("id")
            if not isinstance(thread_id, (str, int)):
                continue
            if self._is_trivial_bot_comment(comment):
                if not dry_run:
                    await self._adapter.resolve_thread(repo, pr_number, thread_id)
                resolved_count += 1
            elif self._is_human_comment(comment):
                preserved_count += 1

        logger.info(
            "comment-resolution complete: pr=%s repo=%s resolved=%d preserved=%d dry_run=%s",
            pr_number,
            repo,
            resolved_count,
            preserved_count,
            dry_run,
        )

        return ModelCommentResolutionResult(
            pr_number=pr_number,
            repo=repo,
            resolved_count=resolved_count,
            preserved_count=preserved_count,
            dry_run=dry_run,
        )


__all__: list[str] = [
    "HandlerCommentResolution",
    "ModelCommentResolutionResult",
    "ProtocolCommentAdapter",
]
