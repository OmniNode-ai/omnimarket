# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Standing command census alongside the factory-line seam plan."""

from omnimarket.nodes.node_seam_read_orchestrator.handlers.handler_command_seam_census import (
    HandlerCommandSeamCensus,
)

__all__ = ["HandlerCommandSeamCensus", "NodeSeamReadOrchestrator"]


class NodeSeamReadOrchestrator(HandlerCommandSeamCensus):
    """Declarative runtime entry point; the contract owns its trigger and routes."""
