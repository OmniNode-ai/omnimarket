# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Public entry point for the one-off register read."""

from omnimarket.nodes.node_oneoff_register_compute.handlers.handler_oneoff_register import (
    HandlerOneOffRegister,
)


class NodeOneOffRegisterCompute(HandlerOneOffRegister):
    """Registered ONEX node."""


__all__ = ["NodeOneOffRegisterCompute"]
