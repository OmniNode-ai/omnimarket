# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Node-local coverage marker for handler_omnigate_projection."""

from __future__ import annotations

from omnimarket.nodes.node_omnigate_projection.handlers.handler_omnigate_projection import (
    HandlerOmniGateProjection,
)


def test_handler_omnigate_projection_imports() -> None:
    handler = HandlerOmniGateProjection()

    assert handler.handler_type == "NODE_HANDLER"
