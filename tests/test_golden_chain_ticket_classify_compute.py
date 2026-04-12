# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain test for node_ticket_classify_compute.

Verifies ticket classification logic using keyword heuristics.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnimarket.nodes.node_ticket_classify_compute.handlers.handler_ticket_classify import (
    HandlerTicketClassify,
)
from omnimarket.nodes.node_ticket_classify_compute.models.enum_buildability import (
    EnumBuildability,
)
from omnimarket.nodes.node_ticket_classify_compute.models.model_ticket_for_classification import (
    ModelTicketForClassification,
)


@pytest.mark.unit
class TestTicketClassifyComputeGoldenChain:
    """Golden chain: tickets in -> classifications out."""

    async def test_auto_buildable_ticket(self, event_bus: EventBusInmemory) -> None:
        """Ticket with implementation keywords should be AUTO_BUILDABLE."""
        handler = HandlerTicketClassify()
        correlation_id = uuid4()
        tickets = (
            ModelTicketForClassification(
                ticket_id="OMN-1001",
                title="Implement new handler for node registration",
                description="Add a handler that registers nodes",
                labels=("compute",),
                state="Todo",
            ),
        )

        result = await handler.handle(correlation_id=correlation_id, tickets=tickets)

        assert result.correlation_id == correlation_id
        assert len(result.classifications) == 1
        assert result.classifications[0].buildability == EnumBuildability.AUTO_BUILDABLE
        assert result.total_auto_buildable == 1
        assert result.total_non_buildable == 0

    async def test_skip_terminal_state(self, event_bus: EventBusInmemory) -> None:
        """Ticket in terminal state should be SKIP."""
        handler = HandlerTicketClassify()
        correlation_id = uuid4()
        tickets = (
            ModelTicketForClassification(
                ticket_id="OMN-1002",
                title="Some completed ticket",
                state="Done",
            ),
        )

        result = await handler.handle(correlation_id=correlation_id, tickets=tickets)

        assert len(result.classifications) == 1
        assert result.classifications[0].buildability == EnumBuildability.SKIP
        assert result.total_non_buildable == 1

    async def test_skip_keyword_match(self, event_bus: EventBusInmemory) -> None:
        """Ticket with skip keywords should be SKIP."""
        handler = HandlerTicketClassify()
        correlation_id = uuid4()
        tickets = (
            ModelTicketForClassification(
                ticket_id="OMN-1003",
                title="WIP: draft implementation",
                state="In Progress",
            ),
        )

        result = await handler.handle(correlation_id=correlation_id, tickets=tickets)

        assert result.classifications[0].buildability == EnumBuildability.SKIP

    async def test_blocked_ticket(self, event_bus: EventBusInmemory) -> None:
        """Ticket with blocked keywords should be BLOCKED."""
        handler = HandlerTicketClassify()
        correlation_id = uuid4()
        tickets = (
            ModelTicketForClassification(
                ticket_id="OMN-1004",
                title="Integrate third-party auth provider",
                description="Blocked waiting on vendor API access",
                state="Todo",
            ),
        )

        result = await handler.handle(correlation_id=correlation_id, tickets=tickets)

        assert result.classifications[0].buildability == EnumBuildability.BLOCKED
        assert result.total_non_buildable == 1

    async def test_needs_arch_decision(self, event_bus: EventBusInmemory) -> None:
        """Ticket requiring architecture decisions should be NEEDS_ARCH_DECISION."""
        handler = HandlerTicketClassify()
        correlation_id = uuid4()
        tickets = (
            ModelTicketForClassification(
                ticket_id="OMN-1005",
                title="RFC: evaluate new architecture for event routing",
                state="Todo",
            ),
        )

        result = await handler.handle(correlation_id=correlation_id, tickets=tickets)

        assert (
            result.classifications[0].buildability
            == EnumBuildability.NEEDS_ARCH_DECISION
        )

    async def test_multiple_tickets_mixed(self, event_bus: EventBusInmemory) -> None:
        """Multiple tickets should be classified independently."""
        handler = HandlerTicketClassify()
        correlation_id = uuid4()
        tickets = (
            ModelTicketForClassification(
                ticket_id="OMN-2001",
                title="Fix broken handler registration",
                state="Todo",
            ),
            ModelTicketForClassification(
                ticket_id="OMN-2002",
                title="Old stale ticket",
                state="Done",
            ),
            ModelTicketForClassification(
                ticket_id="OMN-2003",
                title="Spike: research tradeoff for new DB",
                state="Todo",
            ),
        )

        result = await handler.handle(correlation_id=correlation_id, tickets=tickets)

        assert len(result.classifications) == 3
        assert result.total_auto_buildable == 1
        assert result.total_non_buildable == 2

    async def test_default_auto_buildable(self, event_bus: EventBusInmemory) -> None:
        """Ticket with no matching keywords defaults to AUTO_BUILDABLE."""
        handler = HandlerTicketClassify()
        correlation_id = uuid4()
        tickets = (
            ModelTicketForClassification(
                ticket_id="OMN-3001",
                title="Something generic",
                state="Todo",
            ),
        )

        result = await handler.handle(correlation_id=correlation_id, tickets=tickets)

        assert result.classifications[0].buildability == EnumBuildability.AUTO_BUILDABLE
        assert result.classifications[0].confidence == 0.3  # base confidence

    async def test_event_bus_wiring(self, event_bus: EventBusInmemory) -> None:
        """Handler can be wired to event bus for command/completion flow."""
        handler = HandlerTicketClassify()
        completions: list[dict[str, object]] = []

        async def on_command(message: object) -> None:
            correlation_id = uuid4()
            tickets = (
                ModelTicketForClassification(
                    ticket_id="OMN-4001",
                    title="Create new compute node",
                    state="Todo",
                ),
            )
            result = await handler.handle(
                correlation_id=correlation_id, tickets=tickets
            )
            completion = {
                "total_auto_buildable": result.total_auto_buildable,
                "total_non_buildable": result.total_non_buildable,
                "count": len(result.classifications),
            }
            completions.append(completion)
            await event_bus.publish(
                "onex.evt.omnimarket.ticket-classify-completed.v1",
                key=None,
                value=json.dumps(completion).encode(),
            )

        await event_bus.start()
        await event_bus.subscribe(
            "onex.cmd.omnimarket.ticket-classify-start.v1",
            on_message=on_command,
            group_id="test-classify",
        )

        await event_bus.publish(
            "onex.cmd.omnimarket.ticket-classify-start.v1",
            key=None,
            value=b'{"classify": "all"}',
        )

        assert len(completions) == 1
        assert completions[0]["total_auto_buildable"] == 1

        history = await event_bus.get_event_history(
            topic="onex.evt.omnimarket.ticket-classify-completed.v1"
        )
        assert len(history) == 1

        await event_bus.close()
