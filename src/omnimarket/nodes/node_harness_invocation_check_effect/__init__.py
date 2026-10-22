# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Harness invocation check effect node (OMN-21289)."""

from omnimarket.nodes.node_harness_invocation_check_effect.handlers.handler_harness_invocation_check import (
    HandlerHarnessInvocationCheck,
)


class NodeHarnessInvocationCheckEffect(HandlerHarnessInvocationCheck):
    """ONEX entrypoint refusing harness invocations outside the delegation effect node."""


__all__ = ["HandlerHarnessInvocationCheck", "NodeHarnessInvocationCheckEffect"]
