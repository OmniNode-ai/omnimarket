# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Similarity Compute - ONEX COMPUTE Node.

Migrated from omnimemory to omnimarket (OMN-8297).
Pure compute node for vector similarity calculations.
"""

from omnimarket.nodes.node_similarity_compute.handlers import (
    HandlerSimilarityCompute,
    ModelHandlerSimilarityComputeConfig,
)
from omnimarket.nodes.node_similarity_compute.models import (
    ModelSimilarityComputeRequest,
    ModelSimilarityComputeResponse,
)

__all__ = [
    "HandlerSimilarityCompute",
    "ModelHandlerSimilarityComputeConfig",
    "ModelSimilarityComputeRequest",
    "ModelSimilarityComputeResponse",
]
