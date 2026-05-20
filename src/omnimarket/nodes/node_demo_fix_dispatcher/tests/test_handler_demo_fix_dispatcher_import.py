# SPDX-License-Identifier: MIT
"""Node-local dep-health coverage for handler_demo_fix_dispatcher."""

from omnimarket.nodes.node_demo_fix_dispatcher.handlers.handler_demo_fix_dispatcher import (
    HandlerDemoFixDispatcher,
)


def test_handler_demo_fix_dispatcher_importable() -> None:
    assert HandlerDemoFixDispatcher is not None
