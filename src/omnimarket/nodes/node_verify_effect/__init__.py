# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_verify_effect — System health verification effect node."""

from omnimarket.nodes.node_verify_effect.handlers.handler_verify import (
    HandlerVerify,
)

__all__ = [
    "HandlerVerify",
    "NodeVerifyEffect",
]


class NodeVerifyEffect(HandlerVerify):
    """ONEX entry-point wrapper for HandlerVerify."""
