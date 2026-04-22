# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler that queries tickets via ProtocolProjectTracker.

This is an EFFECT handler — it calls the project tracker adapter, which
in turn calls the Linear API. No mcp__linear-server__ calls here.

Related:
    - OMN-8772: Create missing ProtocolProjectTracker handler nodes
    - OMN-8771: Replace hardcoded mcp__linear-server__ in skill prompts
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from omnimarket.nodes.node_ticket_query.models.model_ticket_query_input import (
    ModelTicketQueryInput,
)
from omnimarket.nodes.node_ticket_query.models.model_ticket_query_output import (
    ModelIssueResult,
    ModelTicketQueryOutput,
)

if TYPE_CHECKING:
    from omnibase_spi.protocols.services.protocol_project_tracker import (
        ProtocolProjectTracker,
    )

logger = logging.getLogger(__name__)


class HandlerTicketQuery:
    """Queries tickets from ProtocolProjectTracker — no MCP calls."""

    def __init__(self, tracker: ProtocolProjectTracker) -> None:
        self._tracker = tracker

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["EFFECT"]:
        return "EFFECT"

    async def handle(
        self,
        correlation_id: UUID,
        input_data: ModelTicketQueryInput,
    ) -> ModelTicketQueryOutput:
        """Execute a ticket query through ProtocolProjectTracker.

        If ``issue_id`` is set, fetches a single issue via get_issue.
        If ``query`` is set, searches via search_issues.
        Otherwise, lists issues via list_issues with optional filters.

        Args:
            correlation_id: Trace ID for this operation.
            input_data: Query parameters.

        Returns:
            ModelTicketQueryOutput with matching issues.
        """
        logger.info(
            "TicketQuery: correlation_id=%s query_set=%s has_issue_id=%s limit=%d",
            correlation_id,
            input_data.query is not None,
            input_data.issue_id is not None,
            input_data.limit,
        )
        logger.debug(
            "TicketQuery: query=%r issue_id=%r",
            input_data.query,
            input_data.issue_id,
        )

        if input_data.issue_id is not None:
            raw_issue = await self._tracker.get_issue(input_data.issue_id)
            raw_issues = [raw_issue]
        elif input_data.query is not None:
            raw_issues = await self._tracker.search_issues(
                input_data.query, limit=input_data.limit
            )
        else:
            raw_issues = await self._tracker.list_issues(
                filters=input_data.filters, limit=input_data.limit
            )

        issues = tuple(ModelIssueResult(**issue.model_dump()) for issue in raw_issues)

        logger.info("TicketQuery: returned %d issues", len(issues))

        return ModelTicketQueryOutput(
            issues=issues,
            total=len(issues),
            query=input_data.query,
            issue_id=input_data.issue_id,
        )
