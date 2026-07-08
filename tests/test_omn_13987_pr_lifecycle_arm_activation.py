# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13987 — activate dead-pathed PR-automation arms via the two enum chokepoints.

Two chokepoints in ``node_pr_lifecycle_orchestrator`` left built-and-tested fix
arms / effect nodes unreachable in production:

* CP1 — ``_block_reason_for_fix`` never emitted DEPLOY_GATE_CONTRACT_NOT_FOUND /
  RECEIPT_EVIDENCE_SOURCE_AUTOBIND / CI_FAILURE, so the OccContractAdapter,
  OccAutobindAdapter, and GitHubCliAdapter.rerun_failed_checks arms in
  ``node_pr_lifecycle_fix_effect`` were dead.
* CP2 — ``_publish_fixer_dispatch_start`` emitted ``stall_category = block_reason``
  (prose), which never matched the ``EnumStallCategory`` routing table in
  ``node_fixer_dispatcher``, so every dispatch escalated and
  node_ci_fix_effect / node_conflict_hunk_effect were dead.

These tests prove BEHAVIORAL routing (the right machine enum for the right
machine signature AND the right adapter / effect node reached), plus negative
anti-over-broaden cases: genuine code failures and genuine OCC dependencies must
keep their existing routes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from omnimarket.nodes.node_fixer_dispatcher.handlers.handler_fixer_dispatcher import (
    HandlerFixerDispatcher,
)
from omnimarket.nodes.node_fixer_dispatcher.models.model_fixer_dispatch import (
    EnumStallCategory,
    ModelFixerDispatchRequest,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.handler_pr_lifecycle_fix import (
    HandlerPrLifecycleFix,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_command import (
    EnumPrBlockReason,
    ModelPrLifecycleFixCommand,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
    _PR_CATEGORY_TO_STALL_CATEGORY,
    _block_reason_for_fix,
    _stall_category_for_dispatch,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.protocols.protocol_sub_handlers import (
    EnumPrCategory,
    TriageRecord,
)

_RECEIPT_GATE = "verify / verify"
_DEPLOY_GATE = "deploy-gate / deploy-gate"


def _record(
    *,
    category: EnumPrCategory,
    failed_check_names: tuple[str, ...] = (),
    failed_check_flaky_evidence: tuple[str, ...] = (),
    ticket_ids: tuple[str, ...] = (),
    block_reason: str = "",
) -> TriageRecord:
    return TriageRecord(
        pr_number=42,
        repo="OmniNode-ai/omnimarket",
        category=category,
        ticket_ids=ticket_ids,
        failed_check_names=failed_check_names,
        failed_check_flaky_evidence=failed_check_flaky_evidence,
        block_reason=block_reason,
    )


# ---------------------------------------------------------------------------
# Mock adapters — duck-typed to the fix-effect protocol surface. Each records
# the arm it was routed to so we assert BEHAVIORAL routing, not just imports.
# ---------------------------------------------------------------------------


class _RecordingGitHubAdapter:
    def __init__(self) -> None:
        self.rerun_calls: list[tuple[str, int]] = []
        self.conflict_calls: list[tuple[str, int]] = []

    async def rerun_failed_checks(self, repo: str, pr_number: int) -> str:
        self.rerun_calls.append((repo, pr_number))
        return f"[mock] rerun {repo}#{pr_number}"

    async def resolve_conflicts(self, repo: str, pr_number: int) -> str:
        self.conflict_calls.append((repo, pr_number))
        return f"[mock] resolved {repo}#{pr_number}"


class _RecordingAgentAdapter:
    def __init__(self) -> None:
        self.review_fix_calls: list[tuple[str, int, str | None]] = []
        self.coderabbit_calls: list[tuple[str, int]] = []

    async def dispatch_review_fix(
        self, repo: str, pr_number: int, ticket_id: str | None
    ) -> str:
        self.review_fix_calls.append((repo, pr_number, ticket_id))
        return f"[mock] review-fix {repo}#{pr_number}"

    async def dispatch_coderabbit_reply(self, repo: str, pr_number: int) -> str:
        self.coderabbit_calls.append((repo, pr_number))
        return f"[mock] coderabbit {repo}#{pr_number}"


class _RecordingOccContractAdapter:
    def __init__(self) -> None:
        self.create_calls: list[tuple[str, int, str]] = []

    async def create_occ_contract(
        self, repo: str, pr_number: int, ticket_id: str
    ) -> str:
        self.create_calls.append((repo, pr_number, ticket_id))
        return f"[mock] created OCC contract {ticket_id} {repo}#{pr_number}"


class _RecordingOccAutobindAdapter:
    def __init__(self) -> None:
        self.autobind_calls: list[tuple[str, int, str | None]] = []

    async def autobind_evidence_source(
        self, repo: str, pr_number: int, ticket_id: str | None = None
    ) -> str:
        self.autobind_calls.append((repo, pr_number, ticket_id))
        return f"[mock] autobound {repo}#{pr_number}"


def _fix_command(
    block_reason: EnumPrBlockReason,
    *,
    ticket_id: str | None = "OMN-8085",
    changed_files: list[str] | None = None,
    diff_total_lines: int = 0,
) -> ModelPrLifecycleFixCommand:
    return ModelPrLifecycleFixCommand(
        correlation_id=uuid4(),
        pr_number=42,
        repo="OmniNode-ai/omnimarket",
        block_reason=block_reason,
        ticket_id=ticket_id,
        changed_files=changed_files or [],
        diff_total_lines=diff_total_lines,
        dry_run=False,
        requested_at=datetime.now(tz=UTC),
    )


def _fix_handler() -> tuple[
    HandlerPrLifecycleFix,
    _RecordingGitHubAdapter,
    _RecordingAgentAdapter,
    _RecordingOccContractAdapter,
    _RecordingOccAutobindAdapter,
]:
    gh = _RecordingGitHubAdapter()
    agent = _RecordingAgentAdapter()
    occ = _RecordingOccContractAdapter()
    autobind = _RecordingOccAutobindAdapter()
    handler = HandlerPrLifecycleFix(
        github_adapter=gh,
        agent_dispatch_adapter=agent,
        occ_contract_adapter=occ,
        occ_autobind_adapter=autobind,
    )
    return handler, gh, agent, occ, autobind


# ===========================================================================
# CP1 — classifier emits the right machine enum for the right machine signature
# ===========================================================================


@pytest.mark.unit
class TestChokepoint1Classifier:
    def test_deploy_gate_check_routes_to_contract_not_found(self) -> None:
        pr = _record(
            category=EnumPrCategory.RED,
            failed_check_names=(_DEPLOY_GATE,),
            ticket_ids=("OMN-8085",),
        )
        assert _block_reason_for_fix(pr) == (
            EnumPrBlockReason.DEPLOY_GATE_CONTRACT_NOT_FOUND
        )

    def test_receipt_only_with_ticket_routes_to_autobind(self) -> None:
        pr = _record(
            category=EnumPrCategory.RED,
            failed_check_names=(_RECEIPT_GATE,),
            ticket_ids=("OMN-13317",),
        )
        assert _block_reason_for_fix(pr) == (
            EnumPrBlockReason.RECEIPT_EVIDENCE_SOURCE_AUTOBIND
        )

    def test_red_all_flaky_infra_routes_to_ci_failure(self) -> None:
        pr = _record(
            category=EnumPrCategory.RED,
            failed_check_names=("self-hosted runner", "Set up job"),
        )
        assert _block_reason_for_fix(pr) == EnumPrBlockReason.CI_FAILURE

    # -- negative / anti-over-broaden --------------------------------------

    def test_genuine_code_failure_stays_code_failure(self) -> None:
        """A real lint/type/test failure must NOT be rerun as flaky."""
        pr = _record(
            category=EnumPrCategory.RED,
            failed_check_names=("ruff / lint", "pytest / unit"),
        )
        assert _block_reason_for_fix(pr) == EnumPrBlockReason.CODE_FAILURE

    def test_mixed_flaky_and_code_failure_stays_code_failure(self) -> None:
        """One genuine code check among flaky ones blocks the cheap rerun."""
        pr = _record(
            category=EnumPrCategory.RED,
            failed_check_names=("self-hosted runner", "mypy / strict"),
        )
        assert _block_reason_for_fix(pr) == EnumPrBlockReason.CODE_FAILURE

    def test_mixed_flaky_evidence_and_code_failure_stays_code_failure(self) -> None:
        """Network evidence for one check must not hide a real code check."""
        pr = _record(
            category=EnumPrCategory.RED,
            failed_check_names=("Hostile Reviewer", "pytest / unit"),
            failed_check_flaky_evidence=("could not resolve host: github.com",),
        )
        assert _block_reason_for_fix(pr) == EnumPrBlockReason.CODE_FAILURE

    def test_receipt_only_without_ticket_stays_receipt_failure(self) -> None:
        """No ticket → autobind cannot run; keep the agent RECEIPT_FAILURE path."""
        pr = _record(
            category=EnumPrCategory.RED,
            failed_check_names=(_RECEIPT_GATE,),
            ticket_ids=(),
        )
        assert _block_reason_for_fix(pr) == EnumPrBlockReason.RECEIPT_FAILURE

    def test_genuine_occ_dependency_category_is_never_reclassified(self) -> None:
        """OCC_DEPENDENCY records (skipped by the reducer, never fed to fix) do
        not resolve to any of the newly activated arms — the classifier falls to
        the generic fallthrough, leaving genuine OCC dependencies untouched."""
        pr = _record(
            category=EnumPrCategory.OCC_DEPENDENCY,
            failed_check_names=(_RECEIPT_GATE,),
            ticket_ids=("OMN-9999",),
        )
        result = _block_reason_for_fix(pr)
        assert result not in {
            EnumPrBlockReason.DEPLOY_GATE_CONTRACT_NOT_FOUND,
            EnumPrBlockReason.CI_FAILURE,
        }
        # receipt-gate-only + ticket resolves to the autobind arm (a no-op-safe,
        # self-guarding adapter), never to a deploy-gate/flaky misroute.
        assert result == EnumPrBlockReason.RECEIPT_EVIDENCE_SOURCE_AUTOBIND

    def test_conflicted_still_routes_to_conflict(self) -> None:
        pr = _record(category=EnumPrCategory.CONFLICTED)
        assert _block_reason_for_fix(pr) == EnumPrBlockReason.CONFLICT

    def test_coderabbit_signature_still_routes_to_coderabbit(self) -> None:
        pr = _record(
            category=EnumPrCategory.RED,
            failed_check_names=("CodeRabbit",),
        )
        assert _block_reason_for_fix(pr) == EnumPrBlockReason.CODERABBIT


# ===========================================================================
# CP1 — the emitted enum actually reaches the correct fix-effect adapter
# ===========================================================================


@pytest.mark.unit
class TestChokepoint1AdapterRouting:
    async def test_ci_failure_reaches_github_rerun(self) -> None:
        handler, gh, _agent, occ, autobind = _fix_handler()
        await handler.handle(_fix_command(EnumPrBlockReason.CI_FAILURE))
        assert gh.rerun_calls == [("OmniNode-ai/omnimarket", 42)]
        assert occ.create_calls == []
        assert autobind.autobind_calls == []

    async def test_deploy_gate_not_found_reaches_occ_contract_create(self) -> None:
        handler, gh, _agent, occ, autobind = _fix_handler()
        # Empty changed_files → trivial-infra fast-path not eligible → create.
        await handler.handle(
            _fix_command(
                EnumPrBlockReason.DEPLOY_GATE_CONTRACT_NOT_FOUND,
                ticket_id="OMN-13776",
                changed_files=[],
            )
        )
        assert occ.create_calls == [("OmniNode-ai/omnimarket", 42, "OMN-13776")]
        assert autobind.autobind_calls == []
        assert gh.rerun_calls == []

    async def test_deploy_gate_trivial_infra_takes_fastpath_no_create(self) -> None:
        handler, _gh, _agent, occ, _autobind = _fix_handler()
        result = await handler.handle(
            _fix_command(
                EnumPrBlockReason.DEPLOY_GATE_CONTRACT_NOT_FOUND,
                ticket_id="OMN-13776",
                changed_files=["deploy/compose.yml"],
                diff_total_lines=2,
            )
        )
        assert "fast-path" in result.fix_action.lower()
        assert occ.create_calls == []

    async def test_autobind_reaches_occ_autobind_adapter(self) -> None:
        handler, gh, _agent, occ, autobind = _fix_handler()
        await handler.handle(
            _fix_command(
                EnumPrBlockReason.RECEIPT_EVIDENCE_SOURCE_AUTOBIND,
                ticket_id="OMN-13317",
            )
        )
        assert autobind.autobind_calls == [("OmniNode-ai/omnimarket", 42, "OMN-13317")]
        assert occ.create_calls == []
        assert gh.rerun_calls == []


# ===========================================================================
# CP2 — stall_category maps to a real EnumStallCategory and routes to the node
# ===========================================================================


@pytest.mark.unit
class TestChokepoint2StallCategory:
    def test_mapping_values_are_valid_enum_stall_categories(self) -> None:
        """Guard against drift: every mapped literal must be a real
        EnumStallCategory member (asserted here rather than imported at runtime,
        honoring the cross-node package boundary)."""
        valid = {
            EnumStallCategory.RED,
            EnumStallCategory.CONFLICTED,
            EnumStallCategory.BEHIND,
            EnumStallCategory.DEPLOY_GATE,
            EnumStallCategory.UNKNOWN,
            EnumStallCategory.STALE,
        }
        for value in _PR_CATEGORY_TO_STALL_CATEGORY.values():
            assert value in valid
        assert _stall_category_for_dispatch(EnumPrCategory.RED) == EnumStallCategory.RED
        assert (
            _stall_category_for_dispatch(EnumPrCategory.CONFLICTED)
            == EnumStallCategory.CONFLICTED
        )

    @pytest.mark.parametrize(
        "category",
        [
            EnumPrCategory.NEEDS_REVIEW,
            EnumPrCategory.OCC_DEPENDENCY,
            EnumPrCategory.UNKNOWN,
            EnumPrCategory.GREEN,
        ],
    )
    def test_unmapped_categories_return_unknown(self, category: EnumPrCategory) -> None:
        assert _stall_category_for_dispatch(category) == EnumStallCategory.UNKNOWN

    def test_red_stall_category_routes_dispatcher_to_ci_fix_effect(self) -> None:
        stall = _stall_category_for_dispatch(EnumPrCategory.RED)
        result = HandlerFixerDispatcher().handle(
            ModelFixerDispatchRequest(
                pr_number=42, repo="omnimarket", stall_category=stall
            )
        )
        assert result.target_node == "node_ci_fix_effect"
        assert result.action == "dispatch_ci_fix"

    def test_conflicted_stall_category_routes_to_conflict_hunk_effect(self) -> None:
        stall = _stall_category_for_dispatch(EnumPrCategory.CONFLICTED)
        result = HandlerFixerDispatcher().handle(
            ModelFixerDispatchRequest(
                pr_number=42, repo="omnimarket", stall_category=stall
            )
        )
        assert result.target_node == "node_conflict_hunk_effect"
        assert result.action == "dispatch_conflict_resolve"

    def test_prose_block_reason_would_have_escalated(self) -> None:
        """Regression witness for the pre-fix bug: the old prose value never
        matched the routing table and forced escalation."""
        result = HandlerFixerDispatcher().handle(
            ModelFixerDispatchRequest(
                pr_number=42,
                repo="omnimarket",
                stall_category="CI status is 'failing' — fix required before merge.",
            )
        )
        assert result.action == "escalate"
        assert result.target_node == ""
