"""Package-local smoke coverage for handler_test_generator."""

from omnimarket.nodes.node_test_generator_compute.handlers.handler_test_generator import (
    HandlerTestGenerator,
)


def test_handler_test_generator_importable() -> None:
    assert HandlerTestGenerator.__name__ == "HandlerTestGenerator"
