# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Def-B flip proof for node_similarity_compute (OMN-14840, Class-B Tier-1).

Tests-as-proof for the canonical-shape hand-flip (parent epic OMN-14355):

* RED marker: before the flip, ``HandlerSimilarityCompute`` exposed no callable
  ``handle`` dispatch entrypoint, so ``test_handle_entrypoint_is_callable`` FAILS
  on the pre-flip tree (``AttributeError`` / missing attribute). The shared
  runtime's ``_make_dispatch_callback`` binds ``handle``; a handler exposing none
  is bound to ``_missing_handle`` and raises on every dispatch.
* GREEN: the def-B ``handle(request) -> response`` entrypoint OWNS behavior and
  is a faithful adapter over the byte-identical pure-math methods
  (``cosine_distance`` / ``euclidean_distance`` / ``compare``). The parity tests
  assert ``handle``'s output equals the preserved methods' output field-by-field
  (behavior equivalence), and the golden test pins exact numeric outputs.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from omnibase_core.container import ModelONEXContainer

from omnimarket.nodes.node_similarity_compute.handlers.handler_similarity_compute import (
    HandlerSimilarityCompute,
)
from omnimarket.nodes.node_similarity_compute.models.model_similarity_compute_request import (
    ModelSimilarityComputeRequest,
)

_CONTRACT = (
    Path(__file__).resolve().parent.parent
    / "src/omnimarket/nodes/node_similarity_compute/contract.yaml"
)


def _handler() -> HandlerSimilarityCompute:
    return HandlerSimilarityCompute(ModelONEXContainer())


@pytest.mark.unit
class TestSimilarityComputeDefBEntrypoint:
    """The canonical def-B dispatch entrypoint exists and is authoritative."""

    def test_handle_entrypoint_is_callable(self) -> None:
        """RED on pre-flip tree (no ``handle``); GREEN post-flip.

        This is the invariant that lets the node leave both
        ``canonical_handler_shape_baseline.py`` and
        ``handler_dispatch_entrypoint_baseline.yaml`` (twin shrink-only ratchets).
        """
        handler = _handler()
        assert hasattr(handler, "handle"), (
            "HandlerSimilarityCompute must expose a def-B handle() dispatch entrypoint"
        )
        assert callable(handler.handle)

    def test_handle_works_without_explicit_initialize(self) -> None:
        """Def-B COMPUTE dispatch is self-contained (lazy default config)."""
        resp = _handler().handle(
            ModelSimilarityComputeRequest(
                operation="cosine_distance",
                vector_a=[1.0, 0.0],
                vector_b=[0.0, 1.0],
            )
        )
        assert resp.status == "success"
        assert resp.distance == pytest.approx(1.0, abs=1e-9)


