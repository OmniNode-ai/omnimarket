"""Package-local smoke coverage for handler_closeout_verifier."""

from omnimarket.nodes.node_closeout_verifier_compute.handlers.handler_closeout_verifier import (
    HandlerCloseoutVerifier,
)


def test_handler_closeout_verifier_importable() -> None:
    assert HandlerCloseoutVerifier.__name__ == "HandlerCloseoutVerifier"
