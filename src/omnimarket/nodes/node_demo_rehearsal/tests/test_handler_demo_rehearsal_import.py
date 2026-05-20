# SPDX-License-Identifier: MIT
"""Node-local dep-health coverage for handler_demo_rehearsal."""

from omnimarket.nodes.node_demo_rehearsal.handlers.handler_demo_rehearsal import (
    HandlerDemoRehearsal,
)


def test_handler_demo_rehearsal_importable() -> None:
    assert HandlerDemoRehearsal is not None
