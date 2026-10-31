# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Construct the handler bound to node_typed_decision_effect."""

from __future__ import annotations

from omnimarket.nodes.node_typed_decision_effect.handlers.handler_typed_decision import (
    HandlerTypedDecision,
)

__all__ = ["create_handler_typed_decision"]


def create_handler_typed_decision() -> HandlerTypedDecision:
    """Build the node handler with its bound dependencies."""
    return HandlerTypedDecision()
