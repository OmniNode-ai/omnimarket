# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerTicketFacts reads a ticket's state type and assignee id from the tracker."""

from __future__ import annotations

import httpx
import pytest

from omnimarket.models.ticket_query.model_ticket_facts import ModelTicketFactsInput
from omnimarket.nodes.node_ticket_query.handlers.handler_ticket_facts import (
    HandlerTicketFacts,
)


class _Tracker:
    def __init__(self, answer: dict[str, object]) -> None:
        self._answer = answer

    async def post_graphql(
        self, query: str, variables: dict[str, object] | None = None
    ) -> httpx.Response:
        return httpx.Response(200, json=self._answer)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_facts_carry_state_type_and_assignee_id() -> None:
    node = {
        "identifier": "OMN-1",
        "state": {"type": "started"},
        "assignee": {"id": "me"},
    }
    tracker = _Tracker({"data": {"issues": {"nodes": [node]}}})

    async def provider() -> _Tracker:
        return tracker

    out = await HandlerTicketFacts(tracker_provider=provider).handle(
        ModelTicketFactsInput(identifier="OMN-1")
    )
    assert (out.identifier, out.state_type, out.assignee_id) == (
        "OMN-1",
        "started",
        "me",
    )
