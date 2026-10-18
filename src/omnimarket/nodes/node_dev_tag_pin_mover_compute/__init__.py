# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_dev_tag_pin_mover_compute - move a consumer's uv git pin to a sibling's new dev tag (OMN-19998)."""

from omnimarket.nodes.node_dev_tag_pin_mover_compute.handlers.handler_dev_tag_pin_mover import (
    HandlerDevTagPinMover,
)


class NodeDevTagPinMoverCompute(HandlerDevTagPinMover):
    """ONEX entry-point wrapper for HandlerDevTagPinMover (OMN-19998)."""


__all__ = ["HandlerDevTagPinMover", "NodeDevTagPinMoverCompute"]
