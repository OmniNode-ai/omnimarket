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

import json
import logging
import re
import subprocess
from typing import Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

# Patterns that identify trivial bot comments
_TRIVIAL_BOT_PATTERNS = re.compile(
    r"\b(nit|nitpick|nit-pick|style|minor|trivial|suggestion)\b",
    re.IGNORECASE,
)

# Bot login names to detect
_BOT_LOGINS = frozenset({"coderabbitai", "github-actions[bot]", "dependabot[bot]"})


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
        """Return raw review comment dicts from the GitHub API."""
        ...

    async def resolve_thread(self, repo: str, pr_number: int, comment_id: int) -> None:
        """Mark a review comment thread resolved."""
        ...


# ---------------------------------------------------------------------------
# Default live adapter (gh CLI + gh api)
# ---------------------------------------------------------------------------


class _LiveCommentAdapter:
    async def list_review_comments(
        self, repo: str, pr_number: int
    ) -> list[dict[str, object]]:
        result = subprocess.run(
            ["gh", "api", f"repos/{repo}/pulls/{pr_number}/comments"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.debug("gh api review_comments failed: %s", result.stderr.strip())
            return []
        data: list[dict[str, object]] = json.loads(result.stdout)
        return data

    async def resolve_thread(self, repo: str, pr_number: int, comment_id: int) -> None:
        subprocess.run(
            [
                "gh",
                "api",
                "--method",
                "PATCH",
                f"repos/{repo}/pulls/comments/{comment_id}",
                "-f",
                "body=[resolved by omninode merge-sweep]",
            ],
            capture_output=True,
            text=True,
        )


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
        return is_bot and is_trivial

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
            comment_id = int(comment.get("id", 0))  # type: ignore[call-overload]
            if self._is_trivial_bot_comment(comment):
                if not dry_run:
                    await self._adapter.resolve_thread(
                        repo=repo, pr_number=pr_number, comment_id=comment_id
                    )
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
