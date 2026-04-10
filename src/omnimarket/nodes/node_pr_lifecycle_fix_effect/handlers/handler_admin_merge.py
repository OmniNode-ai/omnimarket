# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerAdminMerge — admin merge fallback for stuck merge queue PRs.

Consumes ModelStuckQueueEntry list from InventoryResult.stuck_queue_prs.
Only fires when enable_admin_merge_fallback=True (default: False — opt-in per
adversarial R5).

Emits explicit log line "ADMIN MERGE TRIGGERED pr={pr_number} repo={repo}"
before acting.

Related:
    - OMN-8207: Task 10 — Add HandlerCommentResolution + HandlerAdminMerge
    - OMN-8206: Task 9 — Stuck merge queue detection (produces stuck_queue_prs)
"""

from __future__ import annotations

import logging
import subprocess
from typing import Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_pr_lifecycle_inventory_compute.models.model_pr_lifecycle_inventory import (
    ModelStuckQueueEntry,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


class ModelAdminMergeResult(BaseModel):
    """Result of an admin merge fallback pass."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prs_merged: int = 0
    prs_skipped: int = 0
    prs_failed: int = 0
    dry_run: bool = False


# ---------------------------------------------------------------------------
# Adapter protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ProtocolAdminMergeAdapter(Protocol):
    """Minimal GitHub merge operations for admin merge fallback."""

    async def admin_merge(self, repo: str, pr_number: int) -> None:
        """Admin-merge a PR via gh pr merge --admin --squash."""
        ...


# ---------------------------------------------------------------------------
# Default live adapter
# ---------------------------------------------------------------------------


class _LiveAdminMergeAdapter:
    async def admin_merge(self, repo: str, pr_number: int) -> None:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "merge",
                str(pr_number),
                "--admin",
                "--squash",
                "--repo",
                repo,
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            msg = (
                f"gh pr merge --admin failed for {repo}#{pr_number} "
                f"(exit {result.returncode}): {result.stderr.strip()}"
            )
            raise RuntimeError(msg)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class HandlerAdminMerge:
    """Admin merge fallback for PRs stuck in merge queue >30min.

    Only fires when enable_admin_merge_fallback=True. Logs an explicit
    "ADMIN MERGE TRIGGERED" line before each merge action for audit trails.
    """

    def __init__(self, adapter: ProtocolAdminMergeAdapter | None = None) -> None:
        self._adapter: ProtocolAdminMergeAdapter = adapter or _LiveAdminMergeAdapter()

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
        stuck_prs: list[ModelStuckQueueEntry],
        enable_admin_merge_fallback: bool = False,
        dry_run: bool = False,
    ) -> ModelAdminMergeResult:
        """Admin-merge all stuck PRs if opt-in flag is set.

        Args:
            stuck_prs: PRs identified as stuck by inventory compute.
            enable_admin_merge_fallback: Must be True to actually merge.
            dry_run: When True, log intent without merging.

        Returns:
            ModelAdminMergeResult with merge counts.
        """
        if not enable_admin_merge_fallback:
            logger.info(
                "admin-merge: skipped (enable_admin_merge_fallback=False), "
                "stuck_prs=%d",
                len(stuck_prs),
            )
            return ModelAdminMergeResult(prs_skipped=len(stuck_prs), dry_run=dry_run)

        prs_merged = 0
        prs_skipped = 0
        prs_failed = 0

        for pr in stuck_prs:
            logger.warning(
                "ADMIN MERGE TRIGGERED pr=%s repo=%s queue_age_minutes=%.1f dry_run=%s",
                pr.pr_number,
                pr.repo,
                pr.queue_age_minutes,
                dry_run,
            )
            if dry_run:
                prs_merged += 1
                continue
            try:
                await self._adapter.admin_merge(repo=pr.repo, pr_number=pr.pr_number)
                prs_merged += 1
                logger.info(
                    "admin-merge succeeded: pr=%s repo=%s", pr.pr_number, pr.repo
                )
            except Exception as exc:
                prs_failed += 1
                logger.warning(
                    "admin-merge failed: pr=%s repo=%s error=%s",
                    pr.pr_number,
                    pr.repo,
                    exc,
                    exc_info=True,
                )

        return ModelAdminMergeResult(
            prs_merged=prs_merged,
            prs_skipped=prs_skipped,
            prs_failed=prs_failed,
            dry_run=dry_run,
        )


__all__: list[str] = [
    "HandlerAdminMerge",
    "ModelAdminMergeResult",
    "ProtocolAdminMergeAdapter",
]
