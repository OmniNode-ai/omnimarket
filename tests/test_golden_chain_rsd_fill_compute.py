# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain test for node_rsd_fill_compute.

Verifies top-N selection by RSD score, deterministic tie-breaking,
empty input handling, and EventBusInmemory wiring.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnimarket.nodes.node_rsd_fill_compute.handlers.handler_rsd_fill import (
    HandlerRsdFill,
)
from omnimarket.nodes.node_rsd_fill_compute.models.model_scored_ticket import (
    ModelScoredTicket,
)


def _ticket(
    ticket_id: str = "OMN-1001",
    title: str = "Test ticket",
    rsd_score: float = 5.0,
    priority: int = 2,
) -> ModelScoredTicket:
    return ModelScoredTicket(
        ticket_id=ticket_id,
        title=title,
        rsd_score=rsd_score,
        priority=priority,
    )


@pytest.mark.unit
class TestRsdFillComputeGoldenChain:
    """Golden chain: scored tickets in -> top-N selected out."""

    async def test_select_top_n(self, event_bus: EventBusInmemory) -> None:
        """Select top-2 from 4 candidates by RSD score."""
        handler = HandlerRsdFill()
        cid = uuid4()
        tickets = (
            _ticket("OMN-1", rsd_score=3.0),
            _ticket("OMN-2", rsd_score=8.0),
            _ticket("OMN-3", rsd_score=5.0),
            _ticket("OMN-4", rsd_score=10.0),
        )

        result = await handler.handle(
            correlation_id=cid, scored_tickets=tickets, max_tickets=2
        )

        assert result.correlation_id == cid
        assert result.total_candidates == 4
        assert result.total_selected == 2
        selected_ids = [t.ticket_id for t in result.selected_tickets]
        assert selected_ids == ["OMN-4", "OMN-2"]

    async def test_tie_break_by_priority(self, event_bus: EventBusInmemory) -> None:
        """Same RSD score: lower priority number (more urgent) wins."""
        handler = HandlerRsdFill()
        cid = uuid4()
        tickets = (
            _ticket("OMN-A", rsd_score=5.0, priority=3),
            _ticket("OMN-B", rsd_score=5.0, priority=1),
            _ticket("OMN-C", rsd_score=5.0, priority=2),
        )

        result = await handler.handle(
            correlation_id=cid, scored_tickets=tickets, max_tickets=2
        )

        selected_ids = [t.ticket_id for t in result.selected_tickets]
        assert selected_ids == ["OMN-B", "OMN-C"]

    async def test_tie_break_by_ticket_id(self, event_bus: EventBusInmemory) -> None:
        """Same RSD score and priority: ticket_id ASC wins."""
        handler = HandlerRsdFill()
        cid = uuid4()
        tickets = (
            _ticket("OMN-300", rsd_score=5.0, priority=2),
            _ticket("OMN-100", rsd_score=5.0, priority=2),
            _ticket("OMN-200", rsd_score=5.0, priority=2),
        )

        result = await handler.handle(
            correlation_id=cid, scored_tickets=tickets, max_tickets=2
        )

        selected_ids = [t.ticket_id for t in result.selected_tickets]
        assert selected_ids == ["OMN-100", "OMN-200"]

    async def test_empty_candidates(self, event_bus: EventBusInmemory) -> None:
        """No candidates returns empty selection."""
        handler = HandlerRsdFill()
        cid = uuid4()

        result = await handler.handle(
            correlation_id=cid, scored_tickets=(), max_tickets=5
        )

        assert result.total_candidates == 0
        assert result.total_selected == 0
        assert len(result.selected_tickets) == 0

    async def test_max_tickets_exceeds_candidates(
        self, event_bus: EventBusInmemory
    ) -> None:
        """max_tickets > candidates returns all candidates."""
        handler = HandlerRsdFill()
        cid = uuid4()
        tickets = (
            _ticket("OMN-1", rsd_score=5.0),
            _ticket("OMN-2", rsd_score=3.0),
        )

        result = await handler.handle(
            correlation_id=cid, scored_tickets=tickets, max_tickets=10
        )

        assert result.total_selected == 2
        assert result.total_candidates == 2

    async def test_single_ticket(self, event_bus: EventBusInmemory) -> None:
        """Single ticket is selected."""
        handler = HandlerRsdFill()
        cid = uuid4()
        tickets = (_ticket("OMN-SOLO", rsd_score=42.0),)

        result = await handler.handle(
            correlation_id=cid, scored_tickets=tickets, max_tickets=1
        )

        assert result.total_selected == 1
        assert result.selected_tickets[0].ticket_id == "OMN-SOLO"

    async def test_event_bus_wiring(self, event_bus: EventBusInmemory) -> None:
        """Handler events can be wired through EventBusInmemory."""
        handler = HandlerRsdFill()
        completed_events: list[dict[str, object]] = []
        cmd_topic = "onex.cmd.omnimarket.rsd-fill-start.v1"
        evt_topic = "onex.evt.omnimarket.rsd-fill-completed.v1"

        async def on_command(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            cid = uuid4()
            tickets = tuple(ModelScoredTicket(**t) for t in payload.get("tickets", []))
            result = await handler.handle(
                correlation_id=cid,
                scored_tickets=tickets,
                max_tickets=payload.get("max_tickets", 5),
            )
            result_dict = result.model_dump(mode="json")
            completed_events.append(result_dict)
            await event_bus.publish(
                evt_topic, key=None, value=json.dumps(result_dict).encode()
            )

        await event_bus.start()
        await event_bus.subscribe(
            cmd_topic, on_message=on_command, group_id="test-rsd-fill"
        )

        cmd_payload = json.dumps(
            {
                "tickets": [
                    {
                        "ticket_id": "OMN-1",
                        "title": "Test",
                        "rsd_score": 8.0,
                        "priority": 1,
                    },
                    {
                        "ticket_id": "OMN-2",
                        "title": "Test 2",
                        "rsd_score": 3.0,
                        "priority": 2,
                    },
                ],
                "max_tickets": 1,
            }
        ).encode()
        await event_bus.publish(cmd_topic, key=None, value=cmd_payload)

        assert len(completed_events) == 1
        assert completed_events[0]["total_selected"] == 1

        await event_bus.close()
