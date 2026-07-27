"""Tests for golden chain sweep idle-gate (OMN-8723, hardened OMN-14536).

Validates that idle_gate=True produces GATED (non-blocking) instead of TIMEOUT
when consumers are healthy but no events have flowed.

OMN-14536 — why these tests now pass an explicit ``census``: idle_gate's real
meaning is "the collector queried the tail surface and found no row, so the
consumer is idle". Before the census model existed there was no way to say that;
the tests expressed it as ``projected_rows={}``, which is byte-identical to "the
collector never ran". That ambiguity WAS the vacuous-green hatch — an entirely
uncollected census laundered into a non-blocking GATED with exit 0. The census
gives these tests the vocabulary to say "I looked and found nothing" (scanned>0,
GATED is legitimate) as distinct from "I never looked" (scanned==0 →
NOT_COLLECTED, blocking, asserted in TestUncollectedCensusCannotBeGated below).
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_golden_chain_sweep.handlers.handler_golden_chain_sweep import (
    EnumChainStatus,
    EnumSweepStatus,
    GoldenChainSweepRequest,
    ModelChainCensus,
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


def _census(scanned: int) -> ModelChainCensus:
    """A census proving the collector actually queried ``scanned`` tail surfaces."""
    return ModelChainCensus(source="postgres:test", scanned_count=scanned)


@pytest.mark.unit
class TestIdleGate:
    """idle_gate=True: missing rows → GATED, not TIMEOUT."""

    def test_missing_row_without_idle_gate_is_timeout(self) -> None:
        """Collector queried the table, found no row, idle_gate off → TIMEOUT/FAIL."""
        handler = NodeGoldenChainSweep()
        request = GoldenChainSweepRequest(
            chains=[_CHAIN_A],
            projected_rows={},
            census=_census(1),
            idle_gate=False,
        )
        result = handler.handle(request)

        assert result.chain_results[0].status == EnumChainStatus.TIMEOUT
        assert result.overall_status == EnumSweepStatus.FAIL

    def test_missing_row_with_idle_gate_is_gated(self) -> None:
        """Collector queried the table, found no row, idle_gate on → GATED."""
        handler = NodeGoldenChainSweep()
        request = GoldenChainSweepRequest(
            chains=[_CHAIN_A],
            projected_rows={},
            census=_census(1),
            idle_gate=True,
        )
        result = handler.handle(request)

        assert result.chain_results[0].status == EnumChainStatus.GATED
        assert result.overall_status == EnumSweepStatus.GATED
        assert result.chains_gated == 1
        assert result.chains_failed == 0

    def test_all_missing_idle_gate_overall_gated(self) -> None:
        """Both tables queried, both empty → GATED (a real observation of idleness)."""
        handler = NodeGoldenChainSweep()
        request = GoldenChainSweepRequest(
            chains=[_CHAIN_A, _CHAIN_B],
            projected_rows={},
            census=_census(2),
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
            census=_census(1),
            idle_gate=False,
        )
        result = handler.handle(request)

        assert result.chains_gated == 0


@pytest.mark.unit
class TestUncollectedCensusCannotBeGated:
    """The census invariant: idle_gate must not launder an uncollected census.

    THE HATCH THIS CLOSES (OMN-14536): before the census existed, ``idle_gate=True``
    over ``projected_rows={}`` returned ``overall_status=GATED`` — a *non-blocking*
    status that the CLI exits 0 on. So a sweep that observed **zero** tail surfaces
    reported "healthy, just idle". Flipping one boolean turned "I proved nothing"
    into a green-equivalent.

    These tests are the regression guard. They are RED against the EXISTS-but-WRONG
    state — the flag is set, the code path runs, and the old code returns GATED —
    not merely against the absence of a check.
    """

    def test_uncollected_census_with_idle_gate_is_not_collected_not_gated(self) -> None:
        """scanned_count==0 + idle_gate=True → NOT_COLLECTED. The hatch is shut."""
        handler = NodeGoldenChainSweep()
        request = GoldenChainSweepRequest(
            chains=[_CHAIN_A, _CHAIN_B],
            projected_rows={},
            idle_gate=True,  # the laundering flag, deliberately ON
        )
        result = handler.handle(request)

        assert result.scanned_count == 0
        # The precise regression: this used to be GATED (non-blocking, exit 0).
        assert result.overall_status == EnumSweepStatus.NOT_COLLECTED
        assert result.overall_status != EnumSweepStatus.GATED
        assert result.status not in ("pass", "gated", "warn")  # CLI exits non-zero

    def test_uncollected_census_without_idle_gate_is_not_collected(self) -> None:
        """scanned_count==0 is blocking regardless of idle_gate."""
        handler = NodeGoldenChainSweep()
        result = handler.handle(
            GoldenChainSweepRequest(chains=[_CHAIN_A], projected_rows={})
        )

        assert result.scanned_count == 0
        assert result.overall_status == EnumSweepStatus.NOT_COLLECTED

    def test_explicit_zero_scan_census_is_not_collected(self) -> None:
        """A collector that reports it queried nothing is fail-closed too.

        Covers the harness-ran-but-collected-nothing case (DB unreachable, table
        list empty) — distinct from "no census supplied at all", same verdict.
        """
        handler = NodeGoldenChainSweep()
        request = GoldenChainSweepRequest(
            chains=[_CHAIN_A],
            projected_rows={},
            census=ModelChainCensus(source="postgres:unreachable", scanned_count=0),
            idle_gate=True,
        )
        result = handler.handle(request)

        assert result.overall_status == EnumSweepStatus.NOT_COLLECTED
        assert result.census_source == "postgres:unreachable"

    def test_unreachable_chain_is_blocking_not_skipped(self) -> None:
        """A chain whose tail surface could not be queried is UNREACHABLE, not skipped.

        'I could not look at this chain' must never be silently dropped from the
        denominator — that is how a shrinking census reads green.
        """
        handler = NodeGoldenChainSweep()
        request = GoldenChainSweepRequest(
            chains=[_CHAIN_A, _CHAIN_B],
            projected_rows={"delegation": {"correlation_id": "abc"}},
            census=ModelChainCensus(
                source="postgres:test", scanned_count=1, unreachable=["routing"]
            ),
            idle_gate=True,  # must NOT rescue an unreachable chain
        )
        result = handler.handle(request)

        statuses = {r.name: r.status for r in result.chain_results}
        assert statuses["routing"] == EnumChainStatus.UNREACHABLE
        assert result.chains_unreachable == 1
        # delegation passed, routing unreachable → PARTIAL (blocking), never PASS/GATED
        assert result.overall_status == EnumSweepStatus.PARTIAL
        assert result.overall_status != EnumSweepStatus.PASS

    def test_caller_supplied_rows_count_as_scanned(self) -> None:
        """Rows supplied with no census (unit/fixture callers) are a non-zero scan.

        Guards the invariant against over-reach: it must fail the *uncollected*
        census, not every census-less caller.
        """
        handler = NodeGoldenChainSweep()
        result = handler.handle(
            GoldenChainSweepRequest(
                chains=[_CHAIN_A],
                projected_rows={"delegation": {"correlation_id": "abc"}},
            )
        )

        assert result.scanned_count == 1
        assert result.overall_status == EnumSweepStatus.PASS