@pytest.mark.unit
class TestSimilarityComputeDefBParity:
    """``handle`` output == the preserved pure-math methods' output (equivalence)."""

    def test_cosine_distance_parity(self) -> None:
        handler = _handler()
        req = ModelSimilarityComputeRequest(
            operation="cosine_distance",
            vector_a=[1.0, 2.0, 3.0],
            vector_b=[4.0, 5.0, 6.0],
        )
        resp = handler.handle(req)
        assert resp.status == "success"
        assert resp.distance == pytest.approx(
            handler.cosine_distance(req.vector_a, req.vector_b), abs=1e-12
        )
        # cosine_distance operation returns distance + dimensions only (no similarity).
        assert resp.similarity is None
        assert resp.is_match is None
        assert resp.dimensions == 3

    def test_euclidean_distance_parity(self) -> None:
        handler = _handler()
        req = ModelSimilarityComputeRequest(
            operation="euclidean_distance",
            vector_a=[0.0, 0.0],
            vector_b=[3.0, 4.0],
        )
        resp = handler.handle(req)
        assert resp.status == "success"
        assert resp.distance == pytest.approx(
            handler.euclidean_distance(req.vector_a, req.vector_b), abs=1e-12
        )
        assert resp.similarity is None
        assert resp.dimensions == 2

    def test_compare_cosine_threshold_parity(self) -> None:
        handler = _handler()
        req = ModelSimilarityComputeRequest(
            operation="compare",
            vector_a=[1.0, 0.0, 0.0],
            vector_b=[0.9, 0.1, 0.0],
            metric="cosine",
            threshold=0.5,
        )
        resp = handler.handle(req)
        golden = handler.compare(
            req.vector_a, req.vector_b, metric="cosine", threshold=0.5
        )
        assert resp.status == "success"
        assert resp.distance == pytest.approx(golden.distance, abs=1e-12)
        assert resp.similarity == pytest.approx(golden.similarity, abs=1e-12)
        assert resp.is_match is True
        assert resp.dimensions == golden.dimensions == 3

    def test_compare_euclidean_threshold_parity(self) -> None:
        handler = _handler()
        req = ModelSimilarityComputeRequest(
            operation="compare",
            vector_a=[0.0, 0.0],
            vector_b=[10.0, 10.0],
            metric="euclidean",
            threshold=1.0,
        )
        resp = handler.handle(req)
        golden = handler.compare(
            req.vector_a, req.vector_b, metric="euclidean", threshold=1.0
        )
        assert resp.status == "success"
        assert resp.distance == pytest.approx(golden.distance, abs=1e-12)
        assert resp.similarity is None
        assert resp.is_match is False
        assert resp.dimensions == 2

    def test_compare_no_threshold_is_match_none(self) -> None:
        resp = _handler().handle(
            ModelSimilarityComputeRequest(
                operation="compare",
                vector_a=[1.0, 0.0],
                vector_b=[0.0, 1.0],
                metric="cosine",
            )
        )
        assert resp.status == "success"
        assert resp.is_match is None
        assert resp.distance == pytest.approx(1.0, abs=1e-9)
        assert resp.similarity == pytest.approx(0.0, abs=1e-9)

    def test_zero_magnitude_returns_error_response(self) -> None:
        """A math ValueError (zero-magnitude cosine) maps to an error response."""
        resp = _handler().handle(
            ModelSimilarityComputeRequest(
                operation="cosine_distance",
                vector_a=[0.0, 0.0, 0.0],
                vector_b=[1.0, 0.0, 0.0],
            )
        )
        assert resp.status == "error"
        assert resp.error_message is not None
        assert "zero magnitude" in resp.error_message
        assert resp.distance is None


@pytest.mark.unit
class TestSimilarityComputeDefBGolden:
    """Golden: exact numeric contract of the def-B entrypoint over a fixed corpus."""

    def test_golden_cosine_orthogonal(self) -> None:
        resp = _handler().handle(
            ModelSimilarityComputeRequest(
                operation="cosine_distance",
                vector_a=[1.0, 0.0],
                vector_b=[0.0, 1.0],
            )
        )
        assert resp.status == "success"
        assert resp.distance == pytest.approx(1.0, abs=1e-9)
        assert resp.dimensions == 2

    def test_golden_euclidean_3_4_5(self) -> None:
        resp = _handler().handle(
            ModelSimilarityComputeRequest(
                operation="euclidean_distance",
                vector_a=[0.0, 0.0],
                vector_b=[3.0, 4.0],
            )
        )
        assert resp.status == "success"
        assert resp.distance == pytest.approx(5.0, abs=1e-9)
        assert resp.dimensions == 2

    def test_golden_cosine_identical(self) -> None:
        resp = _handler().handle(
            ModelSimilarityComputeRequest(
                operation="cosine_distance",
                vector_a=[1.0, 2.0, 3.0],
                vector_b=[1.0, 2.0, 3.0],
            )
        )
        assert resp.status == "success"
        assert resp.distance == pytest.approx(0.0, abs=1e-9)
        assert resp.dimensions == 3


@pytest.mark.unit
class TestSimilarityComputeContractStates:
    """The node's declared terminal-event topics are asserted (state-coverage gate)."""

    def test_contract_publish_topics(self) -> None:
        contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
        publish = contract["event_bus"]["publish_topics"]
        assert publish == [
            "onex.evt.omnimemory.similarity-compute-completed.v1",
            "onex.evt.omnimemory.similarity-compute-failed.v1",
        ]
