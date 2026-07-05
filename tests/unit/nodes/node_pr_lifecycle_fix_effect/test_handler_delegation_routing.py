# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerPrLifecycleFix delegation-routing safety bar (WS-D/D2, OMN-13940).

Four required scenarios per the ticket:
  (a) an eligible fix is delegated and accepted -- the agent is never called.
  (b) a denylisted path is refused -- delegation is never attempted, the
      agent handles it directly.
  (c) two-strike escalates -- the second delegation failure on the same
      PR/block_reason permanently disables delegation for that key.
  (d) a gate failure escalates rather than force-pushes -- the failure is
      surfaced, not silently treated as success, and (on the first strike)
      no agent fallback happens either -- the PR is left for retry/escalation
      rather than force-pushed.

Also locks in safety bar #5: RECEIPT_FAILURE never reaches the delegation
path, even when it would otherwise look eligible.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.handler_pr_lifecycle_fix import (
    HandlerPrLifecycleFix,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_command import (
    EnumPrBlockReason,
    ModelPrLifecycleFixCommand,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_result import (
    EnumDelegationOutcome,
)


class _MockAgentDispatchAdapter:
    def __init__(self) -> None:
        self.review_fix_calls: list[tuple[str, int, str | None]] = []

    async def dispatch_review_fix(
        self, repo: str, pr_number: int, ticket_id: str | None
    ) -> str:
        self.review_fix_calls.append((repo, pr_number, ticket_id))
        return f"[mock] dispatched review-fix on {repo}#{pr_number}"

    async def dispatch_coderabbit_reply(self, repo: str, pr_number: int) -> str:
        raise AssertionError("not used in these tests")


class _MockDelegationFixAdapter:
    """Configurable delegated-fix adapter: always succeeds, or raises N times."""

    def __init__(self, *, fail_times: int = 0) -> None:
        self.calls: list[tuple[str, int, str | None]] = []
        self._fail_times = fail_times

    async def dispatch_delegated_fix(
        self,
        repo: str,
        pr_number: int,
        ticket_id: str | None,
        command: ModelPrLifecycleFixCommand,
    ) -> str:
        self.calls.append((repo, pr_number, ticket_id))
        if len(self.calls) <= self._fail_times:
            raise RuntimeError("pr_polish gate/verify failed: precommit hook failure")
        return f"delegated fix accepted on {repo}#{pr_number}"


class _InMemoryTwoStrikeStore:
    def __init__(self) -> None:
        self._counts: dict[str, int] = {}

    def get_strikes(self, key: str) -> int:
        return self._counts.get(key, 0)

    def record_failure(self, key: str) -> int:
        self._counts[key] = self._counts.get(key, 0) + 1
        return self._counts[key]


def _make_command(
    *,
    block_reason: EnumPrBlockReason = EnumPrBlockReason.CODE_FAILURE,
    pr_number: int = 500,
    repo: str = "OmniNode-ai/omnimarket",
    ticket_id: str | None = "OMN-13940",
    changed_files: list[str] | None = None,
    diff_total_lines: int = 8,
    review_context_text: str = "",
) -> ModelPrLifecycleFixCommand:
    return ModelPrLifecycleFixCommand(
        correlation_id=uuid4(),
        pr_number=pr_number,
        repo=repo,
        block_reason=block_reason,
        ticket_id=ticket_id,
        changed_files=changed_files if changed_files is not None else ["src/foo.py"],
        diff_total_lines=diff_total_lines,
        review_context_text=review_context_text,
        requested_at=datetime.now(tz=UTC),
    )


@pytest.mark.unit
class TestDelegationRoutingSafetyBar:
    async def test_a_eligible_fix_delegated_and_accepted_agent_never_called(
        self,
    ) -> None:
        """(a) Eligible ruff-fixable failure is auto-fixed via delegation; the
        agent is never invoked, and the result is only accepted because the
        (mocked) gates+verify path succeeded."""
        agent = _MockAgentDispatchAdapter()
        delegation = _MockDelegationFixAdapter(fail_times=0)
        handler = HandlerPrLifecycleFix(
            agent_dispatch_adapter=agent,
            delegation_fix_adapter=delegation,
            two_strike_store=_InMemoryTwoStrikeStore(),
        )
        command = _make_command()

        result = await handler.handle(command)

        assert result.fix_applied is True
        assert result.delegated is True
        assert result.delegation_outcome == EnumDelegationOutcome.ACCEPTED
        assert result.delegation_model == "ruff-deterministic"
        assert result.delegation_cost_usd == 0.0
        assert delegation.calls == [("OmniNode-ai/omnimarket", 500, "OMN-13940")]
        assert agent.review_fix_calls == [], "accepted delegation must not call agent"

    async def test_b_denylisted_path_refused_agent_handles_directly(self) -> None:
        """(b) A denylisted changed_files path is refused before any delegation
        attempt -- the agent handles the fix directly."""
        agent = _MockAgentDispatchAdapter()
        delegation = _MockDelegationFixAdapter(fail_times=0)
        handler = HandlerPrLifecycleFix(
            agent_dispatch_adapter=agent,
            delegation_fix_adapter=delegation,
            two_strike_store=_InMemoryTwoStrikeStore(),
        )
        command = _make_command(
            changed_files=["onex_change_control/contracts/OMN-1.yaml"]
        )

        result = await handler.handle(command)

        assert result.fix_applied is True
        assert result.delegated is False
        assert result.delegation_outcome == EnumDelegationOutcome.NOT_ATTEMPTED
        assert result.delegation_model is None
        assert delegation.calls == [], "denylisted path must never reach delegation"
        assert agent.review_fix_calls == [("OmniNode-ai/omnimarket", 500, "OMN-13940")]

    async def test_b_content_keyword_denylist_refused(self) -> None:
        """(b) variant: a security-keyword hit in review_context_text refuses
        delegation even with an otherwise-eligible small diff."""
        agent = _MockAgentDispatchAdapter()
        delegation = _MockDelegationFixAdapter(fail_times=0)
        handler = HandlerPrLifecycleFix(
            agent_dispatch_adapter=agent,
            delegation_fix_adapter=delegation,
            two_strike_store=_InMemoryTwoStrikeStore(),
        )
        command = _make_command(
            review_context_text="CodeRabbit: this touches the auth middleware"
        )

        result = await handler.handle(command)

        assert result.delegated is False
        assert delegation.calls == []
        assert agent.review_fix_calls == [("OmniNode-ai/omnimarket", 500, "OMN-13940")]

    async def test_c_two_strike_permanently_escalates(self) -> None:
        """(c) The second delegation failure for the same PR/block_reason
        permanently disables delegation for that key -- the third call never
        even attempts delegation again."""
        agent = _MockAgentDispatchAdapter()
        delegation = _MockDelegationFixAdapter(fail_times=2)
        store = _InMemoryTwoStrikeStore()
        handler = HandlerPrLifecycleFix(
            agent_dispatch_adapter=agent,
            delegation_fix_adapter=delegation,
            two_strike_store=store,
        )
        command = _make_command()

        result_1 = await handler.handle(command)
        assert result_1.delegation_outcome == EnumDelegationOutcome.GATE_FAILED
        assert agent.review_fix_calls == [], "first strike must not escalate to agent"

        result_2 = await handler.handle(command)
        assert result_2.delegation_outcome == EnumDelegationOutcome.ESCALATED
        assert len(agent.review_fix_calls) == 1, (
            "second strike (permanent escalation) must dispatch the agent"
        )

        result_3 = await handler.handle(command)
        assert result_3.delegation_outcome == EnumDelegationOutcome.NOT_ATTEMPTED
        assert result_3.delegated is False
        assert len(delegation.calls) == 2, (
            "a PR/block_reason that tripped two-strike must never attempt "
            "delegation again"
        )
        assert len(agent.review_fix_calls) == 2

    async def test_d_gate_failure_escalates_rather_than_force_pushes(self) -> None:
        """(d) A gate/verify failure inside the delegated fix never reports
        success and never force-pushes -- on the first strike it is not even
        handed to the agent yet (retry/escalate next tick), so nothing pushed
        this call."""
        agent = _MockAgentDispatchAdapter()
        delegation = _MockDelegationFixAdapter(fail_times=1)
        handler = HandlerPrLifecycleFix(
            agent_dispatch_adapter=agent,
            delegation_fix_adapter=delegation,
            two_strike_store=_InMemoryTwoStrikeStore(),
        )
        command = _make_command()

        result = await handler.handle(command)

        assert result.delegation_outcome == EnumDelegationOutcome.GATE_FAILED
        assert result.delegated is True
        assert "failed" in result.fix_action
        assert "pushed" not in result.fix_action
        assert agent.review_fix_calls == [], (
            "a gate failure must not force a push nor silently succeed; on "
            "the first strike it must not yet fall back to the agent either"
        )

    async def test_receipt_failure_never_reaches_delegation(self) -> None:
        """Safety bar #5: RECEIPT_FAILURE is split out and never delegated,
        even with an eligible-looking small diff."""
        agent = _MockAgentDispatchAdapter()
        delegation = _MockDelegationFixAdapter(fail_times=0)
        handler = HandlerPrLifecycleFix(
            agent_dispatch_adapter=agent,
            delegation_fix_adapter=delegation,
            two_strike_store=_InMemoryTwoStrikeStore(),
        )
        command = _make_command(block_reason=EnumPrBlockReason.RECEIPT_FAILURE)

        result = await handler.handle(command)

        assert result.delegated is False
        assert result.delegation_outcome is None
        assert delegation.calls == [], "RECEIPT_FAILURE must never be delegated"
        assert agent.review_fix_calls == [("OmniNode-ai/omnimarket", 500, "OMN-13940")]

    async def test_empty_changed_files_never_eligible(self) -> None:
        """Unknown blast radius (empty changed_files) is always ineligible."""
        agent = _MockAgentDispatchAdapter()
        delegation = _MockDelegationFixAdapter(fail_times=0)
        handler = HandlerPrLifecycleFix(
            agent_dispatch_adapter=agent,
            delegation_fix_adapter=delegation,
            two_strike_store=_InMemoryTwoStrikeStore(),
        )
        command = _make_command(changed_files=[])

        result = await handler.handle(command)

        assert result.delegated is False
        assert delegation.calls == []

    async def test_oversized_diff_never_eligible(self) -> None:
        """Blast radius above the size gate refuses delegation."""
        agent = _MockAgentDispatchAdapter()
        delegation = _MockDelegationFixAdapter(fail_times=0)
        handler = HandlerPrLifecycleFix(
            agent_dispatch_adapter=agent,
            delegation_fix_adapter=delegation,
            two_strike_store=_InMemoryTwoStrikeStore(),
        )
        command = _make_command(diff_total_lines=500)

        result = await handler.handle(command)

        assert result.delegated is False
        assert delegation.calls == []
