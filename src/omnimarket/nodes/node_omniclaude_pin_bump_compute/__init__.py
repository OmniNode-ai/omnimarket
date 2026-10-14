# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_omniclaude_pin_bump_compute - move the omniclaude pin on a producer release (OMN-20001)."""

from omnimarket.nodes.node_omniclaude_pin_bump_compute.handlers.handler_omniclaude_pin_bump import (
    HandlerOmniclaudePinBump,
)


class NodeOmniclaudePinBumpCompute(HandlerOmniclaudePinBump):
    """ONEX entry-point wrapper for HandlerOmniclaudePinBump (OMN-20001)."""


__all__ = ["HandlerOmniclaudePinBump", "NodeOmniclaudePinBumpCompute"]
