"""Tests for golden chain sweep idle-gate (OMN-8723).

Validates that idle_gate=True produces GATED (non-blocking) instead of TIMEOUT
when consumers are healthy but no events have flowed.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_golden_chain_sweep.handlers.handler_golden_chain_sweep import (
    EnumChainStatus,
    EnumSweepStatus,
    GoldenChainSweepRequest,
    ModelChainDefinition,
    NodeGoldenChainSweep,
)

_CHAIN_A = ModelChainDefinition(
    name="delegation",
    head_topic="onex.evt.omniclaude.task-delegated.v1",
    tail_table="delegation_events",
    expected_fields=["correlation_id"],
)
_CHAIN_B = ModelChainDefinition(
    name="routing",
    head_topic="onex.evt.omniclaude.llm-routing-decision.v1",
    tail_table="llm_routing_decisions",
    expected_fields=["correlation_id"],
)


@pytest.mark.unit
class TestIdleGate:
    """idle_gate=True: missing rows → GATED, not TIMEOUT."""

    def test_missing_row_without_idle_gate_is_timeout(self) -> None:
        handler = NodeGoldenChainSweep()
        request = GoldenChainSweepRequest(
            chains=[_CHAIN_A],
            projected_rows={},
            idle_gate=False,
        )
        result = handler.handle(request)

        assert result.chain_results[0].status == EnumChainStatus.TIMEOUT
        assert result.overall_status == EnumSweepStatus.FAIL

    def test_missing_row_with_idle_gate_is_gated(self) -> None:
        handler = NodeGoldenChainSweep()
        request = GoldenChainSweepRequest(
            chains=[_CHAIN_A],
            projected_rows={},
            idle_gate=True,
        )
        result = handler.handle(request)

        assert result.chain_results[0].status == EnumChainStatus.GATED
        assert result.overall_status == EnumSweepStatus.GATED
        assert result.chains_gated == 1
        assert result.chains_failed == 0

    def test_all_missing_idle_gate_overall_gated(self) -> None:
        handler = NodeGoldenChainSweep()
        request = GoldenChainSweepRequest(
            chains=[_CHAIN_A, _CHAIN_B],
            projected_rows={},
            idle_gate=True,
        )
        result = handler.handle(request)

        assert result.overall_status == EnumSweepStatus.GATED
        assert result.chains_gated == 2
        assert result.chains_failed == 0
        assert result.chains_passed == 0

    def test_mixed_pass_and_gated_is_pass_not_partial(self) -> None:
        """A passing chain + a gated chain is still non-blocking (PASS, not PARTIAL)."""
        handler = NodeGoldenChainSweep()
        request = GoldenChainSweepRequest(
            chains=[_CHAIN_A, _CHAIN_B],
            projected_rows={"delegation": {"correlation_id": "abc"}},
            idle_gate=True,
        )
        result = handler.handle(request)

        assert result.chains_passed == 1
        assert result.chains_gated == 1
        assert result.chains_failed == 0
        # All non-PASS chains are GATED (non-blocking), so overall is GATED
        assert result.overall_status == EnumSweepStatus.GATED

    def test_real_failure_with_idle_gate_still_fails(self) -> None:
        """FAIL (missing expected fields) is always RED regardless of idle_gate."""
        handler = NodeGoldenChainSweep()
        request = GoldenChainSweepRequest(
            chains=[_CHAIN_A],
            projected_rows={"delegation": {"wrong_field": "x"}},
            idle_gate=True,
        )
        result = handler.handle(request)

        assert result.chain_results[0].status == EnumChainStatus.FAIL
        assert result.overall_status == EnumSweepStatus.FAIL
        assert result.chains_failed == 1

    def test_mix_gated_and_failed_is_partial(self) -> None:
        """GATED + FAIL chains → PARTIAL (has both blocking and non-blocking failures)."""
        handler = NodeGoldenChainSweep()
        request = GoldenChainSweepRequest(
            chains=[_CHAIN_A, _CHAIN_B],
            projected_rows={"routing": {"wrong_field": "x"}},
            idle_gate=True,
        )
        result = handler.handle(request)

        chain_statuses = {r.name: r.status for r in result.chain_results}
        assert chain_statuses["delegation"] == EnumChainStatus.GATED
        assert chain_statuses["routing"] == EnumChainStatus.FAIL
        assert result.overall_status == EnumSweepStatus.PARTIAL
        assert result.chains_gated == 1
        assert result.chains_failed == 1

    def test_gated_message_indicates_idle(self) -> None:
        """GATED chain message should indicate idle/non-blocking context."""
        handler = NodeGoldenChainSweep()
        request = GoldenChainSweepRequest(
            chains=[_CHAIN_A],
            projected_rows={},
            idle_gate=True,
        )
        result = handler.handle(request)

        msg = result.chain_results[0].message
        assert "idle" in msg.lower() or "non-blocking" in msg.lower()

    def test_chains_gated_zero_without_idle_gate(self) -> None:
        """chains_gated counter stays 0 when idle_gate=False."""
        handler = NodeGoldenChainSweep()
        request = GoldenChainSweepRequest(
            chains=[_CHAIN_A],
            projected_rows={},
            idle_gate=False,
        )
        result = handler.handle(request)

        assert result.chains_gated == 0
