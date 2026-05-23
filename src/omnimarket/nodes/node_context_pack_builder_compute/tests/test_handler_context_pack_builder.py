"""Package-local smoke coverage for handler_context_pack_builder."""

from omnimarket.nodes.node_context_pack_builder_compute.handlers.handler_context_pack_builder import (
    HandlerContextPackBuilder,
)


def test_handler_context_pack_builder_importable() -> None:
    assert HandlerContextPackBuilder.__name__ == "HandlerContextPackBuilder"
