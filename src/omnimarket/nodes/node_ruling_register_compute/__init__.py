# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Public entry point for the ruling register read."""

from omnimarket.nodes.node_ruling_register_compute.handlers.handler_ruling_register import (
    HandlerRulingRegister,
)


class NodeRulingRegisterCompute(HandlerRulingRegister):
    """Registered ONEX node."""


__all__ = ["NodeRulingRegisterCompute"]
