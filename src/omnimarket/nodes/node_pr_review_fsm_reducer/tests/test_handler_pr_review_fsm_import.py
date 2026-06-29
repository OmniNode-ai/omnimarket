# SPDX-License-Identifier: MIT
"""Node-local dep-health coverage for handler_pr_review_fsm."""

from omnimarket.nodes.node_pr_review_fsm_reducer.handlers.handler_pr_review_fsm import (
    HandlerPrReviewFsm,
)


def test_handler_pr_review_fsm_importable() -> None:
    assert HandlerPrReviewFsm is not None
