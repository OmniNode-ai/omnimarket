# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerAutoRebase — auto-rebase stale PR branches via GitHub's update-branch API.

Targets Track A-update PRs (merge_state_status=BEHIND or UNKNOWN).
Protocol-injected adapter allows mock substitution in tests with zero infra.

Related:
    - OMN-8204: Task 7 — Add HandlerAutoRebase to node_pr_lifecycle_fix_effect
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from omnimarket.github_api import rest_json, split_repo

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
# Default live adapter (GitHub REST API)
# ---------------------------------------------------------------------------


class _LiveRebaseAdapter:
    """Live adapter that calls GitHub's update-branch API."""

    async def update_branch(self, repo: str, pr_number: int) -> str:
        return await asyncio.to_thread(self._update_branch_sync, repo, pr_number)

    def _update_branch_sync(self, repo: str, pr_number: int) -> str:
        owner, repo_name = split_repo(repo)
        pr = rest_json("GET", f"/repos/{owner}/{repo_name}/pulls/{pr_number}")
        head = pr.get("head") or {}
        head_sha = head.get("sha")
        if not isinstance(head_sha, str) or not head_sha:
            raise RuntimeError(
                f"update-branch failed for {repo}#{pr_number}: missing head sha"
            )
        rest_json(
            "PUT",
            f"/repos/{owner}/{repo_name}/pulls/{pr_number}/update-branch",
            body={"expected_head_sha": head_sha},
        )
        refreshed = rest_json("GET", f"/repos/{owner}/{repo_name}/pulls/{pr_number}")
        refreshed_head = refreshed.get("head") or {}
        new_sha = refreshed_head.get("sha")
        if isinstance(new_sha, str) and new_sha:
            return new_sha
        return f"rebased {repo}#{pr_number}"


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class HandlerAutoRebase:
    """Auto-rebase stale PR branches via the GitHub update-branch API.

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
                "[noop] would rebase branch for %s#%s via update-branch API",
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
