# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler that queries tickets via ProtocolProjectTracker.

This is an EFFECT handler — it calls the project tracker adapter, which
in turn calls the Linear API. No mcp__linear-server__ calls here.

Container-driven pattern (OMN-13603): the handler takes the injectable
``container`` so the runtime resolver constructs it at boot via known-param
injection. The ``ProtocolProjectTracker`` adapter is resolved from the
container at the effect boundary inside ``handle()`` — never at construction
time — so an unregistered tracker no longer quarantines the handler at boot;
it fails loud only when the operation actually runs. The adapter itself owns
``api_key_ref`` secret resolution at its own effect boundary, so the handler
never touches a literal credential.

Related:
    - OMN-8772: Create missing ProtocolProjectTracker handler nodes
    - OMN-8771: Replace hardcoded mcp__linear-server__ in skill prompts
    - OMN-13603: wire ProtocolProjectTracker via container-driven DI at boot
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

from omnibase_spi.protocols.services.protocol_project_tracker import (
    ProtocolProjectTracker,
)

from omnimarket.nodes.node_ticket_query.models.model_ticket_query_input import (
    ModelTicketQueryInput,
)
from omnimarket.nodes.node_ticket_query.models.model_ticket_query_output import (
    ModelIssueResult,
    ModelTicketQueryOutput,
)

if TYPE_CHECKING:
    from omnibase_core.container import ModelONEXContainer

logger = logging.getLogger(__name__)


class HandlerTicketQuery:
    """Queries tickets from ProtocolProjectTracker — no MCP calls.

    The tracker is resolved from the injected ``container`` at the effect
    boundary (inside ``handle()``), not stored at construction. This lets the
    runtime resolver build the handler at boot via known-param injection while
    the protocol adapter is resolved lazily when an operation runs.
    """

    def __init__(self, container: ModelONEXContainer) -> None:
        self._container = container

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["EFFECT"]:
        return "EFFECT"

    def _resolve_tracker(self) -> ProtocolProjectTracker:
        """Resolve ProtocolProjectTracker from the container at the effect boundary.

        Fails loud (not silent) when no tracker is registered: a ticket-query
        EFFECT with no project tracker provider cannot do its job, so the caller
        must see the wiring gap rather than an empty result.
        """
        # NOTE(OMN-13603): mypy false-positive — a Protocol is the canonical DI
        # key for get_service; it is never instantiated here.
        return self._container.get_service(
            ProtocolProjectTracker  # type: ignore[type-abstract]  # Protocol used as DI key
        )

    async def handle(self, request: ModelTicketQueryInput) -> ModelTicketQueryOutput:
        """Execute a ticket query through ProtocolProjectTracker.

        If ``issue_id`` is set, fetches a single issue via get_issue.
        If ``query`` is set, searches via search_issues.
        Otherwise, lists issues via list_issues with optional filters.

        Args:
            request: Query parameters.

        Returns:
            ModelTicketQueryOutput with matching issues.
        """
        logger.info(
            "TicketQuery: query_set=%s has_issue_id=%s limit=%d",
            request.query is not None,
            request.issue_id is not None,
            request.limit,
        )
        logger.debug(
            "TicketQuery: query=%r issue_id=%r",
            request.query,
            request.issue_id,
        )

        tracker = self._resolve_tracker()

        if request.issue_id is not None:
            raw_issue = await tracker.get_issue(request.issue_id)
            raw_issues = [raw_issue]
        elif request.query is not None:
            raw_issues = await tracker.search_issues(request.query, limit=request.limit)
        else:
            raw_issues = await tracker.list_issues(
                filters=request.filters, limit=request.limit
            )

        issues = tuple(ModelIssueResult(**issue.model_dump()) for issue in raw_issues)

        logger.info("TicketQuery: returned %d issues", len(issues))

        return ModelTicketQueryOutput(
            issues=issues,
            total=len(issues),
            query=request.query,
            issue_id=request.issue_id,
        )
