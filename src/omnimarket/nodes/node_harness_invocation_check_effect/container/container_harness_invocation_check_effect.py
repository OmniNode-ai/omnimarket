# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one place that builds this node's handlers."""

from omnimarket.nodes.node_harness_invocation_check_effect.handlers.handler_harness_invocation_check import (
    HandlerHarnessInvocationCheck,
)


def build_handler_harness_invocation_check() -> HandlerHarnessInvocationCheck:
    """Build the handler with its constructor defaults."""
    return HandlerHarnessInvocationCheck()
