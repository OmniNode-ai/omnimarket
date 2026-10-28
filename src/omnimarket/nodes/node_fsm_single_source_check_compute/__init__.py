# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_fsm_single_source_check_compute: one source per state machine (OMN-21477)."""

from omnimarket.nodes.node_fsm_single_source_check_compute.handlers.handler_fsm_single_source_check import (
    HandlerFsmSingleSourceCheck,
)

__all__ = ["HandlerFsmSingleSourceCheck", "NodeFsmSingleSourceCheckCompute"]


class NodeFsmSingleSourceCheckCompute(HandlerFsmSingleSourceCheck):
    """ONEX entry-point wrapper for HandlerFsmSingleSourceCheck."""
