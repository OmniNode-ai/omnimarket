# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerAdminMerge — admin merge fallback for stuck merge queue PRs.

Consumes ModelStuckQueueEntry list from InventoryResult.stuck_queue_prs.
Fires when enable_admin_merge_fallback=True (default ON; pass
`--no-admin-merge-fallback` to disable).

Emits explicit log line "ADMIN MERGE TRIGGERED pr={pr_number} repo={repo}"
before acting.

Related:
    - OMN-8207: Task 10 — Add HandlerCommentResolution + HandlerAdminMerge
    - OMN-8206: Task 9 — Stuck merge queue detection (produces stuck_queue_prs)
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from typing import Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_pr_lifecycle_inventory_compute.models.model_pr_lifecycle_inventory import (
    ModelStuckQueueEntry,
)

logger = logging.getLogger(__name__)

_DEFAULT_ADMIN_MERGE_TIMEOUT_SECONDS: float = 60.0


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

    async def admin_merge(
        self,
        repo: str,
        pr_number: int,
        timeout_seconds: float = _DEFAULT_ADMIN_MERGE_TIMEOUT_SECONDS,
    ) -> None:
        """Admin-merge a PR via gh pr merge --admin --squash.

        Must enforce a hard timeout and raise asyncio.TimeoutError when the
        underlying subprocess exceeds timeout_seconds.
        """
        ...


# ---------------------------------------------------------------------------
# Default live adapter
# ---------------------------------------------------------------------------


def _run_gh_admin_merge(repo: str, pr_number: int) -> subprocess.CompletedProcess[str]:
    """Synchronous subprocess call — runs on a worker thread via asyncio.to_thread.

    Kept as a module-level function so tests can monkeypatch it cleanly.
    """
    return subprocess.run(
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


class _LiveAdminMergeAdapter:
    async def admin_merge(
        self,
        repo: str,
        pr_number: int,
        timeout_seconds: float = _DEFAULT_ADMIN_MERGE_TIMEOUT_SECONDS,
    ) -> None:
        # Run the blocking gh CLI call on a thread; bound it with a hard timeout
        # so a hung gh process never blocks the asyncio event loop.
        result = await asyncio.wait_for(
            asyncio.to_thread(_run_gh_admin_merge, repo, pr_number),
            timeout=timeout_seconds,
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

    Fires when enable_admin_merge_fallback=True (default ON; pass
    `--no-admin-merge-fallback` to disable). Logs an explicit
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
        enable_admin_merge_fallback: bool = True,
        dry_run: bool = False,
        timeout_seconds: float = _DEFAULT_ADMIN_MERGE_TIMEOUT_SECONDS,
    ) -> ModelAdminMergeResult:
        """Admin-merge all stuck PRs unless explicitly disabled.

        Args:
            stuck_prs: PRs identified as stuck by inventory compute.
            enable_admin_merge_fallback: Default ON; set False to disable.
            dry_run: When True, log intent without merging.
            timeout_seconds: Hard timeout per subprocess invocation. Prevents a
                hung gh process from stalling the sweep.

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
                await self._adapter.admin_merge(
                    repo=pr.repo,
                    pr_number=pr.pr_number,
                    timeout_seconds=timeout_seconds,
                )
                prs_merged += 1
                logger.info(
                    "admin-merge succeeded: pr=%s repo=%s", pr.pr_number, pr.repo
                )
            except TimeoutError:
                prs_failed += 1
                logger.warning(
                    "admin-merge timed out after %.1fs: pr=%s repo=%s",
                    timeout_seconds,
                    pr.pr_number,
                    pr.repo,
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
