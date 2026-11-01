# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_verify_effect — System health verification effect node."""

from omnimarket.nodes.node_verify_effect.handlers.handler_verify import (
    HandlerVerify,
)
from omnimarket.nodes.node_verify_effect.models.model_verify_input import (
    ModelVerifyInput,
)

__all__ = [
    "HandlerVerify",
    "ModelVerifyInput",
    "NodeVerifyEffect",
]


class NodeVerifyEffect(HandlerVerify):
    """ONEX entry-point wrapper for HandlerVerify."""
