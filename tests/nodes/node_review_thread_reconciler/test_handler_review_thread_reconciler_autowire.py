# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-8735: HandlerReviewThreadReconciler auto-wiring compliance tests.

Verifies that HandlerReviewThreadReconciler can be constructed with no
arguments (github_client defaults to None) so the ONEX auto-wiring
system can instantiate it without DI configuration.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_review_thread_reconciler.handlers.handler_review_thread_reconciler import (
    HandlerReviewThreadReconciler,
    ModelReviewThreadReconcileCommand,
)


@pytest.mark.unit
def test_handler_review_thread_reconciler_constructs_with_no_args() -> None:
    """Auto-wiring compliance: handler must be constructable with zero arguments."""
    handler = HandlerReviewThreadReconciler()
    assert handler is not None
    assert handler._client is None


@pytest.mark.unit
def test_handler_review_thread_reconciler_raises_on_handle_without_client() -> None:
    """Null-guard: calling handle() without a client raises RuntimeError, not AttributeError."""
    handler = HandlerReviewThreadReconciler()
    command = ModelReviewThreadReconcileCommand(
        thread_id="T_abc123",
        pr_node_id="PR_abc123",
        repo="owner/repo",
        pr_number=42,
        resolved_by="some-user",
        correlation_id="corr-001",
    )
    with pytest.raises(RuntimeError, match="github_client is not configured"):
        handler.handle(command)
