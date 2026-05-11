# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-8735: HandlerReviewThreadReconciler auto-wiring compliance tests.

Verifies that HandlerReviewThreadReconciler can be constructed with only
the mandatory event_bus argument (github_client defaults to None) so the
ONEX auto-wiring system can instantiate it.
"""

from __future__ import annotations

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_review_thread_reconciler.handlers.handler_review_thread_reconciler import (
    HandlerReviewThreadReconciler,
    ModelReviewThreadReconcileCommand,
)


@pytest.mark.unit
def test_handler_review_thread_reconciler_constructs_with_event_bus() -> None:
    """Auto-wiring compliance: handler must be constructable with only event_bus."""
    handler = HandlerReviewThreadReconciler(event_bus=EventBusInmemory())
    assert handler is not None
    assert handler._client is None


@pytest.mark.unit
def test_handler_review_thread_reconciler_raises_on_handle_without_client() -> None:
    """Null-guard: calling handle() without a client raises RuntimeError, not AttributeError."""
    handler = HandlerReviewThreadReconciler(event_bus=EventBusInmemory())
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
