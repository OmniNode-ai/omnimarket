# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Canonical node for the Wire Compatibility Gate's consumer-pin reader (OMN-21901)."""

from omnimarket.handlers.handler_wire_consumer_pin import HandlerWireConsumerPin


class NodeWireConsumerPinCompute(HandlerWireConsumerPin):
    """Declarative entry point; the pin policy lives in the shared handler."""


__all__ = ["NodeWireConsumerPinCompute"]
