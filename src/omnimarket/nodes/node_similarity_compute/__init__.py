# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Similarity compute node — pure vector math, no I/O.

Migrated from omnimemory (OMN-8297, Wave 1).
"""

from omnimarket.nodes.node_similarity_compute.handlers.handler_similarity_compute import (
    HandlerSimilarityCompute,
)

__all__ = [
    "HandlerSimilarityCompute",
    "NodeSimilarityCompute",
]


class NodeSimilarityCompute:
    """ONEX entry-point marker for node_similarity_compute."""

    __onex_node_type__ = "node_similarity_compute"
