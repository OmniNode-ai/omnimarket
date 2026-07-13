# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Adversarial regression guard for the golden-chain census gate (OMN-14536).

The golden_chain_sweep required CI check was vacuous-green: CI ran the workflow
with ``projected_rows={}``, every chain timed out, the node correctly returned
``overall_status=fail`` — but ci.yml gated on the *workflow* result being
``completed``, so the required check passed anyway. 0 of 13 chains were ever
verified.

These tests are the prove-RED-against-EXISTS-but-WRONG evidence for the fix:

  * the census collector drives the reachable chains through their REAL
    projections and the gate goes GREEN only with a populated, passing census;
  * the exact vacuous scenario (Group-A chains, empty census) goes RED
    (NOT_COLLECTED) — a green over an absent census is impossible;
  * a drifted projection row (an expected field dropped) goes RED (FAIL);
  * the Group A / Group B split covers the whole registry with no chain silently
    dropped from the denominator, and the fixture set cannot diverge from the
    derived reachable set.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_golden_chain_sweep.census_collector import (
    CensusCollectionError,
    collect_census,
    run,
)
from omnimarket.nodes.node_golden_chain_sweep.handlers.handler_golden_chain_sweep import (
    EnumChainStatus,
    EnumSweepStatus,
    GoldenChainSweepRequest,
    NodeGoldenChainSweep,
)
from omnimarket.nodes.node_golden_chain_sweep.reachability import (
    derive_chain_reachability,
)
from omnimarket.nodes.node_golden_chain_sweep.registry import load_registry

_NOW = "2026-07-12T00:00:00+00:00"


@pytest.mark.unit
class TestCensusGateGreenWhenPopulated:
    """The gate is GREEN only with a real, populated, passing census."""

    def test_collector_drives_every_reachable_chain_to_a_row(self) -> None:
        projected_rows, census, _group_a = collect_census(now_iso=_NOW)

        reach = derive_chain_reachability(load_registry())
        # every reachable chain produced a real row via its real projection
        assert set(projected_rows) == set(reach.reachable)
        assert census.scanned_count == len(reach.reachable)
        assert census.scanned_count > 0
        # rows are non-empty dicts (real materialized rows, not placeholders)
        for name, row in projected_rows.items():
            assert isinstance(row, dict), f"chain {name} row is not a dict"
            assert row, f"chain {name} produced an empty row"

    def test_run_gate_is_green(self) -> None:
        out = run(now_iso=_NOW)
        hr = out["handler_result"]
        assert hr["overall_status"] == "pass"
        assert hr["scanned_count"] == len(out["reachable"])
        assert hr["scanned_count"] > 0
        assert hr["chains_failed"] == 0
        assert hr["chains_stale"] == 0


@pytest.mark.unit
class TestVacuousScenarioGoesRed:
    """The exact vacuous-green scenario now fails closed (RED)."""

    def test_group_a_chains_with_empty_census_is_not_collected(self) -> None:
        """Run the Group-A chains the OLD way — no census — and prove it goes RED.

        This is the scenario CI actually ran: real chains, ``projected_rows={}``,
        no census. Before OMN-14536 the workflow reported ``completed`` and CI
        passed. Now the node returns NOT_COLLECTED and the gate is RED."""
        _, _, group_a = collect_census(now_iso=_NOW)
        result = NodeGoldenChainSweep().handle(
            GoldenChainSweepRequest(chains=group_a, projected_rows={})
        )
        assert result.scanned_count == 0
        assert result.overall_status == EnumSweepStatus.NOT_COLLECTED
        assert result.status not in ("pass", "gated", "warn")  # CLI/gate exit non-zero

    def test_full_registry_with_empty_census_is_not_collected(self) -> None:
        """The precise pre-fix CI input: all 13 chains, empty census -> RED."""
        result = NodeGoldenChainSweep().handle(
            GoldenChainSweepRequest(chains=load_registry(), projected_rows={})
        )
        assert result.overall_status == EnumSweepStatus.NOT_COLLECTED
        assert result.overall_status != EnumSweepStatus.PASS


@pytest.mark.unit
class TestDriftedRowGoesRed:
    """A projection that drops an asserted field turns the gate RED (not green)."""

    def test_dropped_expected_field_fails_the_chain(self) -> None:
        projected_rows, census, group_a = collect_census(now_iso=_NOW)
        # simulate a projection regression: drop an expected field from one row
        target = next(c for c in group_a if c.expected_fields)
        drifted = dict(projected_rows)
        drifted[target.name] = {
            k: v
            for k, v in projected_rows[target.name].items()
            if k != target.expected_fields[0]
        }
        result = NodeGoldenChainSweep().handle(
            GoldenChainSweepRequest(
                chains=group_a,
                projected_rows=drifted,
                census=census,
                now_iso=_NOW,
            )
        )
        statuses = {r.name: r.status for r in result.chain_results}
        assert statuses[target.name] == EnumChainStatus.FAIL
        assert result.overall_status != EnumSweepStatus.PASS
        assert result.status not in ("pass", "gated", "warn")


@pytest.mark.unit
class TestDerivedSplitIntegrity:
    """The A/B split is derived, covers the registry, and cannot silently shrink."""

    def test_split_covers_whole_registry(self) -> None:
        chains = load_registry()
        reach = derive_chain_reachability(chains)
        covered = set(reach.reachable) | set(reach.routed)
        assert covered == {c.name for c in chains}
        # no chain is in both buckets
        assert not (set(reach.reachable) & set(reach.routed))

    def test_group_b_is_nonempty_and_routed(self) -> None:
        reach = derive_chain_reachability(load_registry())
        assert reach.routed, "expected cross-repo chains routed to omnibase_infra"
        assert "omnibase_infra" in reach.coverage_boundary
        # every routed chain has a derived reason
        for name in reach.routed:
            assert name in reach.reasons

    def test_fixture_set_cannot_diverge_from_derived_reachable(self) -> None:
        """If a fixture is added/removed without matching the derivation, RED."""
        import omnimarket.nodes.node_golden_chain_sweep.census_collector as cc

        reach = derive_chain_reachability(load_registry())
        original = cc._load_fixtures

        def _extra() -> dict[str, dict[str, object]]:
            base = dict(original())
            base["not_a_reachable_chain"] = {"correlation_id": "x"}
            return base

        cc._load_fixtures = _extra  # type: ignore[assignment]
        try:
            with pytest.raises(CensusCollectionError):
                collect_census(now_iso=_NOW)
        finally:
            cc._load_fixtures = original  # type: ignore[assignment]

        # sanity: the unmodified fixture set exactly matches the derived reachable
        # set (collect_census enforces this equality; here we assert it directly).
        assert set(original()) == set(reach.reachable)
