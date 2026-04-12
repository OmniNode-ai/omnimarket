# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_similarity_compute.

Migrated from omnimemory (OMN-8297, Wave 1).
Verifies cosine distance, euclidean distance, compare with threshold,
error handling, and handler initialization lifecycle.
"""

from __future__ import annotations

import pytest

from omnibase_core.container import ModelONEXContainer
from omnimarket.nodes.node_similarity_compute.handlers.handler_similarity_compute import (
    HandlerSimilarityCompute,
)


@pytest.fixture
async def handler() -> HandlerSimilarityCompute:
    container = ModelONEXContainer()
    h = HandlerSimilarityCompute(container)
    await h.initialize()
    return h


@pytest.mark.unit
class TestSimilarityComputeGoldenChain:
    """Golden chain: vectors in -> distance/similarity out."""

    async def test_cosine_distance_identical_vectors(
        self, handler: HandlerSimilarityCompute
    ) -> None:
        """Identical vectors have cosine distance 0."""
        vec = [1.0, 2.0, 3.0]
        distance = handler.cosine_distance(vec, vec)
        assert distance == pytest.approx(0.0, abs=1e-9)

    async def test_cosine_distance_orthogonal_vectors(
        self, handler: HandlerSimilarityCompute
    ) -> None:
        """Orthogonal vectors have cosine distance 1."""
        vec_a = [1.0, 0.0]
        vec_b = [0.0, 1.0]
        distance = handler.cosine_distance(vec_a, vec_b)
        assert distance == pytest.approx(1.0, abs=1e-9)

    async def test_cosine_distance_opposite_vectors(
        self, handler: HandlerSimilarityCompute
    ) -> None:
        """Opposite vectors have cosine distance 2."""
        vec_pos = [1.0, 0.0]
        vec_neg = [-1.0, 0.0]
        distance = handler.cosine_distance(vec_pos, vec_neg)
        assert distance == pytest.approx(2.0, abs=1e-9)

    async def test_euclidean_distance_identical_vectors(
        self, handler: HandlerSimilarityCompute
    ) -> None:
        """Identical vectors have euclidean distance 0."""
        vec = [1.0, 2.0, 3.0]
        distance = handler.euclidean_distance(vec, vec)
        assert distance == pytest.approx(0.0, abs=1e-9)

    async def test_euclidean_distance_unit_step(
        self, handler: HandlerSimilarityCompute
    ) -> None:
        """Unit step along one axis has euclidean distance 1."""
        vec_a = [0.0, 0.0]
        vec_b = [1.0, 0.0]
        distance = handler.euclidean_distance(vec_a, vec_b)
        assert distance == pytest.approx(1.0, abs=1e-9)

    async def test_euclidean_distance_3_4_5_triangle(
        self, handler: HandlerSimilarityCompute
    ) -> None:
        """3-4-5 right triangle has euclidean distance 5."""
        vec_a = [0.0, 0.0]
        vec_b = [3.0, 4.0]
        distance = handler.euclidean_distance(vec_a, vec_b)
        assert distance == pytest.approx(5.0, abs=1e-9)

    async def test_compare_cosine_with_threshold_match(
        self, handler: HandlerSimilarityCompute
    ) -> None:
        """Similar vectors with generous threshold should match."""
        vec_a = [1.0, 0.0, 0.0]
        vec_b = [0.9, 0.1, 0.0]
        result = handler.compare(vec_a, vec_b, metric="cosine", threshold=0.5)
        assert result.metric == "cosine"
        assert result.is_match is True
        assert result.similarity is not None
        assert result.similarity > 0.9

    async def test_compare_cosine_no_threshold(
        self, handler: HandlerSimilarityCompute
    ) -> None:
        """Compare without threshold returns is_match=None."""
        vec_a = [1.0, 0.0]
        vec_b = [0.0, 1.0]
        result = handler.compare(vec_a, vec_b, metric="cosine")
        assert result.is_match is None
        assert result.distance == pytest.approx(1.0, abs=1e-9)

    async def test_compare_euclidean_with_threshold_no_match(
        self, handler: HandlerSimilarityCompute
    ) -> None:
        """Far-apart vectors should not match tight threshold."""
        vec_a = [0.0, 0.0]
        vec_b = [10.0, 10.0]
        result = handler.compare(vec_a, vec_b, metric="euclidean", threshold=1.0)
        assert result.is_match is False
        assert result.dimensions == 2

    async def test_handler_health_check(
        self, handler: HandlerSimilarityCompute
    ) -> None:
        """Initialized handler reports healthy."""
        health = await handler.health_check()
        assert health.healthy is True
        assert health.initialized is True

    async def test_handler_describe(self, handler: HandlerSimilarityCompute) -> None:
        """Describe returns expected metadata."""
        meta = await handler.describe()
        assert meta.is_pure_compute is True
        assert "cosine_distance" in meta.capabilities
        assert "euclidean_distance" in meta.capabilities
        assert "compare" in meta.capabilities

    async def test_uninitialized_handler_raises(self) -> None:
        """Using an uninitialized handler raises RuntimeError."""
        container = ModelONEXContainer()
        h = HandlerSimilarityCompute(container)
        with pytest.raises(RuntimeError, match="not initialized"):
            h.cosine_distance([1.0], [1.0])

    async def test_zero_magnitude_vector_raises(
        self, handler: HandlerSimilarityCompute
    ) -> None:
        """Zero-magnitude vector raises ValueError for cosine distance."""
        vec_a = [0.0, 0.0, 0.0]
        vec_b = [1.0, 0.0, 0.0]
        with pytest.raises(ValueError, match="zero magnitude"):
            handler.cosine_distance(vec_a, vec_b)

    async def test_dimension_mismatch_raises(
        self, handler: HandlerSimilarityCompute
    ) -> None:
        """Mismatched vector dimensions raise ValueError."""
        vec_a = [1.0, 2.0]
        vec_b = [1.0, 2.0, 3.0]
        with pytest.raises(ValueError, match=r"[Dd]imension"):
            handler.cosine_distance(vec_a, vec_b)

    async def test_handler_type_and_category(self) -> None:
        """Handler has correct type metadata."""
        container = ModelONEXContainer()
        h = HandlerSimilarityCompute(container)
        assert h.handler_type == "NODE_HANDLER"
        assert h.handler_category == "COMPUTE"

    async def test_compare_dimensions_in_result(
        self, handler: HandlerSimilarityCompute
    ) -> None:
        """Compare result includes correct dimension count."""
        vec_a = [1.0, 0.0, 0.0, 0.0]
        vec_b = [0.0, 1.0, 0.0, 0.0]
        result = handler.compare(vec_a, vec_b, metric="cosine")
        assert result.dimensions == 4
