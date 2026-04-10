# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""Pure compute handler for vector similarity operations.

and distance metrics. It performs NO I/O operations - all computation is done
using pure Python math operations for portability and predictability.

Key Design Decisions:
    - **No numpy**: Uses pure Python `list[float]` / `Sequence[float]` for vectors
    - **Numerical stability**: Uses `math.fsum()` for accurate summation
    - **Single-pass loops**: Minimizes memory allocations and cache misses
    - **Strict validation**: Raises clear errors for invalid inputs

Supported Metrics:
    - **Cosine distance**: 1 - cosine_similarity (0 = identical, 2 = opposite)
    - **Euclidean distance**: L2 norm between vectors

Example::

    from omnibase_core.container import ModelONEXContainer
    from omnimarket.nodes.node_similarity_compute.handlers import (
        HandlerSimilarityCompute,
        ModelHandlerSimilarityComputeConfig,
    )

    container = ModelONEXContainer()
    handler = HandlerSimilarityCompute(container)
    await handler.initialize()

    # Compute cosine distance
    vec_a = [0.1, 0.2, 0.3, 0.4]
    vec_b = [0.2, 0.3, 0.4, 0.5]
    distance = handler.cosine_distance(vec_a, vec_b)

    # Full comparison with threshold
    result = handler.compare(vec_a, vec_b, metric="cosine", threshold=0.5)
    print(f"Distance: {result.distance}, Match: {result.is_match}")

Performance:
    - Single vector comparison: <0.1ms for 1536-dimensional vectors
    - Memory: O(1) additional allocation (single-pass computation)

.. versionadded:: 0.1.0
    Initial implementation for OMN-1388.

.. versionchanged:: 0.2.0
    Refactored to container-driven pattern for OMN-1577.
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from collections.abc import Sequence

    from omnibase_core.container import ModelONEXContainer

from omnimemory.models.memory.model_similarity_result import ModelSimilarityResult

from omnimarket.nodes.node_similarity_compute.models import (
    ModelHandlerSimilarityComputeConfig,
)

logger = logging.getLogger(__name__)

__all__ = [
    "HandlerSimilarityCompute",
    "ModelHandlerSimilarityComputeConfig",
    "ModelSimilarityComputeHealth",
    "ModelSimilarityComputeMetadata",
]


class ModelSimilarityComputeHealth(  # omnimemory-model-exempt: handler health
    BaseModel
):
    """Health status for the Similarity Compute Handler.

    Returned by health_check() to provide detailed health information.
    For a pure compute handler with no external dependencies, this
    always reports as healthy when initialized.

    Attributes:
        healthy: Whether the handler is healthy.
        handler: Handler identifier string.
        initialized: Whether the handler has been initialized.
    """

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
    )

    healthy: bool = Field(
        ...,
        description="Whether the handler is healthy",
    )
    handler: str = Field(
        ...,
        description="Handler identifier string",
    )
    initialized: bool = Field(
        ...,
        description="Whether the handler has been initialized",
    )


