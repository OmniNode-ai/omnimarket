# SPDX-License-Identifier: MIT
"""Node-local dep-health coverage for handler_pr_review_orchestrator."""

from omnimarket.nodes.node_pr_review_orchestrator.handlers.handler_pr_review_orchestrator import (
    HandlerPrReviewOrchestrator,
)


def test_handler_pr_review_orchestrator_importable() -> None:
    assert HandlerPrReviewOrchestrator is not None
