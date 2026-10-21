# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Work-state context source node: open work and orchestration state (OMN-21111)."""

from omnimarket.nodes.node_context_source_work_state_effect.handlers.handler_context_source_work_state import (
    HandlerContextSourceWorkState,
)


class NodeContextSourceWorkStateEffect(HandlerContextSourceWorkState):
    """ONEX entry point."""


__all__ = ["HandlerContextSourceWorkState", "NodeContextSourceWorkStateEffect"]
