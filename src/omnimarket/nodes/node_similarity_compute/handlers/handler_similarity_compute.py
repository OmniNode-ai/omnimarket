# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure compute handler for vector similarity operations.

Migrated from omnimemory to omnimarket (OMN-8297, Wave 1).
Performs NO I/O — all computation is pure Python math.

Supported Metrics:
    - cosine distance: 1 - cosine_similarity (0 = identical, 2 = opposite)
    - euclidean distance: L2 norm between vectors
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

# omnimemory is a declared external dep (omninode-memory in pyproject.toml)
from omnimemory.models.memory.model_similarity_result import ModelSimilarityResult

from omnimarket.nodes.node_similarity_compute.models.model_handler_similarity_compute_config import (
    ModelHandlerSimilarityComputeConfig,
)

logger = logging.getLogger(__name__)

__all__ = [
    "HandlerSimilarityCompute",
    "ModelHandlerSimilarityComputeConfig",
    "ModelSimilarityComputeHealth",
    "ModelSimilarityComputeMetadata",
]


class ModelSimilarityComputeHealth(BaseModel):
    """Health status for the Similarity Compute Handler."""

    model_config = ConfigDict(extra="forbid", strict=True)

    healthy: bool = Field(..., description="Whether the handler is healthy")
    handler: str = Field(..., description="Handler identifier string")
    initialized: bool = Field(
        ..., description="Whether the handler has been initialized"
    )


class ModelSimilarityComputeMetadata(BaseModel):
    """Metadata describing similarity compute handler capabilities."""

    model_config = ConfigDict(extra="forbid", strict=True)

    handler_type: str = Field(..., description="Type identifier for this handler")
    capabilities: list[str] = Field(..., description="List of supported operations")
    is_pure_compute: bool = Field(
        ..., description="Whether handler performs only pure computation"
    )
    initialized: bool = Field(
        ..., description="Whether the handler has been initialized"
    )
    supported_metrics: list[str] = Field(
        ..., description="List of supported distance metrics"
    )


class HandlerSimilarityCompute:
    """Pure compute handler for vector similarity operations.

    No I/O — all computation uses pure Python math. Container-driven pattern:
    constructor takes ModelONEXContainer; config provided via initialize().
    """

    handler_type = "NODE_HANDLER"
    handler_category = "COMPUTE"

    def __init__(self, container: ModelONEXContainer) -> None:
        self._container = container
        self._config: ModelHandlerSimilarityComputeConfig | None = None
        self._initialized = False
        self._init_lock = asyncio.Lock()

    async def initialize(
        self,
        config: ModelHandlerSimilarityComputeConfig | None = None,
    ) -> None:
        async with self._init_lock:
            if self._initialized:
                return
            self._config = config or ModelHandlerSimilarityComputeConfig()
            self._initialized = True
            logger.debug("HandlerSimilarityCompute initialized")

    async def health_check(self) -> ModelSimilarityComputeHealth:
        return ModelSimilarityComputeHealth(
            healthy=self._initialized,
            handler="similarity_compute",
            initialized=self._initialized,
        )

    async def describe(self) -> ModelSimilarityComputeMetadata:
        return ModelSimilarityComputeMetadata(
            handler_type="similarity_compute",
            capabilities=["cosine_distance", "euclidean_distance", "compare"],
            is_pure_compute=True,
            initialized=self._initialized,
            supported_metrics=["cosine", "euclidean"],
        )

    async def shutdown(self) -> None:
        self._config = None
        self._initialized = False

    def _ensure_initialized(self) -> None:
        if not self._initialized:
            raise RuntimeError(
                "HandlerSimilarityCompute is not initialized. Call initialize() first."
            )

    @property
    def config(self) -> ModelHandlerSimilarityComputeConfig:
        self._ensure_initialized()
        if self._config is None:
            raise RuntimeError("Config is None after initialization check.")
        return self._config

    def _validate_vectors(
        self,
        vec_a: Sequence[float],
        vec_b: Sequence[float],
        check_zero_magnitude: bool = False,
    ) -> tuple[float, float]:
        len_a = len(vec_a)
        len_b = len(vec_b)

        if len_a == 0:
            raise ValueError("vec_a cannot be empty")
        if len_b == 0:
            raise ValueError("vec_b cannot be empty")
        if len_a != len_b:
            raise ValueError(
                f"Dimension mismatch: vec_a has {len_a} dimensions, vec_b has {len_b} dimensions"
            )

        sum_sq_a = 0.0
        sum_sq_b = 0.0

        for i, (a, b) in enumerate(zip(vec_a, vec_b, strict=False)):
            if math.isnan(a):
                raise ValueError(f"vec_a contains NaN at index {i}")
            if math.isinf(a):
                raise ValueError(f"vec_a contains infinity at index {i}")
            if math.isnan(b):
                raise ValueError(f"vec_b contains NaN at index {i}")
            if math.isinf(b):
                raise ValueError(f"vec_b contains infinity at index {i}")

            if check_zero_magnitude:
                sum_sq_a += a * a
                sum_sq_b += b * b

        if check_zero_magnitude:
            mag_a = math.sqrt(sum_sq_a)
            mag_b = math.sqrt(sum_sq_b)
            if mag_a < self.config.epsilon:
                raise ValueError("vec_a has zero magnitude")
            if mag_b < self.config.epsilon:
                raise ValueError("vec_b has zero magnitude")
            return (mag_a, mag_b)

        return (0.0, 0.0)

    def cosine_distance(
        self,
        vec_a: Sequence[float],
        vec_b: Sequence[float],
    ) -> float:
        """Compute cosine distance (1 - cosine_similarity). Range [0, 2]."""
        self._ensure_initialized()
        mag_a, mag_b = self._validate_vectors(vec_a, vec_b, check_zero_magnitude=True)
        dot_product = math.fsum(a * b for a, b in zip(vec_a, vec_b, strict=False))
        cosine_similarity = max(-1.0, min(1.0, dot_product / (mag_a * mag_b)))
        return 1.0 - cosine_similarity

    def euclidean_distance(
        self,
        vec_a: Sequence[float],
        vec_b: Sequence[float],
    ) -> float:
        """Compute Euclidean (L2) distance."""
        self._ensure_initialized()
        self._validate_vectors(vec_a, vec_b, check_zero_magnitude=False)
        return math.sqrt(
            math.fsum((a - b) ** 2 for a, b in zip(vec_a, vec_b, strict=False))
        )

    def compare(
        self,
        vec_a: Sequence[float],
        vec_b: Sequence[float],
        metric: Literal["cosine", "euclidean"] = "cosine",
        threshold: float | None = None,
    ) -> ModelSimilarityResult:
        """Compare two vectors and return a structured result."""
        self._ensure_initialized()
        dimensions = len(vec_a)

        if metric == "cosine":
            distance = self.cosine_distance(vec_a, vec_b)
            similarity = 1.0 - distance
            is_match: bool | None = (
                (distance <= threshold) if threshold is not None else None
            )
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
            is_match = (distance <= threshold) if threshold is not None else None
            return ModelSimilarityResult(
                metric="euclidean",
                distance=distance,
                similarity=None,
                is_match=is_match,
                threshold=threshold,
                dimensions=dimensions,
            )

        raise ValueError(f"Unknown metric '{metric}'. Supported: 'cosine', 'euclidean'")
