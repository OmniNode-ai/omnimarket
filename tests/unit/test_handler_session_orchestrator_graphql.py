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
    SessionLinearFetchError,
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
    assert 'project: { name: { eq: "Active Sprint" } }' in source, (
        "Session Phase 2 must only score tickets from the Active Sprint project"
    )


@pytest.mark.unit
def test_fetch_linear_active_tickets_raises_on_http_error() -> None:
    """Network failures must not be indistinguishable from an empty sprint.

    Returning [] here causes silent zero-dispatch: callers cannot tell whether
    Linear is unavailable or the Active Sprint is legitimately empty.
    """
    handler = HandlerSessionOrchestrator()

    with (
        patch(
            "omnimarket.nodes.node_session_orchestrator.handlers.handler_session_orchestrator.urllib.request.urlopen",
            side_effect=OSError("network unreachable"),
        ),
        pytest.raises(SessionLinearFetchError),
    ):
        handler._fetch_linear_active_tickets("fake-key")
