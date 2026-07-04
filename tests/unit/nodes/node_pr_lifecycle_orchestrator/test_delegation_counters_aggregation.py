# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Delegation-counter aggregation through _dispatch_fix_parallel (OMN-13940).

Proves ModelPrLifecycleFixResult.delegated/delegation_outcome maps correctly
into the FixResult aggregate's prs_delegated_fix_* counters -- the same
mapping ``_fix_one`` already does for prs_dispatched/prs_skipped.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from omnibase_core.protocols.event_bus.protocol_event_bus_publisher import (
    ProtocolEventBusPublisher,
)

from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_command import (
    EnumPrBlockReason,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_result import (
    EnumDelegationOutcome,
    ModelPrLifecycleFixResult,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
    HandlerPrLifecycleOrchestrator,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.protocols.protocol_sub_handlers import (
    EnumPrCategory,
    TriageRecord,
)


class _StubFix:
    """Returns a real ModelPrLifecycleFixResult per PR number, by lookup."""

    def __init__(self, results: dict[int, ModelPrLifecycleFixResult]) -> None:
        self._results = results
        self.calls: list[int] = []

    async def handle(self, command: object) -> ModelPrLifecycleFixResult:
        pr_number = command.pr_number  # type: ignore[attr-defined]
        self.calls.append(pr_number)
        return self._results[pr_number]


def _fix_result(
    pr_number: int,
    *,
    delegated: bool,
    outcome: EnumDelegationOutcome | None,
    fix_applied: bool = True,
) -> ModelPrLifecycleFixResult:
    return ModelPrLifecycleFixResult(
        correlation_id=uuid4(),
        pr_number=pr_number,
        repo="OmniNode-ai/omnimarket",
        block_reason=EnumPrBlockReason.CODE_FAILURE,
        fix_applied=fix_applied,
        fix_action="test",
        completed_at=datetime.now(tz=UTC),
        delegated=delegated,
        delegation_model="ruff-deterministic" if delegated else None,
        delegation_outcome=outcome,
        delegation_cost_usd=0.0 if delegated else None,
    )


def _triage_record(pr_number: int) -> TriageRecord:
    return TriageRecord(
        pr_number=pr_number,
        repo="OmniNode-ai/omnimarket",
        category=EnumPrCategory.RED,
        block_reason="code_failure",
    )


@pytest.mark.unit
class TestDelegationCountersAggregation:
    async def test_aggregates_across_outcomes(self) -> None:
        results = {
            1: _fix_result(1, delegated=True, outcome=EnumDelegationOutcome.ACCEPTED),
            2: _fix_result(
                2, delegated=True, outcome=EnumDelegationOutcome.GATE_FAILED
            ),
            3: _fix_result(3, delegated=True, outcome=EnumDelegationOutcome.ESCALATED),
            4: _fix_result(
                4, delegated=False, outcome=EnumDelegationOutcome.NOT_ATTEMPTED
            ),
        }
        stub_fix = _StubFix(results)
        orch = HandlerPrLifecycleOrchestrator(
            event_bus=MagicMock(spec=ProtocolEventBusPublisher)
        )
        orch._fix = stub_fix

        fix_results = await orch._dispatch_fix_parallel(
            fix_prs=tuple(_triage_record(n) for n in (1, 2, 3, 4)),
            correlation_id=uuid4(),
            dry_run=False,
            max_parallel=4,
            enable_admin_merge_fallback=False,
            admin_fallback_threshold_minutes=60,
        )

        assert len(stub_fix.calls) == 4
        attempted = sum(r.prs_delegated_fix_attempted for r in fix_results)
        accepted = sum(r.prs_delegated_fix_accepted for r in fix_results)
        gate_failed = sum(r.prs_delegated_fix_gate_failed for r in fix_results)
        escalated = sum(r.prs_delegated_fix_escalated for r in fix_results)
        assert attempted == 3
        assert accepted == 1
        assert gate_failed == 1
        assert escalated == 1

    async def test_magicmock_result_without_delegated_attr_never_counts(self) -> None:
        """A bare MagicMock (no explicit `delegated=True`) must never be
        counted as an attempted delegation -- guards against test-double
        auto-attribute truthiness leaking into the real counters."""

        class _MagicMockFix:
            async def handle(self, command: object) -> MagicMock:
                result = MagicMock()
                result.fix_applied = True
                result.pr_number = getattr(command, "pr_number", 0)
                return result

        orch = HandlerPrLifecycleOrchestrator(
            event_bus=MagicMock(spec=ProtocolEventBusPublisher)
        )
        orch._fix = _MagicMockFix()

        fix_results = await orch._dispatch_fix_parallel(
            fix_prs=(_triage_record(1),),
            correlation_id=uuid4(),
            dry_run=False,
            max_parallel=1,
            enable_admin_merge_fallback=False,
            admin_fallback_threshold_minutes=60,
        )

        assert sum(r.prs_delegated_fix_attempted for r in fix_results) == 0
