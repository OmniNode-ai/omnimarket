# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Node entry point for context-selection policy compute (OMN-12843 / M3)."""

from __future__ import annotations

from omnimarket.nodes.node_context_selection_policy_compute.handlers.handler_context_selection_policy import (
    HandlerContextSelectionPolicy,
)


class NodeContextSelectionPolicyCompute(HandlerContextSelectionPolicy):
    """ONEX entry-point wrapper for HandlerContextSelectionPolicy."""


__all__ = ["NodeContextSelectionPolicyCompute"]
