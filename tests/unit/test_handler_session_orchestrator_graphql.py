"""Regression tests for the Linear GraphQL query built by HandlerSessionOrchestrator.

OMN-9561: `orderBy: priority` is not a valid Linear PaginationOrderBy enum value
(only `createdAt` and `updatedAt` are accepted). Linear returns HTTP 400 and the
generic `except Exception` swallows the error, causing silent zero-dispatch from
/onex:session Phase 2.
"""

from __future__ import annotations

import inspect
from unittest.mock import patch

import pytest

from omnimarket.nodes.node_session_orchestrator.handlers.handler_session_orchestrator import (
    HandlerSessionOrchestrator,
)


@pytest.mark.unit
def test_fetch_linear_active_tickets_query_uses_valid_orderby() -> None:
    """The GraphQL query must not use the invalid `orderBy: priority` enum.

    Linear's PaginationOrderBy enum only accepts `createdAt` and `updatedAt`.
    Using `priority` raises HTTP 400, which _fetch_linear_active_tickets silently
    converts to an empty list — causing /onex:session to dispatch 0 tickets.
    """
    source = inspect.getsource(HandlerSessionOrchestrator._fetch_linear_active_tickets)

    assert "orderBy: priority" not in source, (
        "orderBy: priority is not a valid Linear PaginationOrderBy value — "
        "use orderBy: updatedAt and sort by priority client-side"
    )
    assert "orderBy: updatedAt" in source, (
        "GraphQL query must include orderBy: updatedAt (the valid enum value)"
    )


@pytest.mark.unit
def test_fetch_linear_active_tickets_returns_empty_on_http_error() -> None:
    """Regression guard: network failures return [] rather than crashing.

    The orderBy fix restores happy-path behavior, but the defensive catch must
    stay intact for real network failures.
    """
    handler = HandlerSessionOrchestrator()

    with patch(
        "omnimarket.nodes.node_session_orchestrator.handlers.handler_session_orchestrator.urllib.request.urlopen",
        side_effect=OSError("network unreachable"),
    ):
        result = handler._fetch_linear_active_tickets("fake-key")

    assert result == []
