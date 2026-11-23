# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Refuses a node whose contract names no caller, schedule, tick or on-demand command."""

from omnimarket.nodes.node_trigger_declaration_check_effect.handlers import (
    HandlerTriggerDeclarationCheck,
)


class NodeTriggerDeclarationCheckEffect(HandlerTriggerDeclarationCheck):
    """ONEX entry point."""


__all__ = ["HandlerTriggerDeclarationCheck", "NodeTriggerDeclarationCheckEffect"]
