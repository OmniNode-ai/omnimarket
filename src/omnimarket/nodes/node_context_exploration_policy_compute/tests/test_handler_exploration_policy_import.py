# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Node-local dep-health coverage for handler_exploration_policy."""

from omnimarket.nodes.node_context_exploration_policy_compute.handlers.handler_exploration_policy import (
    HandlerExplorationPolicy,
)


def test_handler_exploration_policy_imports() -> None:
    assert HandlerExplorationPolicy() is not None
