# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host observer for automatic processes that cannot emit (OMN-20801)."""

from omnimarket.nodes.node_automation_run_observer_effect.handlers import (
    HandlerAutomationRunObserver,
)

__all__ = ["HandlerAutomationRunObserver"]
