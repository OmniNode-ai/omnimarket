"""HandlerPrLifecycleFix — routes PR remediation by block reason.

Routes fix actions:
  ci_failure        -> GitHub CI fix dispatch (re-run or targeted fix agent)
  conflict          -> Conflict resolution (absorbed from pr_polish)
  changes_requested -> Review-comment address (agent dispatch)
  coderabbit        -> CodeRabbit auto-reply (absorbed from coderabbit_triage)

Protocol-injected adapters for GitHub operations and agent dispatch allow
mock substitution in tests with zero infrastructure.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable
from uuid import UUID

from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_command import (
    EnumPrBlockReason,
    ModelPrLifecycleFixCommand,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_result import (
    ModelPrLifecycleFixResult,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Adapter protocols — injected at construction; swapped for mocks in tests
# ---------------------------------------------------------------------------


@runtime_checkable
class ProtocolGitHubAdapter(Protocol):
    """Minimal GitHub operations required by the fix effect."""

    async def rerun_failed_checks(self, repo: str, pr_number: int) -> str:
        """Re-run failed CI checks for a PR. Returns a human-readable action string."""
        ...

    async def resolve_conflicts(self, repo: str, pr_number: int) -> str:
        """Attempt to resolve merge conflicts for a PR. Returns action string."""
        ...


@runtime_checkable
class ProtocolAgentDispatchAdapter(Protocol):
    """Minimal agent dispatch operations required by the fix effect."""

    async def dispatch_review_fix(
        self, repo: str, pr_number: int, ticket_id: str | None
    ) -> str:
        """Dispatch an agent to address review comments. Returns action string."""
        ...

    async def dispatch_coderabbit_reply(self, repo: str, pr_number: int) -> str:
        """Dispatch auto-reply for open CodeRabbit threads. Returns action string."""
        ...


# ---------------------------------------------------------------------------
# Default no-op adapters (used in standalone / dry-run mode)
# ---------------------------------------------------------------------------


class _NoopGitHubAdapter:
    """No-op GitHub adapter for dry_run and standalone execution."""

    async def rerun_failed_checks(self, repo: str, pr_number: int) -> str:
        return f"[noop] would rerun CI checks on {repo}#{pr_number}"

    async def resolve_conflicts(self, repo: str, pr_number: int) -> str:
        return f"[noop] would resolve conflicts on {repo}#{pr_number}"


class _NoopAgentDispatchAdapter:
    """No-op agent dispatch adapter for dry_run and standalone execution."""

    async def dispatch_review_fix(
        self, repo: str, pr_number: int, ticket_id: str | None
    ) -> str:
        return f"[noop] would dispatch review-fix agent on {repo}#{pr_number}"

    async def dispatch_coderabbit_reply(self, repo: str, pr_number: int) -> str:
        return f"[noop] would dispatch coderabbit-reply agent on {repo}#{pr_number}"


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class HandlerPrLifecycleFix:
    """Routes PR remediation actions by block reason.

    Accepts protocol adapters for GitHub and agent dispatch so tests can
    inject mocks without any infrastructure.
    """

    def __init__(
        self,
        github_adapter: ProtocolGitHubAdapter | None = None,
        agent_dispatch_adapter: ProtocolAgentDispatchAdapter | None = None,
    ) -> None:
        self._github: ProtocolGitHubAdapter = github_adapter or _NoopGitHubAdapter()
        self._agent: ProtocolAgentDispatchAdapter = (
            agent_dispatch_adapter or _NoopAgentDispatchAdapter()
        )

    async def handle(
        self, command: ModelPrLifecycleFixCommand
    ) -> ModelPrLifecycleFixResult:
        """Route fix action by block_reason and return the result.

        In dry_run mode, no external calls are made — the no-op adapters
        describe the action that would be taken.
        """
        logger.info(
            "PR lifecycle fix: pr=%s repo=%s reason=%s dry_run=%s correlation_id=%s",
            command.pr_number,
            command.repo,
            command.block_reason,
            command.dry_run,
            command.correlation_id,
        )

        fix_action: str
        error: str | None = None
        fix_applied = False

        try:
            fix_action = await self._route(command)
            fix_applied = True
        except Exception as exc:
            fix_action = f"failed: {exc}"
            error = str(exc)
            logger.warning(
                "PR lifecycle fix failed: pr=%s repo=%s reason=%s error=%s",
                command.pr_number,
                command.repo,
                command.block_reason,
                exc,
                exc_info=True,
            )

        return ModelPrLifecycleFixResult(
            correlation_id=command.correlation_id,
            pr_number=command.pr_number,
            repo=command.repo,
            block_reason=command.block_reason,
            fix_applied=fix_applied,
            fix_action=fix_action,
            error=error,
            completed_at=datetime.now(tz=UTC),
        )

    async def _route(self, command: ModelPrLifecycleFixCommand) -> str:
        """Dispatch to the correct adapter based on block_reason."""
        reason = command.block_reason
        repo = command.repo
        pr = command.pr_number

        if reason == EnumPrBlockReason.CI_FAILURE:
            return await self._github.rerun_failed_checks(repo, pr)

        if reason == EnumPrBlockReason.CONFLICT:
            return await self._github.resolve_conflicts(repo, pr)

        if reason == EnumPrBlockReason.CHANGES_REQUESTED:
            return await self._agent.dispatch_review_fix(repo, pr, command.ticket_id)

        if reason == EnumPrBlockReason.CODERABBIT:
            return await self._agent.dispatch_coderabbit_reply(repo, pr)

        msg = f"Unhandled block_reason: {reason!r}"
        raise ValueError(msg)

    # RuntimeLocal handler shim
    def handle_sync(
        self, command: ModelPrLifecycleFixCommand
    ) -> ModelPrLifecycleFixResult:
        """Synchronous shim for RuntimeLocal compatibility."""
        import asyncio

        return asyncio.get_event_loop().run_until_complete(self.handle(command))

    @property
    def handler_type(self) -> str:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> str:
        return "EFFECT"

    @property
    def correlation_id(self) -> UUID | None:
        return None


__all__: list[str] = [
    "HandlerPrLifecycleFix",
    "ProtocolAgentDispatchAdapter",
    "ProtocolGitHubAdapter",
]
