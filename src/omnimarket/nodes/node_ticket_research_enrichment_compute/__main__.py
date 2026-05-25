# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Entry point for node_ticket_research_enrichment_compute."""

from omnimarket.nodes.node_ticket_research_enrichment_compute.handlers.handler_ticket_research_enrichment import (
    HandlerTicketResearchEnrichment,
)

__all__: list[str] = ["HandlerTicketResearchEnrichment"]
