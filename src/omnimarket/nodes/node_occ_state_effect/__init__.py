# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from omnimarket.nodes.node_occ_state_effect.handlers.handler_occ_state_effect import (
    HandlerOccStateEffect,
)


class NodeOccStateEffect(HandlerOccStateEffect):
    """ONEX entry-point wrapper for HandlerOccStateEffect (RSD-2, OMN-14619)."""


__all__ = [
    "HandlerOccStateEffect",
    "NodeOccStateEffect",
]
