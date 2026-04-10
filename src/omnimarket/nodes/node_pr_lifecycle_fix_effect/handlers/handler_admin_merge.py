# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerAdminMerge — admin merge fallback for PRs stuck in merge queue.

Consumes ModelStuckQueueEntry list from InventoryResult.stuck_queue_prs.
Only fires when enable_admin_merge_fallback=True (default: False — opt-in).

IMPORTANT: This handler emits an explicit log line before every admin merge:
  "ADMIN MERGE TRIGGERED pr={pr_number} repo={repo}"

Related:
    - OMN-8207: Add HandlerCommentResolution + HandlerAdminMerge to fix_effect
    - OMN-8206: Stuck merge queue detection (produces stuck_queue_prs)
"""

from __future__ import annotations

import logging
import subprocess
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

_ADMIN_MERGE_LOG_PREFIX = "ADMIN MERGE TRIGGERED"


class ModelAdminMergeEntry(BaseModel):
    """A single stuck PR eligible for admin merge fallback."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr_number: int
    repo: str
    queue_age_minutes: float


class ModelAdminMergeResult(BaseModel):
    """Result of an admin merge fallback run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prs_attempted: int = Field(default=0, ge=0)
    prs_merged: int = Field(default=0, ge=0)
    prs_failed: int = Field(default=0, ge=0)
    dry_run: bool = False
    skipped_reason: str | None = None


class HandlerAdminMerge:
    """Admin merge fallback for PRs stuck in merge queue.

    Safety contract:
    - NEVER fires unless enable_admin_merge_fallback=True is explicitly passed.
    - Logs "ADMIN MERGE TRIGGERED pr=N repo=R" before every merge action.
    - In dry_run=True: logs intent, does not call gh.
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
        stuck_prs: list[ModelAdminMergeEntry],
        enable_admin_merge_fallback: bool = False,
        admin_fallback_threshold_minutes: int = 30,
        dry_run: bool = False,
    ) -> ModelAdminMergeResult:
        """Execute admin merge fallback for stuck PRs.

        Args:
            stuck_prs: PRs to attempt admin merge on.
            enable_admin_merge_fallback: Must be True for any merges to occur.
            admin_fallback_threshold_minutes: Only merge PRs older than this.
            dry_run: If True, log intent without calling gh.

        Returns:
            ModelAdminMergeResult with counts.
        """
        if not enable_admin_merge_fallback:
            logger.info(
                "[ADMIN-MERGE] skipped: enable_admin_merge_fallback=False (opt-in required)"
            )
            return ModelAdminMergeResult(
                skipped_reason="enable_admin_merge_fallback=False"
            )

        eligible = [
            pr
            for pr in stuck_prs
            if pr.queue_age_minutes >= admin_fallback_threshold_minutes
        ]

        if not eligible:
            logger.info("[ADMIN-MERGE] no eligible stuck PRs above threshold")
            return ModelAdminMergeResult(skipped_reason="no_eligible_prs")

        merged = 0
        failed = 0

        for entry in eligible:
            # Mandatory pre-action log line — do not remove
            logger.warning(
                "%s pr=%d repo=%s queue_age_minutes=%.1f dry_run=%s",
                _ADMIN_MERGE_LOG_PREFIX,
                entry.pr_number,
                entry.repo,
                entry.queue_age_minutes,
                dry_run,
            )

            if dry_run:
                merged += 1
                continue

            if self._gh_admin_merge(entry.pr_number, entry.repo):
                merged += 1
            else:
                failed += 1

        return ModelAdminMergeResult(
            prs_attempted=len(eligible),
            prs_merged=merged,
            prs_failed=failed,
            dry_run=dry_run,
        )

    def _gh_admin_merge(self, pr_number: int, repo: str) -> bool:
        """Execute gh pr merge --admin --squash."""
        cmd = [
            "gh",
            "pr",
            "merge",
            str(pr_number),
            "--admin",
            "--squash",
            "--repo",
            repo,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            logger.error(
                "[ADMIN-MERGE] gh merge failed pr=%d repo=%s: %s",
                pr_number,
                repo,
                result.stderr.strip(),
            )
        return result.returncode == 0


__all__: list[str] = [
    "HandlerAdminMerge",
    "ModelAdminMergeEntry",
    "ModelAdminMergeResult",
]
