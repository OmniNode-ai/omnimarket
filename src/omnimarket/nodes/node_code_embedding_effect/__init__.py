# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_code_embedding_effect — Code entity embedding and Qdrant storage effect node.

Re-implements omniintelligence dispatch_handler_code_embedding (deleted in PR #568).
[OMN-5657, OMN-5665]
"""

from omnimarket.nodes.node_code_embedding_effect.handlers.handler_code_embedding_effect import (
    HandlerCodeEmbeddingEffect,
    build_embedding_text,
)

__all__ = ["HandlerCodeEmbeddingEffect", "build_embedding_text"]
