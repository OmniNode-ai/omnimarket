# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Registry-binding guard for the OMN-16004 frozen slice.

A frozen slice enumeration that nobody checks is folklore. This module is what
makes ``slice_manifest.yaml`` load-bearing:

* every manifest edge must exist in ``seams.v1.yaml`` (no invented edge ids),
* the manifest's recorded classification/severity must equal the registry's
  (the slice cannot describe a seam differently than the registry of record),
* the registry must be the 2026-08-13 re-derivation, not the superseded
  2026-08-08 source that reproduces the staleness defect this work exists to
  guard against,
* every included edge's declared golden module must exist on disk, and
* the slice must account for all 15 registry edges — an edge may be excluded,
  but only explicitly and with a stated reason, never by omission.

The last point is the scope bound made structural: the full 15-edge golden
program is a separate blocked ticket, and this file proves this PR does not
silently claim it.
"""

from __future__ import annotations

import pytest

from tests.seam_goldens.harness import registry_edge, registry_edge_ids, registry_header
from tests.seam_goldens.manifest import (
    EnumSeamObservationClass,
    EnumSliceInclusion,
    ModelSliceEdge,
    load_slice_manifest,
)

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def manifest() -> object:
    return load_slice_manifest()


def _slice_edges() -> tuple[ModelSliceEdge, ...]:
    return load_slice_manifest().edges


def _included_edges() -> tuple[ModelSliceEdge, ...]:
    return load_slice_manifest().included()


class TestRegistryProvenance:
    """The registry of record must be the 2026-08-13 re-derivation."""

    def test_registry_schema_version_matches_manifest(self) -> None:
        binding = load_slice_manifest().registry
        assert registry_header()["schema_version"] == binding.schema_version

    def test_registry_is_bound_to_the_2026_08_13_source(self) -> None:
        """Fail closed on a registry regenerated from the superseded source.

        The 2026-08-08 seam-graph source was superseded; a registry still
        pointing at it is invalid regardless of how green its rows look,
        because the shapes it pinned describe a topology that no longer holds.
        """

        binding = load_slice_manifest().registry
        generated_from = registry_header()["generated_from"]
        assert isinstance(generated_from, dict)
        assert generated_from["source_path"] == binding.source_path
        assert generated_from["generated_at"] == binding.generated_at
        assert "2026-08-08" not in str(generated_from["source_path"])

    def test_registry_edge_total_matches_manifest(self) -> None:
        binding = load_slice_manifest().registry
        summary = registry_header()["summary"]
        assert isinstance(summary, dict)
        assert summary["edges_total"] == binding.edges_total
        assert len(registry_edge_ids()) == binding.edges_total


class TestSliceAccountsForEveryRegistryEdge:
    """No registry edge may be dropped by silence."""

    def test_manifest_covers_exactly_the_registry_edge_set(self) -> None:
        manifest_ids = {edge.edge_id for edge in _slice_edges()}
        assert manifest_ids == set(registry_edge_ids())

    def test_every_excluded_edge_states_a_reason(self) -> None:
        excluded = load_slice_manifest().excluded()
        assert excluded, "a slice that excludes nothing has not been scoped"
        for edge in excluded:
            assert edge.exclusion_reason
            assert edge.golden_module is None

    def test_full_program_is_not_claimed_by_this_slice(self) -> None:
        """This PR is the narrow pre-activation slice, not the 15-edge program.

        Asserted structurally rather than in prose: if a future edit quietly
        goldens every registry edge, this test fails and forces the scope
        question to be answered deliberately.
        """

        manifest = load_slice_manifest()
        assert len(manifest.included()) < manifest.registry.edges_total


@pytest.mark.parametrize(
    "edge",
    _slice_edges(),
    ids=lambda edge: edge.edge_id,
)
class TestEveryManifestEdgeBindsToTheRegistry:
    """Per-edge binding — parametrized so a drifted row names itself."""

    def test_edge_exists_in_registry(self, edge: ModelSliceEdge) -> None:
        assert registry_edge(edge.edge_id)["edge_id"] == edge.edge_id

    def test_classification_agrees_with_registry(self, edge: ModelSliceEdge) -> None:
        row = registry_edge(edge.edge_id)
        assert row["classification"] == edge.registry_classification

    def test_severity_agrees_with_registry(self, edge: ModelSliceEdge) -> None:
        row = registry_edge(edge.edge_id)
        assert row["severity"] == edge.registry_severity


@pytest.mark.parametrize(
    "edge",
    _included_edges(),
    ids=lambda edge: edge.edge_id,
)
class TestEveryIncludedEdgeHasAnExecutableGolden:
    """ "Enumerated" and "goldened" must not be able to drift apart."""

    def test_declared_golden_module_exists(self, edge: ModelSliceEdge) -> None:
        module = edge.resolved_golden_module
        assert module is not None
        assert module.is_file(), f"{edge.edge_id}: missing golden {edge.golden_module}"


class TestWs7MandatoryUnionIsComplete:
    """Every high-severity registry edge must be in slice (WS-7 union rule)."""

    def test_all_high_severity_edges_are_included(self) -> None:
        high_severity = {
            edge_id
            for edge_id in registry_edge_ids()
            if registry_edge(edge_id)["severity"] == "high"
        }
        included = {edge.edge_id for edge in _included_edges()}
        missing = high_severity - included
        assert not missing, (
            f"WS-7 mandates every high-severity edge in the union; missing: "
            f"{sorted(missing)}"
        )

    def test_no_registry_edge_is_rated_critical(self) -> None:
        """The union rule is high-or-critical; pin that no critical row exists.

        If a future regeneration introduces a critical edge, this fails and
        forces a deliberate re-scope rather than letting the union rule silently
        under-collect.
        """

        severities = {
            str(registry_edge(edge_id)["severity"]) for edge_id in registry_edge_ids()
        }
        assert "critical" not in severities

    def test_high_severity_edges_are_marked_ws7_mandatory(self) -> None:
        for edge in _included_edges():
            if edge.registry_severity == "high":
                assert edge.inclusion is EnumSliceInclusion.WS7_MANDATORY_HIGH


class TestRegistryRegenerableFlagIsMeasuredStale:
    """Bind the goldens' measured regenerability to the registry's own flag.

    The registry of record carries a per-edge ``regenerable`` boolean and a
    ``summary.regenerable_count``. Every row records ``false`` and the count is
    ``0`` — which was true when the registry was derived, because no executable
    proof existed for any edge yet.

    That is no longer true for four edges, and the divergence has to be
    load-bearing somewhere or the goldens' REGENERABLE verdict binds to
    nothing at all. (In the first cut it bound to nothing twice over: the
    verdict itself was tautological AND it was never compared against this
    flag.) Re-deriving ``seams.v1.yaml`` is a generator run against
    ``docs/design/2026-08-13-delegation-seam-graph.json``, not a hand-edit of a
    generated file, so it is out of scope for a test-only change and is carried
    as receipt evidence instead. The divergence is pinned here so it stays
    visible, and so the re-derivation FAILS these tests — which is the correct
    signal to delete the pin.
    """

    def test_registry_still_records_zero_regenerable_edges(self) -> None:
        summary = registry_header()["summary"]
        assert isinstance(summary, dict)
        assert summary["regenerable_count"] == 0
        for edge_id in registry_edge_ids():
            assert registry_edge(edge_id)["regenerable"] is False, edge_id

    def test_the_slice_measures_regenerable_edges_the_registry_denies(self) -> None:
        """The measured set the next registry re-derivation must account for.

        S6 was in this set until 2026-08-14 and is not any more. That is not a
        downgrade to stay green: under the seam graph's ``tracing_convention``
        A (OMN-16033) the edge re-scored ``UNMATCHED`` at the resolved
        omnibase_infra rev, and an UNMATCHED edge has no second side to
        observe, so ``NOT_CLAIMED`` is the only honest class for it. The
        entitlement follows the measurement; it was not chosen to make a test
        pass.
        """

        measured = {
            edge.edge_id
            for edge in load_slice_manifest().by_observation_class(
                EnumSeamObservationClass.REGENERABLE
            )
        }
        assert measured == {"S10", "S11", "S12", "S13"}, (
            "the set of edges with a genuine two-sided observation changed; "
            "re-derive seams.v1.yaml and update this pin deliberately"
        )
        for edge_id in measured:
            assert registry_edge(edge_id)["regenerable"] is False, (
                f"{edge_id}: seams.v1.yaml now records regenerable=true, so the "
                f"registry has been re-derived — delete this divergence pin"
            )

    def test_no_edge_claims_regenerable_against_an_unreachable_side(self) -> None:
        """The invariant the first cut violated, asserted at the slice level."""

        for edge in load_slice_manifest().included():
            if edge.observation_class is EnumSeamObservationClass.REGENERABLE:
                assert edge.producer_symbol_reachable, edge.edge_id
                assert edge.consumer_symbol_reachable, edge.edge_id


class TestTraversalClaimsAreHonest:
    """A `traversed: true` claim is an assertion about the receipt's path."""

    def test_core_round_trip_edges_are_traversed(self) -> None:
        for edge in _included_edges():
            if edge.inclusion is EnumSliceInclusion.CORE_ROUND_TRIP:
                assert edge.traversed, f"{edge.edge_id}: core leg must be traversed"

    def test_flagged_ambiguous_edges_do_not_claim_traversal(self) -> None:
        """Ambiguity is recorded as ambiguity, never upgraded to a claim."""

        for edge in _slice_edges():
            if edge.inclusion is EnumSliceInclusion.FLAGGED_AMBIGUOUS:
                assert not edge.traversed

    def test_both_directions_of_the_round_trip_are_present(self) -> None:
        """The receipt binds a round trip, so a one-directional slice is wrong."""

        legs = {
            edge.leg
            for edge in _included_edges()
            if edge.inclusion is EnumSliceInclusion.CORE_ROUND_TRIP
        }
        assert any("cloud->local" in leg for leg in legs)
        assert any("local->cloud" in leg for leg in legs)
