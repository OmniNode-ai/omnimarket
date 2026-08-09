# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Cross-boundary test: seams.v1.yaml -> HandlerSeamMatch (OMN-15763 fix-forward).

Post-merge adversarial finding: ``scripts/seed_seams_registry.py``'s pin and
``handler_seam_match.py``'s ``check_stale_proof`` lived in incompatible hash
namespaces — the registry pinned a sha256 of a PROSE record
(``{edge_id, seam, classification, producer_shape, consumer_shape}``) while
the detector compares against ``canonical_sha256(ModelSeamProjection)``, a
structurally different byte string. Two individually-green unit suites (one
per node) never drove the actual seam between them — the exact anti-pattern
CLAUDE.md's "define and match seams" rule exists to catch.

This module is the real cross-boundary regression test: it drives the
literal seam between the registry/extractor side and the match-node side,
using genuine values from both, not synthetic doubles for either.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_seam_match_compute.handlers.handler_seam_match import (
    check_stale_proof,
)
from omnimarket.seams.canonical import canonical_sha256
from omnimarket.seams.extraction import extract_seam_graph
from omnimarket.seams.models.model_seam_projection import (
    EnumSeamDeliverySemantics,
    EnumSeamProjectionRole,
    ModelSeamProjection,
    ModelSeamProjectionField,
)

_REGISTRY_PATH = Path("src/omnimarket/configs/seams.v1.yaml")
_FIXTURE_REPO = Path("tests/nodes/node_seam_graph_compute/fixtures/repo_a")


def _load_registry_edges() -> list[dict[str, object]]:
    raw = yaml.safe_load(_REGISTRY_PATH.read_text(encoding="utf-8"))
    edges = raw["edges"]
    assert isinstance(edges, list)
    return edges


@pytest.mark.unit
class TestRegistryNamespaceIsUnambiguous:
    """Every checked-in row must use the renamed, namespace-explicit key —
    the bare ``pinned_hash`` name that caused the original confusion must
    never resurface."""

    def test_every_row_carries_source_record_pinned_hash(self) -> None:
        for edge in _load_registry_edges():
            assert isinstance(edge["source_record_pinned_hash"], str)
            assert len(edge["source_record_pinned_hash"]) == 64

    def test_every_row_carries_projection_pinned_hash_key_even_if_null(self) -> None:
        for edge in _load_registry_edges():
            assert "projection_pinned_hash" in edge

    def test_no_row_carries_the_ambiguous_bare_key(self) -> None:
        for edge in _load_registry_edges():
            assert "pinned_hash" not in edge


@pytest.mark.unit
class TestSourceRecordPinIsNotProjectionComparable:
    """Demonstrates the ORIGINAL bug directly: feeding a registry
    source-record pin into ``check_stale_proof`` as if it were a
    projection-namespace pin reports stale unconditionally, because the two
    hash spaces share nothing but sha256. This is now expected, documented
    behavior of a type-confused caller — not "hash pin current" — precisely
    because the fix keeps the two namespaces distinct rather than papering
    over the mismatch."""

    def test_feeding_source_record_pin_as_projection_pin_is_always_stale(
        self,
    ) -> None:
        edge = next(e for e in _load_registry_edges() if e["edge_id"] == "S1")
        # A real ModelSeamProjection for S1 — any content is legitimate here,
        # the point is that NO ModelSeamProjection's canonical hash will ever
        # equal a sha256 computed over the prose record shape.
        producer = ModelSeamProjection(
            edge_id="S1",
            role=EnumSeamProjectionRole.PRODUCER,
            topic="onex.cmd.omnibase-infra.delegation-request.v1",
            envelope_model="omnibase_core.models.wire.model_delegation_routing_input.ModelDelegationRoutingInput",
            envelope_version="1.0.0",
        )
        result = check_stale_proof(
            edge_id="S1",
            pinned_hash=str(edge["source_record_pinned_hash"]),
            current_producer=producer,
        )
        assert result.stale is True


@pytest.mark.unit
class TestGenuineProjectionPinRoundTripsAsCurrent:
    """The positive proof the finding demanded: build a REAL
    ``ModelSeamGraphEdgeDeclaration`` from the same extractor
    ``node_seam_graph_compute`` uses (not a hand-typed double), derive its
    ``ModelSeamProjection``, pin it with the canonical projection-namespace
    hash, and prove ``check_stale_proof`` reports "hash pin current" —
    the outcome the shipped detector could never reach when fed a
    source-record pin."""

    def _extracted_projection(self) -> ModelSeamProjection:
        graph = extract_seam_graph(str(_FIXTURE_REPO), ("svc_producer",))
        assert len(graph.edges) == 1
        edge = graph.edges[0]
        return ModelSeamProjection(
            edge_id=edge.edge_id,
            role=EnumSeamProjectionRole.PRODUCER,
            topic=edge.topic,
            envelope_model=edge.envelope_model,
            envelope_version=edge.envelope_version,
            key_fields=(ModelSeamProjectionField(name="tenant_id", field_type="str"),),
            delivery_semantics=EnumSeamDeliverySemantics.AT_LEAST_ONCE,
        )

    def test_projection_pin_current_hash_reports_not_stale(self) -> None:
        projection = self._extracted_projection()
        projection_pinned_hash = canonical_sha256(projection)

        result = check_stale_proof(
            edge_id=projection.edge_id,
            pinned_hash=projection_pinned_hash,
            current_producer=projection,
        )

        assert result.stale is False
        assert result.detail == "hash pin current"
        assert result.pinned_hash == result.current_hash

    def test_projection_pin_drifted_hash_reports_stale(self) -> None:
        projection = self._extracted_projection()
        stale_pin = canonical_sha256(
            projection.model_copy(update={"envelope_version": "0.0.1"})
        )

        result = check_stale_proof(
            edge_id=projection.edge_id,
            pinned_hash=stale_pin,
            current_producer=projection,
        )

        assert result.stale is True
        assert result.detail == "seam changed, proof stale"
