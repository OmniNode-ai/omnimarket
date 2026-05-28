# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelPipelineCacheResult — output from node_pipeline_cache_effect."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_golden_chain_generator_compute.models.model_golden_chain_generation_result import (
    ModelGoldenChainGenerationResult,
)
from omnimarket.nodes.node_test_generator_compute.models.model_test_generation_result import (
    ModelTestGenerationResult,
)

__all__ = ["ModelPipelineCacheResult"]


class ModelPipelineCacheResult(BaseModel):
    """Combined test-generation + golden-chain result with cache provenance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cache_hit: bool
    cache_key: str
    test_generation_result: ModelTestGenerationResult
    chain_generation_result: ModelGoldenChainGenerationResult
