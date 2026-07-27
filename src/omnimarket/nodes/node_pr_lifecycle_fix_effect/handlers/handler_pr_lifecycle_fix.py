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
from typing import NamedTuple, Protocol, runtime_checkable
from uuid import UUID

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_two_strike_store import (
    ProtocolTwoStrikeStore,
    strike_key,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.delegation_eligibility import (
    TWO_STRIKE_THRESHOLD,
    is_delegation_eligible,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_evidence_stamp import (
    classify_trivial_infra_fastpath,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_command import (
    EnumPrBlockReason,
    ModelPrLifecycleFixCommand,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_result import (
    EnumDelegationOutcome,
    ModelOccCompanionVerification,
    ModelPrLifecycleFixResult,
)

logger = logging.getLogger(__name__)


class _DelegationInfo(NamedTuple):
    """Delegation bookkeeping for a single ``_route`` call (WS-D/D2, OMN-13940)."""

    delegated: bool
    model: str | None
    outcome: EnumDelegationOutcome | None
    cost_usd: float | None


_NOT_DELEGATED = _DelegationInfo(
    delegated=False, model=None, outcome=None, cost_usd=None
)


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
class ProtocolDelegationFixAdapter(Protocol):
    """Delegated (non-Claude) fix path required by the fix effect (WS-D/D2).

    Called only for CODE_FAILURE / CHANGES_REQUESTED once
    ``delegation_eligibility.is_delegation_eligible`` has approved the PR.
    Implementations own worktree resolution, the actual fix (deterministic
    tool or LLM delegation), local gates/verify, commit-with-trailer, and
    re-entry into the existing pr_polish push/coderabbit-triage/auto-merge-arm
    flow. Must raise on any gate/verify failure — the caller records a
    two-strike and NEVER treats a raised exception as a push.
    """

    async def dispatch_delegated_fix(
        self,
        repo: str,
        pr_number: int,
        ticket_id: str | None,
        command: ModelPrLifecycleFixCommand,
    ) -> str:
        """Run the delegated fix. Returns a human-readable action string on
        success; raises on any failure (denylist trip, size gate, git apply
        failure, or a local-gate/verify failure surfaced from pr_polish)."""
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


@runtime_checkable
class ProtocolOccCompanionVerifier(Protocol):
    """Independent read-back verifier for a pushed OCC Evidence-Source companion.

    OMN-14173: the autobind arm's ``fix_applied`` flag reports only that the
    adapter call returned without raising — it does NOT prove a companion was
    pushed. This verifier re-reads GitHub to confirm the EFFECT (Evidence-Source
    patch + open OCC PR + companion branch). ``prs_fixed`` is gated on its
    ``verified`` result, never on the in-memory ``fix_applied`` flag.
    """

    async def verify_companion(
        self, repo: str, pr_number: int, ticket_id: str | None = None
    ) -> ModelOccCompanionVerification:
        """Read GitHub back and return whether the OCC companion actually landed."""
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


class _UnverifiedOccCompanionVerifier:
    """Fail-closed default verifier — proves nothing, so counts nothing.

    OMN-14173: when no real read-back verifier is wired (standalone / dry-run /
    the runtime-boot path), the OCC companion cannot be independently confirmed,
    so verification returns ``verified=False``. This is the safe direction — a
    missing verifier UNDER-counts (never falsely counts) ``prs_fixed``. The
    merge-sweep orchestrator injects the live :class:`OccCompanionVerifier`.
    """

    async def verify_companion(
        self, repo: str, pr_number: int, ticket_id: str | None = None
    ) -> ModelOccCompanionVerification:
        return ModelOccCompanionVerification(
            verified=False,
            detail=(
                "no OCC companion verifier wired; fail-closed (cannot prove the "
                "companion was pushed)"
            ),
        )


class _NoopDelegationFixAdapter:
    """No-op delegation-fix adapter for dry_run and standalone execution."""

    async def dispatch_delegated_fix(
        self,
        repo: str,
        pr_number: int,
        ticket_id: str | None,
        command: ModelPrLifecycleFixCommand,
    ) -> str:
        return f"[noop] would dispatch delegated fix on {repo}#{pr_number}"


class _InMemoryTwoStrikeStore:
    """Ephemeral in-memory two-strike counter for standalone/test use.

    Does NOT persist across process restarts. Real merge-sweep wiring injects
    ``JsonFileTwoStrikeStore`` via the orchestrator's ``_ensure_sub_handlers``
    so the two-strike threshold survives across ticks (safety bar #7).
    """

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}

    def get_strikes(self, key: str) -> int:
        return self._counts.get(key, 0)

    def record_failure(self, key: str) -> int:
        self._counts[key] = self._counts.get(key, 0) + 1
        return self._counts[key]


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
        occ_companion_verifier: ProtocolOccCompanionVerifier | None = None,
        delegation_fix_adapter: ProtocolDelegationFixAdapter | None = None,
        two_strike_store: ProtocolTwoStrikeStore | None = None,
        delegation_model_name: str = "ruff-deterministic",
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
        # OMN-14173: independent read-back verifier for the OCC autobind arm.
        # Defaults fail-closed (proves nothing → counts nothing); the merge-sweep
        # orchestrator injects the live OccCompanionVerifier so a real run gates
        # prs_fixed on a confirmed pushed companion, not on the fix_applied flag.
        self._occ_verifier: ProtocolOccCompanionVerifier = (
            occ_companion_verifier or _UnverifiedOccCompanionVerifier()
        )
        # WS-D/D2 (OMN-13940): delegated (non-Claude) fix path for
        # CODE_FAILURE / CHANGES_REQUESTED. Defaults to noop + an in-memory
        # two-strike store (test/standalone convenience) — production wiring
        # in HandlerPrLifecycleOrchestrator._ensure_sub_handlers injects the
        # persistent JsonFileTwoStrikeStore explicitly.
        self._delegation_fix: ProtocolDelegationFixAdapter = (
            delegation_fix_adapter or _NoopDelegationFixAdapter()
        )
        self._two_strike: ProtocolTwoStrikeStore = (
            two_strike_store or _InMemoryTwoStrikeStore()
        )
        self._delegation_model_name = delegation_model_name

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
        occ_companion_verified = False
        delegation_info = _NOT_DELEGATED

        try:
            fix_action, delegation_info = await self._route(command)
            fix_applied = True
            # OMN-14173 fail-closed accounting: the autobind arm's success is
            # measured by the EFFECT (a pushed OCC companion + Evidence-Source
            # patch), never by the call returning. Re-read GitHub to confirm.
            # `fix_applied` stays True (the route ran), but the orchestrator
            # counts prs_fixed for this arm ONLY when the companion is verified,
            # so a no-op/short-circuit dispatch can never inflate the count.
            if (
                command.block_reason
                == EnumPrBlockReason.RECEIPT_EVIDENCE_SOURCE_AUTOBIND
            ):
                verification = await self._occ_verifier.verify_companion(
                    command.repo, command.pr_number, command.ticket_id
                )
                occ_companion_verified = verification.verified
                if not occ_companion_verified:
                    fix_action = (
                        f"{fix_action} | OCC companion NOT verified: "
                        f"{verification.detail}"
                    )
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
            occ_companion_verified=occ_companion_verified,
            error=error,
            completed_at=datetime.now(tz=UTC),
            delegated=delegation_info.delegated,
            delegation_model=delegation_info.model,
            delegation_outcome=delegation_info.outcome,
            delegation_cost_usd=delegation_info.cost_usd,
        )

    async def _route(
        self, command: ModelPrLifecycleFixCommand
    ) -> tuple[str, _DelegationInfo]:
        """Dispatch to the correct adapter based on block_reason."""
        reason = command.block_reason
        repo = command.repo
        pr = command.pr_number

        if reason == EnumPrBlockReason.CI_FAILURE:
            # Flaky/infrastructure failure — rerun without code changes.
            return await self._github.rerun_failed_checks(repo, pr), _NOT_DELEGATED

        if reason == EnumPrBlockReason.RECEIPT_FAILURE:
            # Safety bar #5: an OCC/receipt-gate surface. Stays on the Claude
            # agent path unconditionally — split out of the delegatable
            # CODE_FAILURE/CHANGES_REQUESTED branch and NEVER delegated.
            action = await self._agent.dispatch_review_fix(repo, pr, command.ticket_id)
            return action, _NOT_DELEGATED

        if reason in {
            EnumPrBlockReason.CODE_FAILURE,
            EnumPrBlockReason.CHANGES_REQUESTED,
        }:
            return await self._route_delegatable_fix(command)

        if reason == EnumPrBlockReason.CONFLICT:
            return await self._github.resolve_conflicts(repo, pr), _NOT_DELEGATED

        if reason == EnumPrBlockReason.CODERABBIT:
            action = await self._agent.dispatch_coderabbit_reply(repo, pr)
            return action, _NOT_DELEGATED

        if reason == EnumPrBlockReason.DEPLOY_GATE_CONTRACT_NOT_FOUND:
            # Trivial-infra OCC fast-path (OMN-13776): a one-line non-runtime
            # infra edit skips the full OCC contract + receipt-chain PR
            # entirely — no skip token, decided purely from changed_files /
            # diff_total_lines size-and-path scoping.
            fastpath_eligible, fastpath_reason = classify_trivial_infra_fastpath(
                command.changed_files, command.diff_total_lines
            )
            if fastpath_eligible:
                logger.info(
                    "PR lifecycle fix: trivial-infra OCC fast-path hit "
                    "pr=%s repo=%s reason=%s",
                    pr,
                    repo,
                    fastpath_reason,
                )
                return f"OCC fast-path: {fastpath_reason}", _NOT_DELEGATED

            # deploy-gate failed because the OCC contract YAML is missing.
            # ticket_id is required; raise if absent so the caller gets a clear error.
            if not command.ticket_id:
                msg = (
                    f"deploy_gate_contract_not_found fix requires ticket_id "
                    f"on {repo}#{pr}"
                )
                raise ValueError(msg)
            action = await self._occ.create_occ_contract(repo, pr, command.ticket_id)
            return action, _NOT_DELEGATED

        if reason == EnumPrBlockReason.RECEIPT_EVIDENCE_SOURCE_AUTOBIND:
            # Receipt Gate failed: Evidence-Source points at the product head
            # SHA instead of an OCC source. Bind OCC evidence and rewrite the
            # PR body. ticket_id is optional — the adapter detects it from the
            # PR title/body when absent (OMN-13317 F1).
            action = await self._occ_autobind.autobind_evidence_source(
                repo, pr, command.ticket_id
            )
            return action, _NOT_DELEGATED

        msg = f"Unhandled block_reason: {reason!r}"
        raise ValueError(msg)

    async def _route_delegatable_fix(
        self, command: ModelPrLifecycleFixCommand
    ) -> tuple[str, _DelegationInfo]:
        """Route CODE_FAILURE / CHANGES_REQUESTED to delegation or the agent.

        Safety bar #7 (two-strike): a second delegation failure on the same
        PR/block_reason permanently disables delegation for that key — the
        call that trips the second strike escalates to the agent immediately;
        every later call for that key is ineligible from the start.
        """
        repo, pr = command.repo, command.pr_number
        key = strike_key(repo, pr, command.block_reason.value)
        strikes = self._two_strike.get_strikes(key)
        eligible, eligibility_reason = is_delegation_eligible(
            block_reason=command.block_reason.value,
            changed_files=command.changed_files,
            diff_total_lines=command.diff_total_lines,
            review_context_text=command.review_context_text,
            strikes=strikes,
        )

        if not eligible:
            logger.info(
                "delegation ineligible: pr=%s repo=%s reason=%s strikes=%d",
                pr,
                repo,
                eligibility_reason,
                strikes,
            )
            action = await self._agent.dispatch_review_fix(repo, pr, command.ticket_id)
            return action, _DelegationInfo(
                delegated=False,
                model=None,
                outcome=EnumDelegationOutcome.NOT_ATTEMPTED,
                cost_usd=None,
            )

        try:
            action = await self._delegation_fix.dispatch_delegated_fix(
                repo, pr, command.ticket_id, command
            )
        except Exception as exc:
            new_strikes = self._two_strike.record_failure(key)
            logger.warning(
                "delegated fix failed: pr=%s repo=%s strikes=%d error=%s",
                pr,
                repo,
                new_strikes,
                exc,
            )
            if new_strikes >= TWO_STRIKE_THRESHOLD:
                fallback = await self._agent.dispatch_review_fix(
                    repo, pr, command.ticket_id
                )
                action = (
                    f"delegated fix failed permanently ({exc}); "
                    f"escalated to agent: {fallback}"
                )
                return action, _DelegationInfo(
                    delegated=True,
                    model=self._delegation_model_name,
                    outcome=EnumDelegationOutcome.ESCALATED,
                    cost_usd=0.0,
                )
            action = (
                f"delegated fix failed ({exc}); strike {new_strikes}/"
                f"{TWO_STRIKE_THRESHOLD}, no push — will retry or escalate next tick"
            )
            return action, _DelegationInfo(
                delegated=True,
                model=self._delegation_model_name,
                outcome=EnumDelegationOutcome.GATE_FAILED,
                cost_usd=0.0,
            )

        return action, _DelegationInfo(
            delegated=True,
            model=self._delegation_model_name,
            outcome=EnumDelegationOutcome.ACCEPTED,
            cost_usd=0.0,
        )

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
    "ProtocolDelegationFixAdapter",
    "ProtocolGitHubAdapter",
    "ProtocolOccAutobindAdapter",
    "ProtocolOccCompanionVerifier",
    "ProtocolOccContractAdapter",
]
