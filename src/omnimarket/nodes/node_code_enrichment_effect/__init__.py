# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_code_enrichment_effect — LLM-based code entity enrichment effect node.

Re-implements omniintelligence dispatch_handler_code_enrichment (deleted in PR #568).
[OMN-5657, OMN-5664]
"""

from omnimarket.nodes.node_code_enrichment_effect.handlers.handler_code_enrichment_effect import (
    HandlerCodeEnrichmentEffect,
)

__all__ = ["HandlerCodeEnrichmentEffect"]
