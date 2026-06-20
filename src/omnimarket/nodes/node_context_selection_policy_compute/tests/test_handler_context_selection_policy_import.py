# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Node-local dep-health coverage for handler_context_selection_policy."""

from omnimarket.nodes.node_context_selection_policy_compute.handlers.handler_context_selection_policy import (
    HandlerContextSelectionPolicy,
)


def test_handler_context_selection_policy_imports() -> None:
    assert HandlerContextSelectionPolicy() is not None
