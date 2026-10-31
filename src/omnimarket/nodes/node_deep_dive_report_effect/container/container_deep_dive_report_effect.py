# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one place that builds this node's handlers."""

from omnimarket.nodes.node_deep_dive_report_effect.handlers import (
    HandlerTechnicalDeepDive,
)


def build_handler_technical_deep_dive() -> HandlerTechnicalDeepDive:
    """Build the handler with its constructor defaults."""
    return HandlerTechnicalDeepDive()
