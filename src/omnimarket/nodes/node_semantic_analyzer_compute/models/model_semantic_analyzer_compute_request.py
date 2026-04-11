# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request model for semantic analyzer compute node.

Migrated from omnimemory (OMN-8297, Wave 1).
Re-exported from omnimemory — implementation owned there alongside protocols.
"""

from __future__ import annotations

# omnimemory is a declared external dep (omninode-memory in pyproject.toml)
from omnimemory.nodes.node_semantic_analyzer_compute.models.model_semantic_analyzer_compute_request import (
    ModelSemanticAnalyzerComputeRequest,
)

__all__ = ["ModelSemanticAnalyzerComputeRequest"]
