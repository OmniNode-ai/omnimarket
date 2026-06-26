# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain test for node_ticket_query.

Verifies HandlerTicketQuery dispatches through ProtocolProjectTracker,
never calls mcp__linear-server__ directly.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from omnimarket.nodes.node_ticket_query.handlers.handler_ticket_query import (
    HandlerTicketQuery,
)
from omnimarket.nodes.node_ticket_query.models.model_ticket_query_input import (
    ModelTicketQueryInput,
)
from omnimarket.nodes.node_ticket_query.models.model_ticket_query_output import (
    ModelIssueResult,
)


def _make_issue(identifier: str, title: str = "Test Issue") -> ModelIssueResult:
    return ModelIssueResult(
        id=str(uuid4()),
        identifier=identifier,
        title=title,
        state="Todo",
    )


def _handler_with_tracker(tracker: AsyncMock) -> HandlerTicketQuery:
    """Build a container-driven HandlerTicketQuery that resolves *tracker*.

    Mirrors the runtime resolver path (OMN-13603): the handler takes the
    injectable container and resolves ProtocolProjectTracker from it at the
    effect boundary.
    """
    container = MagicMock()
    container.get_service.return_value = tracker
    return HandlerTicketQuery(container=container)


@pytest.mark.asyncio
@pytest.mark.unit
class TestTicketQueryGoldenChain:
    """Golden chain: input -> HandlerTicketQuery -> ProtocolProjectTracker -> output."""

    async def test_search_dispatches_through_protocol(self) -> None:
        """search_issues is called on the tracker, not MCP."""
        tracker = AsyncMock()
        tracker.search_issues = AsyncMock(
            return_value=[_make_issue("OMN-8771"), _make_issue("OMN-8772")]
        )
        handler = _handler_with_tracker(tracker)

        result = await handler.handle(
            correlation_id=uuid4(),
            input_data=ModelTicketQueryInput(query="OMN-8771", limit=10),
        )

        tracker.search_issues.assert_awaited_once_with("OMN-8771", limit=10)
        tracker.list_issues.assert_not_awaited()
        tracker.get_issue.assert_not_awaited()
        assert result.total == 2
        assert result.query == "OMN-8771"
        assert result.issues[0].identifier == "OMN-8771"

    async def test_list_dispatches_through_protocol(self) -> None:
        """list_issues is called when no query or issue_id is provided."""
        tracker = AsyncMock()
        tracker.list_issues = AsyncMock(return_value=[_make_issue("OMN-9000")])
        handler = _handler_with_tracker(tracker)

        result = await handler.handle(
            correlation_id=uuid4(),
            input_data=ModelTicketQueryInput(
                filters={"state": "Todo", "team": "Omninode"}, limit=50
            ),
        )

        tracker.list_issues.assert_awaited_once_with(
            filters={"state": "Todo", "team": "Omninode"}, limit=50
        )
        tracker.search_issues.assert_not_awaited()
        tracker.get_issue.assert_not_awaited()
        assert result.total == 1

    async def test_single_issue_fetch_dispatches_through_protocol(self) -> None:
        """get_issue is called when issue_id is set."""
        tracker = AsyncMock()
        tracker.get_issue = AsyncMock(return_value=_make_issue("OMN-8771"))
        handler = _handler_with_tracker(tracker)

        result = await handler.handle(
            correlation_id=uuid4(),
            input_data=ModelTicketQueryInput(issue_id="OMN-8771"),
        )

        tracker.get_issue.assert_awaited_once_with("OMN-8771")
        tracker.search_issues.assert_not_awaited()
        tracker.list_issues.assert_not_awaited()
        assert result.total == 1
        assert result.issue_id == "OMN-8771"

    async def test_empty_results_handled(self) -> None:
        """Empty result list is returned cleanly."""
        tracker = AsyncMock()
        tracker.search_issues = AsyncMock(return_value=[])
        handler = _handler_with_tracker(tracker)

        result = await handler.handle(
            correlation_id=uuid4(),
            input_data=ModelTicketQueryInput(query="nonexistent-xyz"),
        )

        tracker.search_issues.assert_awaited_once()
        tracker.list_issues.assert_not_awaited()
        tracker.get_issue.assert_not_awaited()
        assert result.total == 0
        assert result.issues == ()
