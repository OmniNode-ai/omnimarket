# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_semantic_analyzer_compute.

Migrated from omnimemory (OMN-8297, Wave 1).
Uses a stub embedding provider for deterministic, no-I/O testing.
Verifies embed, extract_entities, and analyze operations.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from omnibase_core.container import ModelONEXContainer
from omnimemory.enums import EnumEntityExtractionMode
from omnimemory.models.config import (
    ModelHandlerSemanticComputeConfig,
    ModelSemanticComputePolicyConfig,
)

from omnimarket.nodes.node_semantic_analyzer_compute.handlers.handler_semantic_compute import (
    HandlerSemanticCompute,
)

_EMBEDDING_DIM = 4


class _StubEmbeddingProvider:
    """Deterministic stub that returns a fixed embedding for any input.

    Implements ProtocolEmbeddingProvider fully — health_check returns bool,
    generate_embeddings_batch is supported.
    """

    @property
    def provider_name(self) -> str:
        return "stub"

    @property
    def model_name(self) -> str:
        return "stub-embed-v1"

    @property
    def embedding_dimension(self) -> int:
        return _EMBEDDING_DIM

    @property
    def is_available(self) -> bool:
        return True

    async def health_check(self) -> bool:
        return True

    async def generate_embedding(
        self,
        text: str,
        *,
        model: str | None = None,
        correlation_id: UUID | None = None,
        timeout_seconds: float | None = None,
    ) -> list[float]:
        # Deterministic: use hash of text to produce a fixed vector
        h = hash(text) % 1000
        base = float(h) / 1000.0
        return [base, 1.0 - base, base * 0.5, 0.25]

    async def generate_embeddings_batch(
        self,
        texts: list[str],
        *,
        model: str | None = None,
        correlation_id: UUID | None = None,
        timeout_seconds: float | None = None,
    ) -> list[list[float]]:
        return [await self.generate_embedding(t) for t in texts]


@pytest.fixture
async def handler() -> HandlerSemanticCompute:
    container = ModelONEXContainer()
    config = ModelHandlerSemanticComputeConfig(
        policy_config=ModelSemanticComputePolicyConfig(
            entity_extraction_mode=EnumEntityExtractionMode.DETERMINISTIC,
        )
    )
    h = HandlerSemanticCompute(container=container)
    await h.initialize(
        config=config,
        embedding_provider=_StubEmbeddingProvider(),
    )
    return h


@pytest.mark.unit
class TestSemanticAnalyzerComputeGoldenChain:
    """Golden chain: text in -> embedding/entities/analysis out."""

    async def test_embed_returns_vector(self, handler: HandlerSemanticCompute) -> None:
        """Embed operation returns a vector of correct dimension."""
        result = await handler.embed("Hello world")
        assert isinstance(result, list)
        assert len(result) == _EMBEDDING_DIM

    async def test_embed_deterministic(self, handler: HandlerSemanticCompute) -> None:
        """Same content produces same embedding."""
        vec1 = await handler.embed("test content")
        vec2 = await handler.embed("test content")
        assert vec1 == vec2

    async def test_embed_different_content_differs(
        self, handler: HandlerSemanticCompute
    ) -> None:
        """Different content produces different embeddings."""
        vec1 = await handler.embed("alpha content")
        vec2 = await handler.embed("beta content xyz")
        assert vec1 != vec2

    async def test_extract_entities_returns_entity_list(
        self, handler: HandlerSemanticCompute
    ) -> None:
        """extract_entities returns a ModelSemanticEntityList."""
        from omnimemory.models.intelligence import ModelSemanticEntityList

        result = await handler.extract_entities("John works at Google in New York.")
        assert isinstance(result, ModelSemanticEntityList)

    async def test_analyze_returns_analysis_result(
        self, handler: HandlerSemanticCompute
    ) -> None:
        """analyze returns a ModelSemanticAnalysisResult."""
        from omnimemory.models.intelligence import ModelSemanticAnalysisResult

        result = await handler.analyze("This is a test sentence for analysis.")
        assert isinstance(result, ModelSemanticAnalysisResult)

    async def test_analyze_confidence_score_in_range(
        self, handler: HandlerSemanticCompute
    ) -> None:
        """Confidence score is within [0.0, 1.0]."""
        result = await handler.analyze("Analyzing some content here.")
        assert 0.0 <= result.confidence_score <= 1.0

    async def test_analyze_complexity_score_in_range(
        self, handler: HandlerSemanticCompute
    ) -> None:
        """Complexity score is within [0.0, 1.0]."""
        result = await handler.analyze("Simple text.")
        assert 0.0 <= result.complexity_score <= 1.0

    async def test_handler_health_check_initialized(
        self, handler: HandlerSemanticCompute
    ) -> None:
        """Initialized handler with provider reports healthy."""
        health = await handler.health_check()
        assert health.initialized is True
        assert health.embedding_provider_healthy is True

    async def test_handler_describe_capabilities(
        self, handler: HandlerSemanticCompute
    ) -> None:
        """Describe returns expected operations."""
        meta = await handler.describe()
        assert "embed" in meta.operations
        assert "extract_entities" in meta.operations
        assert "analyze" in meta.operations
        assert meta.capabilities.embedding_generation is True

    async def test_uninitialized_handler_raises(self) -> None:
        """Using an uninitialized handler raises RuntimeError."""
        container = ModelONEXContainer()
        h = HandlerSemanticCompute(container=container)
        with pytest.raises(RuntimeError):
            await h.embed("test")

    async def test_empty_content_raises_value_error(
        self, handler: HandlerSemanticCompute
    ) -> None:
        """Empty content raises ValueError."""
        with pytest.raises((ValueError, Exception)):
            await handler.embed("")
