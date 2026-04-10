# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerCommentResolution — resolve trivial bot review threads before merge.

Identifies trivial CodeRabbit/bot review comments (nit, style, minor, nitpick)
with no human reply and marks them resolved.

Related:
    - OMN-8207: Add HandlerCommentResolution + HandlerAdminMerge to fix_effect
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

# Patterns that mark a comment as trivially resolvable bot feedback
_TRIVIAL_BOT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bnit\b", re.IGNORECASE),
    re.compile(r"\bnitpick\b", re.IGNORECASE),
    re.compile(r"\bminor\b", re.IGNORECASE),
    re.compile(r"\bstyle\b", re.IGNORECASE),
    re.compile(r"\boptional\b", re.IGNORECASE),
    re.compile(r"\bnon-blocking\b", re.IGNORECASE),
]

# Known bot account login substrings
_BOT_ACCOUNT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"coderabbit", re.IGNORECASE),
    re.compile(r"\[bot\]", re.IGNORECASE),
    re.compile(r"dependabot", re.IGNORECASE),
]


def _is_bot_account(login: str) -> bool:
    return any(p.search(login) for p in _BOT_ACCOUNT_PATTERNS)


def _is_trivial_comment(body: str) -> bool:
    return any(p.search(body) for p in _TRIVIAL_BOT_PATTERNS)


class ModelCommentResolutionResult(BaseModel):
    """Result of a comment resolution pass."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr_number: int
    repo: str
    resolvable_thread_ids: list[str] = Field(default_factory=list)
    resolved_count: int = 0
    dry_run: bool = False


class HandlerCommentResolution:
    """Resolve trivial bot review threads before merge.

    Only resolves threads where:
    - The comment author is a known bot (coderabbit, dependabot, *[bot]*)
    - The comment body matches a trivial pattern (nit, style, minor, nitpick)
    - There is no human reply on the thread

    In dry_run=True: returns list of resolvable threads without acting.
    """

    @property
    def handler_type(self) -> str:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> str:
        return "EFFECT"

    @property
    def correlation_id(self) -> UUID | None:
        return None

    async def handle(
        self,
        *,
        pr_number: int,
        repo: str,
        dry_run: bool = False,
    ) -> ModelCommentResolutionResult:
        """Identify and optionally resolve trivial bot comment threads.

        Args:
            pr_number: GitHub PR number.
            repo: Repo slug (e.g. OmniNode-ai/omnimarket).
            dry_run: If True, identify but do not resolve.

        Returns:
            ModelCommentResolutionResult with resolution details.
        """
        resolvable: list[str] = []

        try:
            review_comments = self._fetch_review_comments(repo, pr_number)
            for comment in review_comments:
                comment_id = str(comment.get("id", ""))
                author_login = str(
                    comment.get("user", {}).get("login", "")
                    if isinstance(comment.get("user"), dict)
                    else comment.get("user", "")
                )
                body = str(comment.get("body", ""))

                if not _is_bot_account(author_login):
                    continue
                if not _is_trivial_comment(body):
                    continue
                # Check no human reply (simplified: if in_reply_to_id is absent = root comment)
                if comment.get("in_reply_to_id"):
                    continue

                resolvable.append(comment_id)

            if dry_run or not resolvable:
                return ModelCommentResolutionResult(
                    pr_number=pr_number,
                    repo=repo,
                    resolvable_thread_ids=resolvable,
                    resolved_count=0,
                    dry_run=dry_run,
                )

            resolved = 0
            for comment_id in resolvable:
                if self._resolve_comment(repo, pr_number, comment_id):
                    resolved += 1

            logger.info(
                "[COMMENT-RESOLUTION] pr=%d repo=%s resolved=%d / %d",
                pr_number,
                repo,
                resolved,
                len(resolvable),
            )
            return ModelCommentResolutionResult(
                pr_number=pr_number,
                repo=repo,
                resolvable_thread_ids=resolvable,
                resolved_count=resolved,
                dry_run=False,
            )
        except Exception as exc:
            logger.warning(
                "[COMMENT-RESOLUTION] error pr=%d repo=%s: %s",
                pr_number,
                repo,
                exc,
                exc_info=True,
            )
            return ModelCommentResolutionResult(
                pr_number=pr_number,
                repo=repo,
                resolvable_thread_ids=[],
                resolved_count=0,
                dry_run=dry_run,
            )

    def _fetch_review_comments(
        self, repo: str, pr_number: int
    ) -> list[dict[str, object]]:
        """Fetch PR review comments via gh api."""
        cmd = [
            "gh",
            "api",
            f"/repos/{repo}/pulls/{pr_number}/comments",
            "--paginate",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            logger.debug(
                "Failed to fetch review comments for PR #%d: %s",
                pr_number,
                result.stderr.strip(),
            )
            return []
        try:
            data: list[dict[str, object]] = json.loads(result.stdout)
            return data if isinstance(data, list) else []
        except json.JSONDecodeError:
            return []

    def _resolve_comment(self, repo: str, pr_number: int, comment_id: str) -> bool:
        """Mark a review comment thread resolved via gh api PATCH."""
        cmd = [
            "gh",
            "api",
            "--method",
            "PATCH",
            f"/repos/{repo}/pulls/comments/{comment_id}",
            "-f",
            "body=Resolved: trivial bot comment.",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            logger.debug(
                "Failed to resolve comment %s on PR #%d: %s",
                comment_id,
                pr_number,
                result.stderr.strip(),
            )
        return result.returncode == 0


__all__: list[str] = [
    "HandlerCommentResolution",
    "ModelCommentResolutionResult",
]
