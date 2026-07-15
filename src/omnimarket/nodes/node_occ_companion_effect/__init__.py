# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from omnimarket.nodes.node_occ_companion_effect.handlers.handler_occ_companion_effect import (
    HandlerOccCompanionEffect,
)


class NodeOccCompanionEffect(HandlerOccCompanionEffect):
    """ONEX entry-point wrapper for HandlerOccCompanionEffect (RSD-3, OMN-14622)."""


__all__ = [
    "HandlerOccCompanionEffect",
    "NodeOccCompanionEffect",
]
