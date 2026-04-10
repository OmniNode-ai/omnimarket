# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerAutoRebase — auto-rebase stale PR branches via `gh pr update-branch`.

Targets Track A-update PRs (merge_state_status=BEHIND or UNKNOWN).
Protocol-injected adapter allows mock substitution in tests with zero infra.

Related:
    - OMN-8204: Task 7 — Add HandlerAutoRebase to node_pr_lifecycle_fix_effect
"""

from __future__ import annotations

import logging
import subprocess
from typing import Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


class ModelRebaseResult(BaseModel):
    """Result of a single PR auto-rebase attempt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr_number: int
    repo: str
    success: bool
    error_message: str | None = None
    rebase_sha: str | None = None


# ---------------------------------------------------------------------------
# Adapter protocol — injected at construction; swapped for mocks in tests
# ---------------------------------------------------------------------------


@runtime_checkable
class ProtocolRebaseAdapter(Protocol):
    """Minimal GitHub operations required by the auto-rebase handler."""

    async def update_branch(self, repo: str, pr_number: int) -> str:
        """Update (rebase) a PR branch against its base. Returns new HEAD SHA or action string."""
        ...


# ---------------------------------------------------------------------------
# Default live adapter (calls gh CLI)
# ---------------------------------------------------------------------------


class _LiveRebaseAdapter:
    """Live adapter that calls `gh pr update-branch`."""

    async def update_branch(self, repo: str, pr_number: int) -> str:
        result = subprocess.run(
            ["gh", "pr", "update-branch", str(pr_number), "--repo", repo],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            msg = f"gh pr update-branch failed (exit {result.returncode}): {result.stderr.strip()}"
            raise RuntimeError(msg)

        # Try to capture the new HEAD SHA for observability
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
        if sha_result.returncode == 0 and sha_result.stdout.strip():
            return sha_result.stdout.strip()
        return result.stdout.strip() or f"rebased {repo}#{pr_number}"


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class HandlerAutoRebase:
    """Auto-rebase stale PR branches via `gh pr update-branch`.

    In dry_run=True: logs intent, returns ModelRebaseResult(success=True) without
    calling gh.
    """

    def __init__(self, adapter: ProtocolRebaseAdapter | None = None) -> None:
        self._adapter: ProtocolRebaseAdapter = adapter or _LiveRebaseAdapter()

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
        self, *, pr_number: int, repo: str, dry_run: bool = False
    ) -> ModelRebaseResult:
        """Rebase a stale PR branch.

        Args:
            pr_number: GitHub PR number.
            repo: Repo slug (owner/repo).
            dry_run: When True, log intent and return success without calling gh.

        Returns:
            ModelRebaseResult indicating success or failure.
        """
        logger.info(
            "auto-rebase: pr=%s repo=%s dry_run=%s",
            pr_number,
            repo,
            dry_run,
        )

        if dry_run:
            logger.info(
                "[noop] would rebase branch for %s#%s via gh pr update-branch",
                repo,
                pr_number,
            )
            return ModelRebaseResult(pr_number=pr_number, repo=repo, success=True)

        try:
            sha = await self._adapter.update_branch(repo=repo, pr_number=pr_number)
            logger.info(
                "auto-rebase succeeded: pr=%s repo=%s sha=%s",
                pr_number,
                repo,
                sha,
            )
            return ModelRebaseResult(
                pr_number=pr_number, repo=repo, success=True, rebase_sha=sha
            )
        except Exception as exc:
            logger.warning(
                "auto-rebase failed: pr=%s repo=%s error=%s",
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


__all__: list[str] = [
    "HandlerAutoRebase",
    "ModelRebaseResult",
    "ProtocolRebaseAdapter",
]
