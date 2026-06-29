"""HandlerPrLifecycleFix — routes PR remediation by block reason.

Routes fix actions:
  ci_failure                     -> flaky/infra rerun via ``gh run rerun --failed``
  code_failure                   -> lint/type/test failure, delegate to pr_polish
  receipt_failure                -> OCC/receipt-gate failure, delegate to pr_polish
  conflict                       -> ``gh pr update-branch``, then pr_polish if still failing
  changes_requested              -> review-comment fix via pr_polish
  coderabbit                     -> CodeRabbit thread auto-reply via dispatch_coderabbit_reply
  deploy_gate_contract_not_found -> auto-create missing OCC contract via create_occ_contract
  receipt_evidence_source_autobind -> bind OCC receipt evidence + rewrite Evidence-Source
                                      via autobind_evidence_source (OMN-13317 F1)

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


@runtime_checkable
class ProtocolOccContractAdapter(Protocol):
    """OCC contract creation adapter required by the fix effect.

    Called when a PR fails deploy-gate because the OCC contract YAML
    ``onex_change_control/contracts/<ticket_id>.yaml`` does not exist.
    """

    async def create_occ_contract(
        self, repo: str, pr_number: int, ticket_id: str
    ) -> str:
        """Create a minimal OCC contract + receipt for the given ticket.

        Creates:
          - ``contracts/<ticket_id>.yaml`` — minimal ModelTicketContract YAML
          - ``drift/dod_receipts/<ticket_id>/dod-<repo_slug>-pr-<pr_number>/command.yaml``
            — receipt binding the contract to this PR

        Commits, pushes, opens an OCC PR, and updates the original PR body with
        ``Evidence-Source: OCC#<num>`` and ``Evidence-Ticket: <ticket_id>``.

        Returns a human-readable action string describing what was created.
        """
        ...


@runtime_checkable
class ProtocolOccAutobindAdapter(Protocol):
    """OCC Evidence-Source autobind adapter required by the fix effect.

    Called when a PR fails the Receipt Gate because its ``Evidence-Source``
    points at the product head SHA instead of an OCC source (OMN-13317 F1).
    """

    async def autobind_evidence_source(
        self, repo: str, pr_number: int, ticket_id: str | None = None
    ) -> str:
        """Bind OCC receipt evidence for the PR and rewrite its Evidence-Source.

        Detects the ticket, generates a receipt stamped with the real PR head
        SHA and number, opens/syncs an OCC binding PR, recomputes
        ``contract_sha256`` across all matching receipts, and PATCHes
        ``Evidence-Source: OCC#<n>`` back onto the product PR body via REST.

        Returns a human-readable action string describing what was bound.
        """
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


class _NoopOccContractAdapter:
    """No-op OCC contract adapter for dry_run and standalone execution."""

    async def create_occ_contract(
        self, repo: str, pr_number: int, ticket_id: str
    ) -> str:
        return f"[noop] would create OCC contract for {ticket_id} on {repo}#{pr_number}"


class _NoopOccAutobindAdapter:
    """No-op OCC autobind adapter for dry_run and standalone execution."""

    async def autobind_evidence_source(
        self, repo: str, pr_number: int, ticket_id: str | None = None
    ) -> str:
        return (
            f"[noop] would autobind Evidence-Source for "
            f"{ticket_id or '<auto>'} on {repo}#{pr_number}"
        )


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
        occ_contract_adapter: ProtocolOccContractAdapter | None = None,
        occ_autobind_adapter: ProtocolOccAutobindAdapter | None = None,
    ) -> None:
        self._github: ProtocolGitHubAdapter = github_adapter or _NoopGitHubAdapter()
        self._agent: ProtocolAgentDispatchAdapter = (
            agent_dispatch_adapter or _NoopAgentDispatchAdapter()
        )
        self._occ: ProtocolOccContractAdapter = (
            occ_contract_adapter or _NoopOccContractAdapter()
        )
        self._occ_autobind: ProtocolOccAutobindAdapter = (
            occ_autobind_adapter or _NoopOccAutobindAdapter()
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
            # Flaky/infrastructure failure — rerun without code changes.
            return await self._github.rerun_failed_checks(repo, pr)

        if reason in {
            EnumPrBlockReason.CODE_FAILURE,
            EnumPrBlockReason.RECEIPT_FAILURE,
            EnumPrBlockReason.CHANGES_REQUESTED,
        }:
            # Code, receipt, or review-comment failure — delegate to pr_polish.
            return await self._agent.dispatch_review_fix(repo, pr, command.ticket_id)

        if reason == EnumPrBlockReason.CONFLICT:
            return await self._github.resolve_conflicts(repo, pr)

        if reason == EnumPrBlockReason.CODERABBIT:
            return await self._agent.dispatch_coderabbit_reply(repo, pr)

        if reason == EnumPrBlockReason.DEPLOY_GATE_CONTRACT_NOT_FOUND:
            # deploy-gate failed because the OCC contract YAML is missing.
            # ticket_id is required; raise if absent so the caller gets a clear error.
            if not command.ticket_id:
                msg = (
                    f"deploy_gate_contract_not_found fix requires ticket_id "
                    f"on {repo}#{pr}"
                )
                raise ValueError(msg)
            return await self._occ.create_occ_contract(repo, pr, command.ticket_id)

        if reason == EnumPrBlockReason.RECEIPT_EVIDENCE_SOURCE_AUTOBIND:
            # Receipt Gate failed: Evidence-Source points at the product head
            # SHA instead of an OCC source. Bind OCC evidence and rewrite the
            # PR body. ticket_id is optional — the adapter detects it from the
            # PR title/body when absent (OMN-13317 F1).
            return await self._occ_autobind.autobind_evidence_source(
                repo, pr, command.ticket_id
            )

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
    "ProtocolOccAutobindAdapter",
    "ProtocolOccContractAdapter",
]
