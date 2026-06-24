# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the vacuous-green fix (OMN-13553).

The canonical skill-dispatch path ``onex skill golden_chain_sweep`` dispatches
``node_golden_chain_sweep`` with a minimal payload (only ``correlation_id``).
Before this fix the request defaulted ``chains=[]`` and the reducer computed
``overall_status=pass`` over the empty set — a vacuous green that told an
operator/CI gate "all golden chains healthy" while validating zero chains.

Two guarantees are asserted here:

1. The skill-dispatch construction (``GoldenChainSweepRequest()`` with no
   ``chains``) loads >0 chains from the packaged registry — ``chains_total > 0``.
2. An empty ``chains`` set (explicit, or an empty/unreachable registry) is
   fail-closed: it MUST NOT report ``overall_status=pass``.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_golden_chain_sweep.handlers.handler_golden_chain_sweep import (
    EnumSweepStatus,
    GoldenChainSweepRequest,
    NodeGoldenChainSweep,
)


@pytest.mark.unit
class TestVacuousGreenFailClosed:
    """An empty chain set must never report pass (OMN-13553 DoD §2)."""

    def test_explicit_empty_chains_is_not_pass(self) -> None:
        """Explicit ``chains=[]`` fails closed — never a vacuous pass."""
        request = GoldenChainSweepRequest(chains=[], projected_rows={})
        result = NodeGoldenChainSweep().handle(request)

        assert result.chains_total == 0
        assert result.overall_status != EnumSweepStatus.PASS
        assert result.overall_status == EnumSweepStatus.FAIL
        assert result.status != "pass"

    def test_empty_chains_with_idle_gate_is_not_pass(self) -> None:
        """idle_gate does NOT launder an empty chain set into pass/gated."""
        request = GoldenChainSweepRequest(chains=[], projected_rows={}, idle_gate=True)
        result = NodeGoldenChainSweep().handle(request)

        assert result.chains_total == 0
        assert result.overall_status == EnumSweepStatus.FAIL


@pytest.mark.unit
class TestSkillDispatchLoadsChains:
    """The skill-dispatch path loads >0 chains (OMN-13553 DoD §1 + §3)."""

    def test_default_construction_loads_registry_chains(self) -> None:
        """``GoldenChainSweepRequest()`` (the runtime ``cls()`` path) is non-empty."""
        request = GoldenChainSweepRequest()
        assert len(request.chains) > 0

    def test_skill_dispatch_with_no_rows_is_not_vacuous_pass(self) -> None:
        """The exact skill-dispatch payload (correlation_id only, no rows) must
        (a) validate >0 chains and (b) NOT return a vacuous pass."""
        # Mirrors what RuntimeLocal builds for `onex skill golden_chain_sweep`:
        # a default-constructed request whose only caller-supplied field is the
        # runtime-injected correlation_id; no projected_rows are fetched.
        request = GoldenChainSweepRequest(correlation_id="skill-dispatch-test")
        result = NodeGoldenChainSweep().handle(request)

        # (a) >0 chains loaded — not the old chains_total:0
        assert result.chains_total > 0
        # (b) with no projected rows, every chain is TIMEOUT (no idle_gate),
        # so the sweep is FAIL — never a vacuous pass.
        assert result.overall_status != EnumSweepStatus.PASS
        assert result.chains_passed == 0

    def test_skill_dispatch_idle_gate_no_rows_is_gated_not_pass(self) -> None:
        """With idle_gate, no-rows chains are GATED (non-blocking) — still not a
        vacuous PASS over zero chains, because chains_total > 0."""
        request = GoldenChainSweepRequest(
            correlation_id="skill-dispatch-test", idle_gate=True
        )
        result = NodeGoldenChainSweep().handle(request)

        assert result.chains_total > 0
        assert result.chains_gated == result.chains_total
        assert result.overall_status == EnumSweepStatus.GATED
