# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerPrLifecycleMerge — auto-merge execution for green PRs.

Consumes triage events for PRs classified green and calls the GitHub merge
API via a protocol-injected adapter. Respects per-repo merge queue policy:
  - use_merge_queue=True  -> --auto (no method; queue determines strategy)
  - use_merge_queue=False -> --squash --auto

Protocol-injected ProtocolGitHubMergeAdapter allows mock substitution in
tests with zero infrastructure.

Related:
    - OMN-8084: Create pr_lifecycle_merge_effect Node
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Literal, Protocol, runtime_checkable
from uuid import UUID

from omnimarket.nodes.node_pr_lifecycle_merge_effect.models.model_merge_command import (
    ModelPrMergeCommand,
)
from omnimarket.nodes.node_pr_lifecycle_merge_effect.models.model_merge_result import (
    ModelPrMergeResult,
)

logger = logging.getLogger(__name__)

HandlerType = Literal["NODE_HANDLER", "INFRA_HANDLER", "PROJECTION_HANDLER"]
HandlerCategory = Literal["EFFECT", "COMPUTE", "NONDETERMINISTIC_COMPUTE"]

_EXPECTED_VERDICT = "green"


# ---------------------------------------------------------------------------
# Adapter protocol — injected at construction; swapped for mocks in tests
# ---------------------------------------------------------------------------


@runtime_checkable
class ProtocolGitHubMergeAdapter(Protocol):
    """Minimal GitHub merge operations required by the merge effect.

    Implementations call the GitHub API to trigger auto-merge.
    The merge strategy is determined by ``use_merge_queue``:
      - True  -> merge queue path (--auto, no method)
      - False -> squash auto-merge (--squash --auto)
    """

    async def merge_pr(
        self,
        repo: str,
        pr_number: int,
        use_merge_queue: bool,
    ) -> str:
        """Execute auto-merge for a PR.

        Args:
            repo: GitHub repo slug (owner/repo).
            pr_number: PR number to merge.
            use_merge_queue: When True, use merge queue path; else squash.

        Returns:
            Human-readable action string describing what was done.
        """
        ...

    async def post_pr_comment(
        self,
        repo: str,
        pr_number: int,
        body: str,
    ) -> None:
        """Post a comment on a PR.

        Args:
            repo: GitHub repo slug (owner/repo).
            pr_number: PR number to comment on.
            body: Comment body text.
        """
        ...


# ---------------------------------------------------------------------------
# Default no-op adapter (used in standalone / dry-run mode)
# ---------------------------------------------------------------------------


class _NoopGitHubMergeAdapter:
    """No-op GitHub merge adapter for dry_run and standalone execution."""

    async def merge_pr(
        self,
        repo: str,
        pr_number: int,
        use_merge_queue: bool,
    ) -> str:
        strategy = "queue" if use_merge_queue else "squash"
        return f"[noop] would auto-merge {repo}#{pr_number} via {strategy}"

    async def post_pr_comment(
        self,
        repo: str,
        pr_number: int,
        body: str,
    ) -> None:
        logger.debug("[noop] would post comment on %s#%s: %s", repo, pr_number, body)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class HandlerPrLifecycleMerge:
    """Executes auto-merge for PRs classified green by triage.

    Accepts a protocol adapter for GitHub merge operations so tests can
    inject mocks without any infrastructure. Default adapter is a no-op
    suitable for dry_run and standalone execution.

    Merge queue policy:
      - use_merge_queue=True  -> merge queue path (no method specified)
      - use_merge_queue=False -> squash auto-merge
    """

    def __init__(
        self,
        github_adapter: ProtocolGitHubMergeAdapter | None = None,
    ) -> None:
        self._github: ProtocolGitHubMergeAdapter = (
            github_adapter or _NoopGitHubMergeAdapter()
        )

    @property
    def handler_type(self) -> HandlerType:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> HandlerCategory:
        return "EFFECT"

    @property
    def correlation_id(self) -> UUID | None:
        return None

    async def handle(self, command: ModelPrMergeCommand) -> ModelPrMergeResult:
        """Execute auto-merge for a green PR.

        Validates that the triage verdict is 'green' before proceeding.
        In dry_run mode, the no-op adapter describes the action that would
        be taken without making any external calls.

        Args:
            command: Merge command including PR coordinates and policy flags.

        Returns:
            ModelPrMergeResult with merged status and action description.
        """
        logger.info(
            "PR lifecycle merge: pr=%s repo=%s verdict=%s use_merge_queue=%s "
            "dry_run=%s correlation_id=%s",
            command.pr_number,
            command.repo,
            command.triage_verdict,
            command.use_merge_queue,
            command.dry_run,
            command.correlation_id,
        )

        if command.triage_verdict != _EXPECTED_VERDICT:
            msg = (
                f"Merge effect received non-green triage verdict "
                f"'{command.triage_verdict}' for {command.repo}#{command.pr_number}. "
                f"Only 'green' PRs should be sent to this node."
            )
            logger.warning(msg)
            return ModelPrMergeResult(
                correlation_id=command.correlation_id,
                pr_number=command.pr_number,
                repo=command.repo,
                merged=False,
                merge_action=f"skipped: verdict={command.triage_verdict!r} is not green",
                error=msg,
                completed_at=datetime.now(tz=UTC),
            )

        merge_action: str
        error: str | None = None
        merged = False

        if command.dry_run:
            strategy = "queue" if command.use_merge_queue else "squash"
            merge_action = f"[noop] would auto-merge {command.repo}#{command.pr_number} via {strategy}"
            merged = True
            return ModelPrMergeResult(
                correlation_id=command.correlation_id,
                pr_number=command.pr_number,
                repo=command.repo,
                merged=merged,
                merge_action=merge_action,
                error=error,
                completed_at=datetime.now(tz=UTC),
            )

        try:
            merge_action = await self._github.merge_pr(
                repo=command.repo,
                pr_number=command.pr_number,
                use_merge_queue=command.use_merge_queue,
            )
            merged = True
            comment_body = (
                f"<!-- onex-correlation-id: {command.correlation_id} -->\n"
                f"Auto-merged by merge-sweep | correlation_id: `{command.correlation_id}`"
            )
            try:
                await self._github.post_pr_comment(
                    repo=command.repo,
                    pr_number=command.pr_number,
                    body=comment_body,
                )
            except Exception as comment_exc:
                logger.warning(
                    "PR lifecycle merge: failed to post correlation comment pr=%s repo=%s correlation_id=%s: %s",
                    command.pr_number,
                    command.repo,
                    command.correlation_id,
                    comment_exc,
                )
        except Exception as exc:
            merge_action = f"failed: {exc}"
            error = str(exc)
            logger.warning(
                "PR lifecycle merge failed: pr=%s repo=%s error=%s",
                command.pr_number,
                command.repo,
                exc,
                exc_info=True,
            )

        logger.info(
            "PR lifecycle merge complete: pr=%s repo=%s merged=%s",
            command.pr_number,
            command.repo,
            merged,
        )

        return ModelPrMergeResult(
            correlation_id=command.correlation_id,
            pr_number=command.pr_number,
            repo=command.repo,
            merged=merged,
            merge_action=merge_action,
            error=error,
            completed_at=datetime.now(tz=UTC),
        )


__all__: list[str] = [
    "HandlerPrLifecycleMerge",
    "ProtocolGitHubMergeAdapter",
]
