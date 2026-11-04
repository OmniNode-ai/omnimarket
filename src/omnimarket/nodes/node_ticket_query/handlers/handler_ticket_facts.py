# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""read_ticket_facts: a ticket's workflow state type and assignee id (OMN-22418).

``handle(request: ModelTicketFactsInput) -> ModelTicketFactsOutput``. The tracker
comes from ``tracker_provider`` at the effect boundary, so the credential is
resolved (from the store) only when the operation runs. The GraphQL POST is the
project tracker adapter's own ``post_graphql``; this handler opens no connection.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Literal

from omnimarket.models.ticket_query.model_ticket_facts import (
    ModelTicketFactsInput,
    ModelTicketFactsOutput,
)
from omnimarket.nodes.node_secret_store_effect.handlers.handler_secret_store import (
    HandlerSecretStore,
)
from omnimarket.nodes.node_ticket_query.handlers.store_resolved_tracker import (
    ProtocolGraphQLPoster,
    resolve_linear_tracker,
)

_QUERY = (
    "query TicketFacts($number: Float!, $team: String!) {"
    " issues(filter: {team: {key: {eq: $team}}, number: {eq: $number}}) {"
    " nodes { identifier state { type } assignee { id } } } }"
)


async def _store_resolved_tracker() -> ProtocolGraphQLPoster:
    return await resolve_linear_tracker(HandlerSecretStore())


class HandlerTicketFacts:
    """EFFECT: one read of a ticket's state type and assignee id."""

    def __init__(
        self,
        tracker_provider: Callable[[], Awaitable[ProtocolGraphQLPoster]] | None = None,
    ) -> None:
        # With none, the tracker is built over a key the secret store node
        # resolves when the operation runs (never over an environment key).
        self._tracker_provider = tracker_provider or _store_resolved_tracker

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["EFFECT"]:
        return "EFFECT"

    async def handle(self, request: ModelTicketFactsInput) -> ModelTicketFactsOutput:
        team, _, number = request.identifier.partition("-")
        tracker = await self._tracker_provider()
        response = await tracker.post_graphql(
            _QUERY, {"number": float(number), "team": team}
        )
        if response.status_code != 200:
            raise ConnectionError(
                f"ticket facts for {request.identifier}: tracker answered "
                f"HTTP {response.status_code}"
            )
        body = response.json()
        if body.get("errors"):
            raise ConnectionError(
                f"ticket facts for {request.identifier}: tracker returned GraphQL errors"
            )
        data = body.get("data")
        issues = data.get("issues") if isinstance(data, dict) else None
        nodes = issues.get("nodes") if isinstance(issues, dict) else None
        if not isinstance(nodes, list) or not nodes:
            raise LookupError(f"{request.identifier} not found in the project tracker")
        node = nodes[0]
        assignee = node.get("assignee") or {}
        return ModelTicketFactsOutput(
            identifier=str(node.get("identifier", request.identifier)),
            state_type=str((node.get("state") or {}).get("type", "")),
            assignee_id=str(assignee.get("id", "")),
        )


__all__ = ["HandlerTicketFacts"]
