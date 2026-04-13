# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Semantic analyzer compute node — embedding generation, entity extraction, full analysis.

Migrated from omnimemory (OMN-8297, Wave 1).
Implementation re-exported from omnimemory alongside its protocol dependencies.
"""

from omnimarket.nodes.node_semantic_analyzer_compute.handlers.handler_semantic_compute import (
    HandlerSemanticCompute,
)

__all__ = [
    "HandlerSemanticCompute",
    "NodeSemanticAnalyzerCompute",
]


class NodeSemanticAnalyzerCompute:
    """ONEX entry-point marker for node_semantic_analyzer_compute."""

    __onex_node_type__ = "node_semantic_analyzer_compute"
