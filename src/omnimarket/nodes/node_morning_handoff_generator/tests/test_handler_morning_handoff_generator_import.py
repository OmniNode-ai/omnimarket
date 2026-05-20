# SPDX-License-Identifier: MIT
"""Node-local dep-health coverage for handler_morning_handoff_generator."""

from omnimarket.nodes.node_morning_handoff_generator.handlers.handler_morning_handoff_generator import (
    HandlerMorningHandoffGenerator,
)


def test_handler_morning_handoff_generator_importable() -> None:
    assert HandlerMorningHandoffGenerator is not None
