# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerAutoRebase — auto-rebase stale PR branches via gh pr update-branch.

Handles Track A-update (stale) PRs whose branches are behind base.

Related:
    - OMN-8204: Add HandlerAutoRebase to node_pr_lifecycle_fix_effect
"""

from __future__ import annotations

import logging
import subprocess
from uuid import UUID

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


class ModelRebaseResult(BaseModel):
    """Result of an auto-rebase operation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr_number: int
    repo: str
    success: bool
    error_message: str | None = None
    rebase_sha: str | None = None


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class HandlerAutoRebase:
    """Auto-rebase stale PR branches via `gh pr update-branch`.

    In dry_run=True: logs intent, returns ModelRebaseResult(success=True)
    without calling gh.
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
    ) -> ModelRebaseResult:
        """Rebase a stale PR branch.

        Args:
            pr_number: GitHub PR number.
            repo: Repo slug (e.g. OmniNode-ai/omnimarket).
            dry_run: If True, log intent only — no gh call made.

        Returns:
            ModelRebaseResult with success status.
        """
        if dry_run:
            logger.info(
                "[AUTO-REBASE] dry_run=True: would rebase pr=%d repo=%s",
                pr_number,
                repo,
            )
            return ModelRebaseResult(pr_number=pr_number, repo=repo, success=True)

        logger.info("[AUTO-REBASE] rebasing pr=%d repo=%s", pr_number, repo)
        try:
            result = subprocess.run(
                ["gh", "pr", "update-branch", str(pr_number), "--repo", repo],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                error_msg = result.stderr.strip() or result.stdout.strip()
                logger.warning(
                    "[AUTO-REBASE] failed pr=%d repo=%s error=%s",
                    pr_number,
                    repo,
                    error_msg,
                )
                return ModelRebaseResult(
                    pr_number=pr_number,
                    repo=repo,
                    success=False,
                    error_message=error_msg,
                )

            # Try to capture the new HEAD SHA for observability
            rebase_sha: str | None = None
            sha_result = subprocess.run(
                [
                    "gh",
                    "pr",
                    "view",
                    str(pr_number),
                    "--repo",
                    repo,
                    "--json",
                    "headRefOid",
                    "--jq",
                    ".headRefOid",
                ],
                capture_output=True,
                text=True,
            )
            if sha_result.returncode == 0:
                rebase_sha = sha_result.stdout.strip() or None

            logger.info(
                "[AUTO-REBASE] success pr=%d repo=%s sha=%s",
                pr_number,
                repo,
                rebase_sha,
            )
            return ModelRebaseResult(
                pr_number=pr_number,
                repo=repo,
                success=True,
                rebase_sha=rebase_sha,
            )
        except Exception as exc:
            logger.warning(
                "[AUTO-REBASE] exception pr=%d repo=%s: %s",
                pr_number,
                repo,
                exc,
                exc_info=True,
            )
            return ModelRebaseResult(
                pr_number=pr_number,
                repo=repo,
                success=False,
                error_message=str(exc),
            )


__all__: list[str] = ["HandlerAutoRebase", "ModelRebaseResult"]
