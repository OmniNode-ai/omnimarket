# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Semantic compute handler — re-export from omnimemory.

Migrated from omnimemory (OMN-8297, Wave 1).
The 1600+ line implementation lives in omnimemory alongside the
ProtocolEmbeddingProvider / ProtocolLLMProvider definitions it depends on.
Omnimarket registers the entry point; omnimemory owns the implementation.
"""

from __future__ import annotations

from omnimemory.nodes.node_semantic_analyzer_compute.handlers.handler_semantic_compute import (
    HandlerSemanticCompute,
    HandlerSemanticComputePolicy,
)

__all__ = [
    "HandlerSemanticCompute",
    "HandlerSemanticComputePolicy",
]
