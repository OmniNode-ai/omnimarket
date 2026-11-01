# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one place that builds this node's handlers."""

from omnimarket.nodes.node_baseline_capture.handlers.handler_baseline_capture import (
    HandlerBaselineCapture,
    ProbeProtocol,
)


def build_handler_baseline_capture(
    probe_registry: dict[str, ProbeProtocol] | None,
) -> HandlerBaselineCapture:
    """Build the capture handler with the supplied probe registry."""
    return HandlerBaselineCapture(probe_registry=probe_registry)