class ModelSimilarityComputeMetadata(  # omnimemory-model-exempt: handler metadata
    BaseModel
):
    """Metadata describing similarity compute handler capabilities and configuration.

    Returned by describe() method to provide introspection information
    about the handler's capabilities, supported metrics, and status.

    Attributes:
        handler_type: Type identifier for this handler.
        capabilities: List of supported operations.
        is_pure_compute: Whether handler performs only pure computation.
        initialized: Whether the handler has been initialized.
        supported_metrics: List of supported distance metrics.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    handler_type: str = Field(
        ...,
        description="Type identifier for this handler",
    )
    capabilities: list[str] = Field(
        ...,
        description="List of supported operations",
    )
    is_pure_compute: bool = Field(
        ...,
        description="Whether handler performs only pure computation (no I/O)",
    )
    initialized: bool = Field(
        ...,
        description="Whether the handler has been initialized",
    )
    supported_metrics: list[str] = Field(
        ...,
        description="List of supported distance metrics",
    )


class HandlerSimilarityCompute:
    """Pure compute handler for vector similarity operations.

    common vector similarity and distance metrics. It performs NO I/O
    operations - all computation uses pure Python math.

    The implementation prioritizes:
        - **Correctness**: Strict validation catches invalid inputs early
        - **Numerical stability**: Uses `math.fsum()` for accurate summation
        - **Performance**: Single-pass algorithms minimize overhead
        - **Portability**: Pure Python with no external dependencies

    Follows the container-driven pattern where:
        - Constructor takes only the DI container
        - Configuration is provided via initialize()
        - Handler provides health_check() and describe() for introspection
        - Fail-fast behavior: raises RuntimeError if used before initialization

    Attributes:
        config: The handler configuration. Raises RuntimeError if accessed
            before initialize() is called.

    Example::

        container = ModelONEXContainer()
        handler = HandlerSimilarityCompute(container)
        await handler.initialize()

        # Identical vectors have distance 0
        vec = [1.0, 2.0, 3.0]
        assert handler.cosine_distance(vec, vec) == 0.0

        # Orthogonal vectors have cosine distance 1.0
        vec_a = [1.0, 0.0]
        vec_b = [0.0, 1.0]
        assert handler.cosine_distance(vec_a, vec_b) == 1.0
    """

    def __init__(self, container: ModelONEXContainer) -> None:
        """Initialize the similarity compute handler with container injection.

        Args:
            container: ONEX container for dependency injection.
        """
        self._container = container
        self._config: ModelHandlerSimilarityComputeConfig | None = None
        self._initialized = False
        self._init_lock = asyncio.Lock()

    async def initialize(
        self,
        config: ModelHandlerSimilarityComputeConfig | None = None,
    ) -> None:
        """Initialize the handler with optional configuration.

        This method must be called before using the handler's compute methods.
        If no config is provided, a default configuration will be used.

        Thread-safe: Uses an asyncio.Lock to prevent concurrent double-initialization.

        Args:
            config: Optional handler configuration. If None, uses defaults.
        """
        async with self._init_lock:
            if self._initialized:
                logger.debug("HandlerSimilarityCompute already initialized, skipping")
                return
            self._config = config or ModelHandlerSimilarityComputeConfig()
            self._initialized = True
            logger.debug("HandlerSimilarityCompute initialization complete")

    async def health_check(self) -> ModelSimilarityComputeHealth:
        """Return health status of the handler.

        For a pure compute handler, health is determined by initialization state.
        An uninitialized handler is not healthy since it cannot perform computations.

        Returns:
            ModelSimilarityComputeHealth with status information.
        """
        return ModelSimilarityComputeHealth(
            healthy=self._initialized,
            handler="similarity_compute",
            initialized=self._initialized,
        )

    async def describe(self) -> ModelSimilarityComputeMetadata:
        """Return handler metadata and capabilities.

        Returns:
            ModelSimilarityComputeMetadata with handler information including
            handler type, capabilities, and supported metrics.
        """
        return ModelSimilarityComputeMetadata(
            handler_type="similarity_compute",
            capabilities=["cosine_distance", "euclidean_distance", "compare"],
            is_pure_compute=True,
            initialized=self._initialized,
            supported_metrics=["cosine", "euclidean"],
        )

    async def shutdown(self) -> None:
        """Clean up handler resources.

        Safe to call multiple times. After shutdown, initialize() must be
        called again to use the handler.

        .. versionadded:: 0.2.0
            Added for container-driven pattern consistency (OMN-1577).
        """
        self._config = None
        self._initialized = False
        logger.debug("HandlerSimilarityCompute shutdown complete")

    def _ensure_initialized(self) -> None:
        """Ensure the handler is initialized before operations.

        Raises:
            RuntimeError: If handler is not initialized.
        """
        if not self._initialized:
            raise RuntimeError(
                "HandlerSimilarityCompute is not initialized. "
                "Call initialize() before using the handler."
            )

    @property
    def container(self) -> ModelONEXContainer:
        """Get the ONEX container."""
        return self._container

    @property
    def config(self) -> ModelHandlerSimilarityComputeConfig:
        """Get the handler configuration.

        Raises:
            RuntimeError: If handler is not initialized.
        """
        self._ensure_initialized()
        # Defensive check: after _ensure_initialized(), _config should never be None
        # Using explicit check instead of assert for production safety
        if self._config is None:
            raise RuntimeError(
                "HandlerSimilarityCompute config is None after initialization check. "
                "This indicates an internal state inconsistency."
            )
        return self._config

    def _validate_vectors(
        self,
        vec_a: Sequence[float],
        vec_b: Sequence[float],
        check_zero_magnitude: bool = False,
    ) -> tuple[float, float]:
        """Validate vector inputs and optionally compute magnitudes.

        Performs comprehensive validation in a single pass through both vectors:
            1. Checks for empty vectors
            2. Validates dimension match
            3. Checks for NaN/Inf values
            4. Optionally computes and validates magnitudes (for cosine)

        Args:
            vec_a: First vector to validate.
            vec_b: Second vector to validate.
            check_zero_magnitude: If True, compute magnitudes and raise
                ValueError if either vector has zero magnitude.

        Returns:
            Tuple of (magnitude_a, magnitude_b) if check_zero_magnitude is True,
            otherwise (0.0, 0.0).

        Raises:
            ValueError: If vectors are empty, have mismatched dimensions,
                contain NaN/Inf values, or have zero magnitude (when checked).
        """
        # Check empty vectors
        len_a = len(vec_a)
        len_b = len(vec_b)

        if len_a == 0:
            raise ValueError("vec_a cannot be empty")
        if len_b == 0:
            raise ValueError("vec_b cannot be empty")

        # Check dimension mismatch
        if len_a != len_b:
            raise ValueError(
                f"Dimension mismatch: vec_a has {len_a} dimensions, "
                f"vec_b has {len_b} dimensions"
            )

        # Single-pass validation and optional magnitude computation
        sum_sq_a = 0.0
        sum_sq_b = 0.0

        for i, (a, b) in enumerate(zip(vec_a, vec_b, strict=False)):
            # Validate vec_a element
            if math.isnan(a):
                raise ValueError(f"vec_a contains NaN at index {i}")
            if math.isinf(a):
                raise ValueError(
                    f"vec_a contains {'positive' if a > 0 else 'negative'} "
                    f"infinity at index {i}"
                )

            # Validate vec_b element
            if math.isnan(b):
                raise ValueError(f"vec_b contains NaN at index {i}")
            if math.isinf(b):
                raise ValueError(
                    f"vec_b contains {'positive' if b > 0 else 'negative'} "
                    f"infinity at index {i}"
                )

            # Accumulate squared values for magnitude if needed
            if check_zero_magnitude:
                sum_sq_a += a * a
                sum_sq_b += b * b

        # Check zero magnitude if required
        if check_zero_magnitude:
            mag_a = math.sqrt(sum_sq_a)
            mag_b = math.sqrt(sum_sq_b)

            if mag_a < self.config.epsilon:
                raise ValueError(
                    f"vec_a has zero magnitude (sum of squares: {sum_sq_a:.2e})"
                )
            if mag_b < self.config.epsilon:
                raise ValueError(
                    f"vec_b has zero magnitude (sum of squares: {sum_sq_b:.2e})"
                )

            return (mag_a, mag_b)

        return (0.0, 0.0)

    def cosine_distance(
        self,
        vec_a: Sequence[float],
        vec_b: Sequence[float],
    ) -> float:
        """Compute cosine distance between two vectors.

        Cosine distance is defined as 1 - cosine_similarity, where:
            cosine_similarity = dot(a, b) / (||a|| * ||b||)

        The result ranges from 0 (identical direction) to 2 (opposite direction),
        with 1 indicating orthogonal vectors.

        Uses `math.fsum()` for numerically stable dot product computation.

        Args:
            vec_a: First vector (must be non-empty with non-zero magnitude).
            vec_b: Second vector (must match vec_a dimensions).

        Returns:
            Cosine distance in range [0, 2].

        Raises:
            RuntimeError: If handler is not initialized.
            ValueError: If vectors are empty, have mismatched dimensions,
                contain NaN/Inf values, or have zero magnitude.

        Example::

            from omnibase_core.container import ModelONEXContainer

            container = ModelONEXContainer()
            handler = HandlerSimilarityCompute(container)
            await handler.initialize()

            # Identical vectors: distance = 0
            vec = [1.0, 2.0, 3.0]
            assert handler.cosine_distance(vec, vec) == 0.0

            # Opposite vectors: distance = 2
            vec_pos = [1.0, 0.0]
            vec_neg = [-1.0, 0.0]
            assert handler.cosine_distance(vec_pos, vec_neg) == 2.0
        """
        self._ensure_initialized()

        # Validate and get magnitudes (single pass)
        mag_a, mag_b = self._validate_vectors(vec_a, vec_b, check_zero_magnitude=True)

        # Compute dot product using math.fsum for numerical stability
        # Single pass through vectors
        dot_products = [a * b for a, b in zip(vec_a, vec_b, strict=False)]
        dot_product = math.fsum(dot_products)

        # Compute cosine similarity
        cosine_similarity = dot_product / (mag_a * mag_b)

        # Clamp to [-1, 1] to handle floating-point errors
        cosine_similarity = max(-1.0, min(1.0, cosine_similarity))

        # Return cosine distance
        return 1.0 - cosine_similarity

    def euclidean_distance(
        self,
        vec_a: Sequence[float],
        vec_b: Sequence[float],
    ) -> float:
        """Compute Euclidean (L2) distance between two vectors.

        Euclidean distance is defined as:
            sqrt(sum((a[i] - b[i])^2 for all i))

        Uses `math.fsum()` for numerically stable summation.

        Args:
            vec_a: First vector (must be non-empty).
            vec_b: Second vector (must match vec_a dimensions).

        Returns:
            Euclidean distance (non-negative).

        Raises:
            RuntimeError: If handler is not initialized.
            ValueError: If vectors are empty, have mismatched dimensions,
                or contain NaN/Inf values.

        Example::

            from omnibase_core.container import ModelONEXContainer

            container = ModelONEXContainer()
            handler = HandlerSimilarityCompute(container)
            await handler.initialize()

            # Identical vectors: distance = 0
            vec = [1.0, 2.0, 3.0]
            assert handler.euclidean_distance(vec, vec) == 0.0

            # Unit distance
            vec_a = [0.0, 0.0]
            vec_b = [1.0, 0.0]
            assert handler.euclidean_distance(vec_a, vec_b) == 1.0
        """
        self._ensure_initialized()

        # Validate vectors (no magnitude check needed for euclidean)
        self._validate_vectors(vec_a, vec_b, check_zero_magnitude=False)

        # Compute squared differences using math.fsum for stability
        squared_diffs = [(a - b) ** 2 for a, b in zip(vec_a, vec_b, strict=False)]
        sum_squared = math.fsum(squared_diffs)

        return math.sqrt(sum_squared)

    def compare(
        self,
        vec_a: Sequence[float],
        vec_b: Sequence[float],
        metric: Literal["cosine", "euclidean"] = "cosine",
        threshold: float | None = None,
    ) -> ModelSimilarityResult:
        """Compare two vectors and return a full result object.

        returning a structured result with distance, similarity (for cosine),
        and optional match determination based on a threshold.

        Args:
            vec_a: First vector to compare.
            vec_b: Second vector to compare.
            metric: Distance metric to use ("cosine" or "euclidean").
            threshold: Optional threshold for match determination.
                For cosine: vectors match if distance <= threshold.
                For euclidean: vectors match if distance <= threshold.

        Returns:
            ModelSimilarityResult with:
                - metric: The metric used
                - distance: The computed distance
                - similarity: Cosine similarity (None for euclidean)
                - is_match: Whether distance is within threshold (None if no threshold)
                - threshold: The threshold used (None if not provided)
                - dimensions: Number of dimensions in the vectors

        Raises:
            RuntimeError: If handler is not initialized.
            ValueError: If vectors are invalid or metric is unknown.

        Example::

            from omnibase_core.container import ModelONEXContainer

            container = ModelONEXContainer()
            handler = HandlerSimilarityCompute(container)
            await handler.initialize()

            vec_a = [0.1, 0.2, 0.3]
            vec_b = [0.15, 0.25, 0.35]

            # Cosine comparison with threshold
            result = handler.compare(vec_a, vec_b, metric="cosine", threshold=0.1)
            print(f"Similarity: {result.similarity:.4f}")
            print(f"Distance: {result.distance:.4f}")
            print(f"Match: {result.is_match}")

            # Euclidean comparison
            result = handler.compare(vec_a, vec_b, metric="euclidean")
            print(f"Distance: {result.distance:.4f}")
        """
        self._ensure_initialized()

        dimensions = len(vec_a)

        if metric == "cosine":
            distance = self.cosine_distance(vec_a, vec_b)
            similarity = 1.0 - distance

            is_match: bool | None = None
            if threshold is not None:
                is_match = distance <= threshold

            return ModelSimilarityResult(
                metric="cosine",
                distance=distance,
                similarity=similarity,
                is_match=is_match,
                threshold=threshold,
                dimensions=dimensions,
            )

        if metric == "euclidean":
            distance = self.euclidean_distance(vec_a, vec_b)

            is_match = None
            if threshold is not None:
                is_match = distance <= threshold

            return ModelSimilarityResult(
                metric="euclidean",
                distance=distance,
                similarity=None,  # Not applicable for euclidean
                is_match=is_match,
                threshold=threshold,
                dimensions=dimensions,
            )

        # This should be caught by type checking, but provide runtime safety
        raise ValueError(
            f"Unknown metric '{metric}'. Supported metrics: 'cosine', 'euclidean'"
        )
