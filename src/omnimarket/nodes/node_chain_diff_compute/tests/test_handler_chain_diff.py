"""Package-local smoke coverage for handler_chain_diff."""

from omnimarket.nodes.node_chain_diff_compute.handlers.handler_chain_diff import (
    HandlerChainDiff,
)


def test_handler_chain_diff_importable() -> None:
    assert HandlerChainDiff.__name__ == "HandlerChainDiff"
