"""Package-local smoke coverage for handler_golden_chain_generator."""

from omnimarket.nodes.node_golden_chain_generator_compute.handlers.handler_golden_chain_generator import (
    HandlerGoldenChainGenerator,
)


def test_handler_golden_chain_generator_importable() -> None:
    assert HandlerGoldenChainGenerator.__name__ == "HandlerGoldenChainGenerator"
