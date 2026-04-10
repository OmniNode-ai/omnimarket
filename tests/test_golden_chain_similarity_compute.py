# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_similarity_compute.

Verifies cosine distance, euclidean distance, compare operations,
threshold matching, idempotency, and error handling.

Migrated from omnimemory to omnimarket (OMN-8297).
"""

from __future__ import annotations

import pytest
from omnibase_core.container import ModelONEXContainer

from omnimarket.nodes.node_similarity_compute.handlers.handler_similarity_compute import (
    HandlerSimilarityCompute,
)
from omnimarket.nodes.node_similarity_compute.models import (
    ModelHandlerSimilarityComputeConfig,
    ModelSimilarityComputeRequest,
    ModelSimilarityComputeResponse,
)


@pytest.fixture
async def handler() -> HandlerSimilarityCompute:
    """Initialized similarity compute handler."""
    container = ModelONEXContainer()
    h = HandlerSimilarityCompute(container)
    await h.initialize(config=ModelHandlerSimilarityComputeConfig())
    return h


@pytest.mark.unit
class TestSimilarityComputeGoldenChain:
    """Golden chain: vectors in -> distance/similarity out."""

    async def test_cosine_identical_vectors(
        self, handler: HandlerSimilarityCompute
    ) -> None:
        """Identical vectors have cosine distance 0."""
        vec = [1.0, 2.0, 3.0]
        distance = handler.cosine_distance(vec, vec)
        assert distance == pytest.approx(0.0, abs=1e-9)

    async def test_cosine_orthogonal_vectors(
        self, handler: HandlerSimilarityCompute
    ) -> None:
        """Orthogonal vectors have cosine distance 1."""
        vec_a = [1.0, 0.0]
        vec_b = [0.0, 1.0]
        distance = handler.cosine_distance(vec_a, vec_b)
        assert distance == pytest.approx(1.0, abs=1e-9)

    async def test_euclidean_identical_vectors(
        self, handler: HandlerSimilarityCompute
    ) -> None:
        """Identical vectors have euclidean distance 0."""
        vec = [1.0, 2.0, 3.0]
        distance = handler.euclidean_distance(vec, vec)
        assert distance == pytest.approx(0.0, abs=1e-9)

    async def test_euclidean_unit_distance(
        self, handler: HandlerSimilarityCompute
    ) -> None:
        """Unit distance along one axis."""
        vec_a = [0.0, 0.0]
        vec_b = [1.0, 0.0]
        distance = handler.euclidean_distance(vec_a, vec_b)
        assert distance == pytest.approx(1.0, abs=1e-9)

    async def test_compare_cosine_with_threshold_match(
        self, handler: HandlerSimilarityCompute
    ) -> None:
        """Vectors within threshold are marked as match."""
        vec = [1.0, 0.0]
        result = handler.compare(vec, vec, metric="cosine", threshold=0.1)
        assert result.is_match is True
        assert result.distance == pytest.approx(0.0, abs=1e-9)
        assert result.similarity is not None
        assert result.similarity == pytest.approx(1.0, abs=1e-9)

    async def test_compare_cosine_no_match(
        self, handler: HandlerSimilarityCompute
    ) -> None:
        """Orthogonal vectors with small threshold are not a match."""
        vec_a = [1.0, 0.0]
        vec_b = [0.0, 1.0]
        result = handler.compare(vec_a, vec_b, metric="cosine", threshold=0.5)
        assert result.is_match is False
        assert result.distance == pytest.approx(1.0, abs=1e-9)

    async def test_compare_no_threshold(
        self, handler: HandlerSimilarityCompute
    ) -> None:
        """Compare without threshold returns is_match=None."""
        vec_a = [1.0, 2.0]
        vec_b = [1.5, 2.5]
        result = handler.compare(vec_a, vec_b, metric="cosine")
        assert result.is_match is None
        assert result.dimensions == 2

    async def test_compare_euclidean(self, handler: HandlerSimilarityCompute) -> None:
        """Euclidean compare sets similarity=None."""
        vec_a = [0.0, 0.0]
        vec_b = [3.0, 4.0]
        result = handler.compare(vec_a, vec_b, metric="euclidean")
        assert result.distance == pytest.approx(5.0, abs=1e-9)
        assert result.similarity is None

    async def test_idempotency(self, handler: HandlerSimilarityCompute) -> None:
        """Same input twice produces identical output."""
        vec_a = [0.1, 0.2, 0.3]
        vec_b = [0.4, 0.5, 0.6]
        d1 = handler.cosine_distance(vec_a, vec_b)
        d2 = handler.cosine_distance(vec_a, vec_b)
        assert d1 == d2

    async def test_dimension_mismatch_raises(
        self, handler: HandlerSimilarityCompute
    ) -> None:
        """Mismatched vector dimensions raise ValueError."""
        with pytest.raises(ValueError, match="Dimension mismatch"):
            handler.cosine_distance([1.0, 2.0], [1.0, 2.0, 3.0])

    async def test_zero_magnitude_raises(
        self, handler: HandlerSimilarityCompute
    ) -> None:
        """Zero-magnitude vector raises ValueError for cosine."""
        with pytest.raises(ValueError, match="zero magnitude"):
            handler.cosine_distance([0.0, 0.0], [1.0, 2.0])

    async def test_health_check_initialized(
        self, handler: HandlerSimilarityCompute
    ) -> None:
        """Health check reports healthy after initialization."""
        health = await handler.health_check()
        assert health.healthy is True
        assert health.initialized is True

    async def test_model_request_response_contract(
        self, handler: HandlerSimilarityCompute
    ) -> None:
        """Request model validates and response model is correct type."""
        request = ModelSimilarityComputeRequest(
            operation="cosine_distance",
            vector_a=[0.5, 0.5],
            vector_b=[0.6, 0.4],
        )
        assert request.operation == "cosine_distance"

        distance = handler.cosine_distance(request.vector_a, request.vector_b)
        response = ModelSimilarityComputeResponse(
            status="success",
            distance=distance,
            dimensions=len(request.vector_a),
        )
        assert response.status == "success"
        assert response.distance is not None
        assert response.dimensions == 2
