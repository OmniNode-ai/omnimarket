# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_projection_dod_verdict — the durable definition-of-done verdict (OMN-18900)."""

from omnimarket.nodes.node_projection_dod_verdict.handlers.handler_projection_dod_verdict import (
    HandlerProjectionDodVerdict,
)

__all__ = [
    "HandlerProjectionDodVerdict",
    "NodeProjectionDodVerdict",
]


class NodeProjectionDodVerdict(HandlerProjectionDodVerdict):
    """ONEX entry-point wrapper for HandlerProjectionDodVerdict."""
